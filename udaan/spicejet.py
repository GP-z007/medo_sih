"""SpiceJet's public booking controls, executed deterministically by Eura."""
import asyncio

from udaan.contracts import DomainError


def control(page, name):
    return page.locator(f'[data-testid="{name}"]')


async def select_airport(browser, field, code, guard):
    box = control(browser.page, 'to-testID-' + field)
    entry = box.locator('input')
    await guard()
    await entry.click(timeout=5000)
    await entry.fill(code)
    option = box.get_by_text(code, exact=True)
    for _ in range(30):
        await guard()
        # Exact codes can auto-select and move focus to the destination input.
        # Clicking the next list would then change the wrong airport.
        if (await entry.input_value()).endswith(f'({code})'):
            return
        if await option.count() == 1 and await option.is_visible():
            await option.click(timeout=5000)
        await asyncio.sleep(.25)
    raise DomainError('EXTRACTION_SCHEMA_FAILED', 'SpiceJet did not retain the selected airport')


async def search(eura, browser, request, guard, checkpoint):
    if (request.adults, request.children, request.infants) != (1, 0, 0):
        raise DomainError('UNSUPPORTED_PASSENGERS', 'SpiceJet currently validates one adult')
    await guard()
    await control(browser.page, 'one-way-radio-button').click(timeout=5000)
    for field, code in [('origin', request.origin), ('destination', request.destination)]:
        await checkpoint('Selecting ' + field + ' airport')
        await select_airport(browser, field, code, guard)
    await checkpoint('Selecting departure date')
    calendar = control(browser.page, 'undefined-calendar-picker')
    if not await calendar.is_visible():
        await control(browser.page, 'departure-date-dropdown-label-test-id').click(timeout=5000)
    day = request.departure_date
    month = control(browser.page, 'undefined-month-' + day.strftime('%B-%Y'))
    cell = control(month, f'undefined-calendar-day-{day.day}')
    await guard()
    if await cell.get_attribute('data-focusable') != 'true':
        raise DomainError('EXTRACTION_SCHEMA_FAILED', 'SpiceJet departure date is unavailable')
    await cell.click(timeout=5000)
    label = await control(browser.page, 'departure-date-dropdown-label-test-id').inner_text()
    if day.strftime('%a, %d %b %Y') not in label:
        raise DomainError('EXTRACTION_SCHEMA_FAILED', 'SpiceJet selected another departure date')
    await checkpoint('Verifying passengers')
    done = control(browser.page, 'home-page-travellers-done-cta')
    if not await done.is_visible():
        await control(browser.page, 'home-page-travellers').click(timeout=5000)
    for kind, expected in [('Adult', '1'), ('Children', '0'), ('Infant', '0')]:
        await guard()
        plus = control(browser.page, kind + '-testID-plus-one-cta')
        count = await plus.evaluate('e=>e.parentElement.innerText.trim()')
        if count != expected:
            raise DomainError('EXTRACTION_SCHEMA_FAILED', 'SpiceJet passenger counts do not match')
    await done.click(timeout=5000)
    for field, code in [('origin', request.origin), ('destination', request.destination)]:
        value = await control(browser.page, 'to-testID-' + field).locator('input').input_value()
        if not value.endswith(f'({code})'):
            raise DomainError('EXTRACTION_SCHEMA_FAILED', 'SpiceJet route changed before search')
    await checkpoint('Searching flights')
    await guard()
    await control(browser.page, 'home-page-flight-cta').click(timeout=5000)
    for _ in range(90):
        await guard()
        if await browser.visible(eura.manifest.result_ready):
            eura._verified_search = (id(browser), request.model_dump(mode='json'))
            if not await context_matches(eura, browser, request):
                raise DomainError('EXTRACTION_SCHEMA_FAILED', 'SpiceJet returned another search context')
            return
        await asyncio.sleep(.5)
    raise DomainError('EXTRACTION_STRUCTURE_FAILED', 'SpiceJet flight results did not load')


async def context_matches(eura, browser, request):
    from urllib.parse import parse_qs, urlsplit
    if getattr(eura, '_verified_search', None) != (id(browser), request.model_dump(mode='json')):
        return False
    url = urlsplit(browser.page.url)
    expected = {'from': request.origin, 'to': request.destination, 'tripType': '1',
                'departure': request.departure_date.isoformat(), 'adult': '1', 'child': '0',
                'infant': '0', 'srCitizen': '0', 'currency': 'INR'}
    query = parse_qs(url.query)
    if url.path != '/search' or any(query.get(k) != [v] for k, v in expected.items()):
        return False
    selected = browser.page.locator('[data-testid="lowfare-calendar-dateId"].r-ov7bg')
    return (await selected.count() == 1 and await selected.is_visible()
            and (await selected.inner_text()).startswith(request.departure_date.strftime('%a, %d %b') + '\n'))


def parse_card(html, headers, request, version):
    import re

    from scrapling.parser import Selector

    from udaan.canonical import FareDetails
    from udaan.contracts import Fare
    from udaan.eura import money
    from udaan.family_extraction import detail_value, read_one

    doc = Selector(html)
    airports = [x.get_all_text().strip() for x in doc.css('div.r-homxoj.r-ubezar')]
    if airports != [request.origin, request.destination]:
        return []
    number = read_one(doc, '#aircraft-no', required=True)
    if not re.fullmatch(r'SG\s*\d{1,5}(?:\s*/\s*SG\s*\d{1,5})*', number):
        raise DomainError('EXTRACTION_SCHEMA_FAILED', 'SpiceJet flight identity is ambiguous')
    duration = read_one(doc, '.r-cqee49.r-ou255f')
    stops = read_one(doc, '.r-v258g')
    rows = []
    for family in doc.css('#fare-bundle-val > div'):
        radios = family.css('[data-testid*="-flight-select-radio-button-"]')
        if len(radios) != 1:
            raise DomainError('EXTRACTION_STRUCTURE_FAILED', 'SpiceJet fare-family control is missing or ambiguous')
        key = re.fullmatch(r'(.+)-flight-select-radio-button-\d+', radios[0].attrib['data-testid'])
        if not key or key[1] not in headers:
            raise DomainError('EXTRACTION_SCHEMA_FAILED', 'SpiceJet fare family has no displayed column heading')
        clickable = radios[0].parent
        if clickable.attrib.get('data-focusable') != 'true' or clickable.attrib.get('aria-disabled') == 'true':
            continue
        price = read_one(family, '.r-1i10wst.r-1kfrs79', required=True)
        if '₹' not in price:
            raise DomainError('EXTRACTION_SCHEMA_FAILED', 'SpiceJet fare currency changed')
        rows.append(Fare(origin=request.origin, destination=request.destination,
            departure_date=request.departure_date, booking_window=request.booking_window,
            airline='SpiceJet', flight_number=number, cabin=None, total_fare=money(price), currency='INR',
            details=FareDetails(fare_family=headers[key[1]], availability_status='AVAILABLE', trip_type='ONE_WAY',
                duration_minutes=detail_value('duration_minutes', duration) if duration else None,
                stops=0 if stops == 'Direct' else detail_value('stops', stops) if stops else None,
                direct_or_connecting='DIRECT' if stops == 'Direct' else None,
                extraction_method='EURA_RECIPE', scrape_status='PARTIAL'),
            extraction_metadata={'selector_version': version, 'fare_basis': 'displayed_fare_family',
                'passengers': {'adults': 1, 'children': 0, 'infants': 0}}))
    if not rows:
        raise DomainError('EXTRACTION_SCHEMA_FAILED', 'SpiceJet card exposes no available fare families')
    return rows


def parse_summary(text, fare):
    import re

    from udaan.eura import money
    normalized = ' '.join(text.split())
    if not re.search(r'Flight\s+' + re.escape(fare.flight_number) + r'\b', normalized):
        raise DomainError('EXTRACTION_SCHEMA_FAILED', 'SpiceJet summary belongs to another flight')
    if fare.departure_date.strftime('%a, %d %b %Y') not in normalized:
        raise DomainError('EXTRACTION_SCHEMA_FAILED', 'SpiceJet summary belongs to another date')
    pattern = (r'1 Adult\s*\(' + re.escape(fare.details.fare_family) + r' Fare\)\s*'
               r'₹\s*([\d,.]+)\s*Fees & Taxes\s*₹\s*([\d,.]+)')
    matches = re.findall(pattern, normalized)
    if len(matches) != 1:
        raise DomainError('EXTRACTION_SCHEMA_FAILED', 'SpiceJet adult fare and combined charges are ambiguous')
    base, combined = [money(value, allow_zero=True) for value in matches[0]]
    if base + combined != fare.total_fare:
        raise DomainError('EXTRACTION_SCHEMA_FAILED', 'SpiceJet summary does not reconcile with the selected family price')
    fare.base_fare = base
    # A combined Fees & Taxes amount cannot be split into its unknown components.
    fare.taxes = fare.fees = fare.details.total_payable = None
    fare.extraction_metadata['displayed_combined_taxes_and_fees'] = str(combined)
    fare.quality_flags.append('COMBINED_TAXES_AND_FEES')
    if 'convenience fee may apply' in normalized.lower():
        fare.quality_flags.append('CONVENIENCE_FEE_NOT_YET_QUOTED')
    return fare


async def extract(eura, browser, request):
    guard = eura._guard
    await guard()
    summary_toggle = control(browser.page, 'toggle_onward_farebreakup')
    details_toggle = control(browser.page, 'toogle_fare_breakup')
    if await summary_toggle.is_visible():
        await browser.page.mouse.click(5, 5)
        await summary_toggle.wait_for(state='hidden', timeout=5000)
    headings = browser.page.locator('[data-testid^="searchPage-sortingOption-"][data-testid$="-child"]')
    headers = {}
    for item in await headings.evaluate_all("es=>es.map(e=>({id:e.getAttribute('data-testid'),text:e.innerText.trim()}))"):
        key = item['id'].removeprefix('searchPage-sortingOption-').removesuffix('-child')
        if key == item['text'].lower():
            headers[key] = item['text']
    bundles = browser.page.locator(eura.manifest.selectors['cards'])
    count = await bundles.count()
    if not 1 <= count <= 1000:
        raise DomainError('EXTRACTION_STRUCTURE_FAILED', 'SpiceJet flight card count is invalid')
    rows = []
    eura._family_matched = 0
    for index in range(count):
        if getattr(eura, 'validation_card_limit', None) and eura._family_matched >= eura.validation_card_limit:
            break
        await eura._checkpoint(f'Reading flight {index + 1}/{count}')
        await guard()
        card = bundles.nth(index).locator('..')
        fares = parse_card(await card.inner_html(), headers, request, eura.manifest.version)
        for fare in fares:
            await guard()
            radio = card.locator(f'[data-testid^="{fare.details.fare_family.lower()}-flight-select-radio-button-"]')
            await radio.click(timeout=5000)
            await asyncio.sleep(eura.manifest.families.minimum_interval_ms / 1000)
            await guard()
            selected = card.locator('#selected-onward-container')
            from udaan.eura import money
            if await selected.count() != 1 or money(await selected.inner_text()) != fare.total_fare:
                raise DomainError('EXTRACTION_SCHEMA_FAILED', 'SpiceJet did not retain the selected fare')
            await details_toggle.click(timeout=5000)
            await summary_toggle.wait_for(state='visible', timeout=5000)
            summary = summary_toggle.locator('../../..')
            expected = summary.get_by_text(f'1 Adult  ({fare.details.fare_family} Fare)', exact=True)
            if not await expected.is_visible():
                await summary_toggle.click(timeout=5000)
            await guard()
            fare = parse_summary(await summary.inner_text(), fare)
            # The summary dismisses on an ordinary click in the inspected page margin.
            await browser.page.mouse.click(5, 5)
            await summary_toggle.wait_for(state='hidden', timeout=5000)
            rows.append(fare)
        if fares:
            eura._family_matched += 1
    await guard()
    if not rows or not await context_matches(eura, browser, request):
        raise DomainError('EXTRACTION_SCHEMA_FAILED', 'SpiceJet fares or final search context could not be verified')
    if request.fare_scope == 'Selected Cabin':
        raise DomainError('CABIN_UNAVAILABLE', 'SpiceJet does not label the cabin for these fare families; use All Available')
    return rows
