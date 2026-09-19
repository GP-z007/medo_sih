"""Inspected IndiGo booking interactions, executed by Eura without a model."""
import asyncio

from udaan.contracts import DomainError
from udaan.planner_contracts import LearnedAction

FROM='div.popover__wrapper.search-widget-form-body__from'
TO='div.popover__wrapper.search-widget-form-body__to'
DEPARTURE='div.popover__wrapper.search-widget-form-body__departure'
PASSENGERS='div.popover__wrapper.search-widget-form-body__pax-fare-selection'


async def airport(browser, wrapper_css, code, guard):
    await guard()
    await asyncio.sleep(.7)
    wrapper=browser.page.locator(wrapper_css)
    inputs=wrapper.locator('input[role="combobox"]')
    visible=[inputs.nth(i) for i in range(await inputs.count()) if await inputs.nth(i).is_visible()]
    if not visible:
        await wrapper.click(timeout=5000)
        await asyncio.sleep(.7)
        await guard()
    candidates=browser.page.locator('input[role="combobox"]')
    visible=[candidates.nth(i) for i in range(await candidates.count()) if await candidates.nth(i).is_visible()]
    if len(visible)!=1:
        raise DomainError('EXTRACTION_STRUCTURE_FAILED','Expected one active airport input')
    await visible[0].fill(code)
    deadline=asyncio.get_running_loop().time()+10
    while True:
        await guard()
        option=browser.page.locator('.city-selection__list').get_by_text(code, exact=True)
        visible=[option.nth(i) for i in range(await option.count()) if await option.nth(i).is_visible()]
        if len(visible)==1:
            break
        if asyncio.get_running_loop().time()>=deadline:
            raise DomainError('EXTRACTION_STRUCTURE_FAILED','Requested airport suggestion is unavailable or ambiguous')
        await asyncio.sleep(.5)
    await visible[0].click(timeout=5000)
    await asyncio.sleep(.7)
    await guard()
    # Selected structured airport data is a public booking value, never a token.
    selected=await wrapper.locator('input[role="combobox"]').first.get_attribute('data-value')
    if selected:
        import json
        try:
            if json.loads(selected).get('stationCode') != code:
                raise DomainError('EXTRACTION_SCHEMA_FAILED','Selected airport does not match the requested airport')
        except ValueError as exc:
            raise DomainError('EXTRACTION_SCHEMA_FAILED','Selected airport metadata is invalid') from exc


async def search(eura, browser, request, guard, checkpoint):
    if (request.adults,request.children,request.infants)!=(1,0,0):
        raise DomainError('UNSUPPORTED_PASSENGERS','This predefined recipe currently requires one adult')
    await checkpoint('Selecting requested airports')
    await browser.page.locator(FROM).wait_for(state='visible')
    consent=await browser.visible('a[role="button"][aria-label="Accept All"]')
    if consent:
        await guard()
        await consent.click()
    await airport(browser,FROM,request.origin,guard)
    await airport(browser,TO,request.destination,guard)
    await checkpoint('Selecting departure date')
    state=await browser.page_state(request,'departure_date',expanded=True)
    if not state or not any(x.get('calendar_date') for x in state['elements']):
        await guard()
        await browser.page.locator(DEPARTURE).click(timeout=5000)
        await asyncio.sleep(.7)
    for _ in range(13):
        await guard()
        await browser.page_state(request,'departure_date',expanded=True)
        dates=[x for x in browser._planner_targets.values() if x.get('calendar_date')==request.departure_date.isoformat() and x.get('enabled')]
        if len(dates)==1:
            step=LearnedAction(action='click',selector=dates[0]['selector'],goal='departure_date',value=None,label=str(request.departure_date),tag='button',calendar_day=True,verified=True)
            ok,reason=await browser.planner_execute(step,request,guard)
            if not ok:
                raise DomainError('EXTRACTION_SCHEMA_FAILED',reason)
            break
        if not eura.manifest.next_month_selector:
            raise DomainError('EXTRACTION_STRUCTURE_FAILED','Requested date is outside the currently inspected calendar view')
        await browser.page.locator(eura.manifest.next_month_selector).click(timeout=5000)
        await asyncio.sleep(.5)
    else:
        raise DomainError('EXTRACTION_STRUCTURE_FAILED','Requested departure date is unavailable')
    await checkpoint('Verifying one adult')
    continuation=browser.page.locator('button[aria-label="1 Passenger, Continue"]')
    if not await continuation.is_visible():
        await guard()
        await browser.page.locator(PASSENGERS).click(timeout=5000)
    await guard()
    await continuation.wait_for(state='visible',timeout=5000)
    counts = await browser.page.locator('.pax-fare-selection-popover .bw-seat-input--counter').evaluate_all("es => es.filter(e => e.getClientRects().length).map(e => ({kind:e.querySelector('.bw-seat-input--counter--left--pax-type')?.getAttribute('aria-label'),count:e.querySelector('.stepper-input--counter')?.innerText}))")
    expected = {'Adult 12 years onwards':'1', 'Senior Citizen (>60 years)':'0', 'Children (2 to 12 years)':'0', 'Infant (3 days to 2 years)':'0'}
    if len(counts) != 4 or {x['kind']:x['count'] for x in counts} != expected:
        raise DomainError('EXTRACTION_SCHEMA_FAILED', 'The visible passenger categories do not match one adult')
    if await browser.page.locator('.pax-fare-selection-popover [role="radio"][aria-checked="true"]').count():
        raise DomainError('EXTRACTION_SCHEMA_FAILED', 'A special eligibility fare is selected; standard public fares are required')
    await continuation.click(timeout=5000)
    await guard()
    await checkpoint('Searching flights')
    button=browser.page.get_by_role('button',name='Search',exact=True)
    await button.click(timeout=5000)
    for _ in range(60):
        await guard()
        if await browser.visible(eura.manifest.result_ready) is not None:
            eura._verified_search=(id(browser),request.model_dump(mode='json'))
            eura._verified_header=await browser.page.locator('.booking-header-widget__box').all_text_contents()
            return
        await asyncio.sleep(.5)
    raise DomainError('EXTRACTION_STRUCTURE_FAILED','Flight results did not become available')


def selected_day_matches(label, requested, today):
    """Resolve the site's yearless visible date only within its accepted search horizon."""
    from datetime import timedelta
    expected = 'Selected Date for ' + requested.strftime('%a, %d %b')
    if label != expected:
        return False
    matches = [today + timedelta(days=i) for i in range(1, 367)
               if 'Selected Date for ' + (today + timedelta(days=i)).strftime('%a, %d %b') == label]
    return matches == [requested]


async def context_matches(eura, browser, request):
    from udaan.db import now
    if getattr(eura, '_verified_search', None) != (id(browser), request.model_dump(mode='json')):
        return False
    selected = browser.page.locator('.flight-date-carousel-container__item.selected')
    if await selected.count() != 1 or not await selected.is_visible():
        return False
    if not selected_day_matches(await selected.get_attribute('aria-label'), request.departure_date, now().date()):
        return False
    if await browser.page.locator('.booking-header-widget__box').all_text_contents() != getattr(eura, '_verified_header', None):
        return False
    # Current page must still display the passenger count used by the verified search.
    import re
    text = await browser.page.locator('.booking-header-widget').inner_text()
    counts = set(re.findall(r'\b(\d+)\s+Passengers?\b', text))
    return counts == {'1'}


def fare_details(family):
    """Actual public benefit rows; conditional fee wording stays conditional."""
    result = {}
    rules = {'change_fee': [], 'cancellation_fee': []}
    for row in family.css('.fare-details__single'):
        # Upsell cards contain optional purchased benefits, not this family's inclusions.
        ancestor = row.parent
        upsell = False
        while ancestor is not None and ancestor != family:
            if any(x in ancestor.attrib.get('class', '') for x in ('upsell', 'benefits-card', 'upgrade-card')):
                upsell = True
                break
            ancestor = ancestor.parent
        if upsell:
            continue
        text = ' '.join(row.get_all_text().split())
        if row.css('.icon-cabin-bag'):
            result['cabin_baggage'] = text
        if row.css('.icon-checkin-bag, .icon-no-checkin-bag') and 'checkin_baggage' not in result:
            result['checkin_baggage'] = text
        if row.css('.icon-6e-eats') and 'complimentary' in text.casefold():
            result['meal_included'] = True
        # Retain every displayed condition; do not infer an unconditional numeric fee.
        for field, word in [('change_fee', 'change'), ('cancellation_fee', 'cancellation')]:
            if word in text.casefold():
                rules[field].append(text)
    for field, values in rules.items():
        if values:
            result[field] = '; '.join(dict.fromkeys(values))[:500]
    return result


async def discover_airports(eura, browser, guard, checkpoint, scope):
    from udaan.discovery import DiscoveryResult
    await checkpoint('Reading public booking airports')
    for _ in range(30):
        await guard()
        if getattr(browser, 'airport_catalog', None):
            break
        await asyncio.sleep(.5)
    else:
        raise DomainError('DISCOVERY_UNVERIFIED', 'The public booking airport catalog did not load')
    if scope == 'ROUTES':
        raise DomainError('DISCOVERY_UNVERIFIED', 'The station catalog does not contain route pairs. Test requested routes individually; full destination discovery is not yet validated.')
    return DiscoveryResult(origins=browser.airport_catalog, origins_complete=False,
                           reason='Read enabled Indian stations exposed by the booking widget; full origin and destination availability still needs individual interface verification.')
