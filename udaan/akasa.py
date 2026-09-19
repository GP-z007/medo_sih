"""Akasa's inspected public booking form. No model calls or security interaction."""
import asyncio

from udaan.contracts import DomainError


async def select_airport(browser, field, value, guard):
    control = browser.page.locator(f'input[name="{field}"]')
    options = browser.page.locator(f'#destinations li[id="{value}"]')
    # The server-rendered input accepts text before React attaches its handlers.
    # Re-enter through the visible control if no suggestion list appears.
    for _ in range(3):
        await guard()
        await control.press('Tab')
        await control.click(timeout=5000)
        await control.fill('')
        await control.fill(value)
        for _ in range(10):
            await guard()
            matches = [options.nth(i) for i in range(await options.count()) if await options.nth(i).is_visible()]
            if len(matches) == 1:
                await matches[0].click(timeout=5000)
                if not (await control.input_value()).endswith(f'({value})'):
                    raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Selected airport does not match the request')
                return
            if len(matches) > 1:
                raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Requested visible airport suggestion is ambiguous')
            await asyncio.sleep(.3)
    raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Requested visible airport suggestion is missing')


async def search(eura, browser, request, guard, checkpoint):
    if (request.adults, request.children, request.infants) != (1, 0, 0):
        raise DomainError('UNSUPPORTED_PASSENGERS', 'The current Akasa search is validated for one adult')
    security_guard = guard
    async def guard():
        await security_guard()
        consent = browser.page.get_by_role('button', name='Accept cookies', exact=True)
        if await consent.is_visible():
            await consent.click(timeout=5000)
            await consent.wait_for(state='hidden', timeout=5000)
            await security_guard()
    await guard()
    for field, value in [('From', request.origin), ('To', request.destination)]:
        await checkpoint('Selecting ' + field.lower() + ' airport')
        await guard()
        await select_airport(browser, field, value, guard)
    await checkpoint('Selecting departure date')
    await asyncio.sleep(.7)
    await guard()
    if not await browser.page.locator('.react-datepicker').is_visible():
        await browser.page.get_by_role('button', name='Select departure date', exact=True).click(timeout=5000)
    day = request.departure_date
    suffix = 'th' if 10 <= day.day % 100 <= 20 else {1:'st', 2:'nd', 3:'rd'}.get(day.day % 10, 'th')
    label = f"Choose {day.strftime('%A, %B')} {day.day}{suffix}, {day.year}"
    for _ in range(13):
        await guard()
        cell = browser.page.get_by_role('gridcell', name=label, exact=True)
        if await cell.count() == 1 and await cell.is_visible():
            if await cell.get_attribute('aria-disabled') == 'true':
                raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Departure date is not available')
            await cell.click(timeout=5000)
            break
        await browser.page.get_by_role('button', name='Next Month', exact=True).click(timeout=5000)
    else:
        raise DomainError('EXTRACTION_STRUCTURE_FAILED', 'Requested calendar date is unavailable')
    await guard()
    passenger = browser.page.locator('button[name="SelectPassengers"]')
    if await passenger.count() != 1 or ' '.join((await passenger.inner_text()).split()) != 'Passenger(s) 1 Adult':
        raise DomainError('EXTRACTION_SCHEMA_FAILED', 'One-adult passenger context could not be verified')
    await passenger.click(timeout=5000)
    for label, expected in [('Adult(s) Plus', '1'), ('Children Plus', '0'), ('Infant(s) Plus', '0')]:
        await guard()
        plus = browser.page.get_by_role('button', name=label, exact=True)
        count = await plus.evaluate("e => e.parentElement.parentElement.querySelector('span').textContent.trim()")
        if count != expected:
            raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Passenger counts do not match the job')
    await browser.page.get_by_role('button', name='Done', exact=True).click(timeout=5000)
    await guard()
    await checkpoint('Searching flights')
    await browser.page.get_by_role('button', name='Search Flights', exact=True).click(timeout=5000)
    for _ in range(60):
        await guard()
        if await browser.visible(eura.manifest.result_ready):
            eura._verified_search = (id(browser), request.model_dump(mode='json'))
            eura._verified_header = (await browser.page.locator('main').inner_text()).split('Modify', 1)[0]
            return
        await asyncio.sleep(.5)
    raise DomainError('EXTRACTION_STRUCTURE_FAILED', 'Flight results did not become available')


def fare_details(family):
    result = {}
    for node in family.css('.border-t p'):
        text = ' '.join(node.get_all_text().split())
        if text.startswith('Check-in Baggage'):
            result['checkin_baggage'] = text
        if text == 'Complimentary Meal':
            result['meal_included'] = True
        # Labels without the actual allowance or fee are not numeric fare details.
    return result


async def context_matches(eura, browser, request):
    if getattr(eura, '_verified_search', None) != (id(browser), request.model_dump(mode='json')):
        return False
    if '/flight-search' != __import__('urllib.parse', fromlist=['urlsplit']).urlsplit(browser.page.url).path:
        return False
    text = await browser.page.locator('main').inner_text()
    header = text.split('Modify', 1)[0]
    expected_date = request.departure_date.strftime('%a, %d %b')
    return (header == getattr(eura, '_verified_header', None) and expected_date in header
            and '1 Passenger(s)' in header and 'One Way:' in header)


def parse_breakdown(html, fare):
    from decimal import Decimal

    from scrapling.parser import Selector

    from udaan.canonical import FareDetails
    from udaan.eura import money
    from udaan.family_extraction import read_one
    doc=Selector(html)
    amounts={}
    for row in doc.css('div'):
        cells=row.xpath('./span')
        if len(cells)==2:
            label=' '.join(cells[0].get_all_text().split())
            price=cells[1].get_all_text().strip()
            if price.startswith('₹'):
                if label in amounts:
                    raise DomainError('EXTRACTION_SCHEMA_FAILED','Fare breakdown repeats a charge label')
                amounts[label]=money(price,allow_zero=True)
    total=money(read_one(doc,'.dialogFooterContainer .text_type_amount_h1:last-child',required=True))
    if total!=fare.total_fare or sum(amounts.values(),Decimal('0'))!=total:
        raise DomainError('EXTRACTION_SCHEMA_FAILED','Akasa selected fare and breakdown do not reconcile')
    required={'1 X Adult','Tax'}
    fee_names={'CUTE Fees','RCS & Admin Charge','Aviation Security Fee','User Development Fee - Departure','User Development Fee - Arrival'}
    if not required <= amounts.keys() or amounts.keys() - required - fee_names:
        raise DomainError('EXTRACTION_SCHEMA_FAILED','Akasa charge labels changed')
    fare.base_fare, fare.taxes=amounts['1 X Adult'],amounts['Tax']
    details=fare.details.model_dump()
    details.update(availability_status='AVAILABLE')
    for label,key in [('CUTE Fees','airport_fee'),('RCS & Admin Charge','other_fee'),('Aviation Security Fee','ASF')]:
        if label in amounts:
            details[key]=amounts[label]
    if all(k in amounts for k in ['User Development Fee - Departure','User Development Fee - Arrival']):
        details['UDF']=amounts['User Development Fee - Departure']+amounts['User Development Fee - Arrival']
    # This page explicitly says another convenience charge is added before checkout.
    # Its "You Pay" is therefore not the final amount actually payable.
    deferred='convenience fee per passenger, per segment will be added before checkout' in doc.get_all_text().lower()
    if deferred:
        details.update(total_payable=None,convenience_fee=None,scrape_status='PARTIAL')
        fare.fees=None
        if 'CONVENIENCE_FEE_NOT_YET_QUOTED' not in fare.quality_flags:
            fare.quality_flags.append('CONVENIENCE_FEE_NOT_YET_QUOTED')
    else:
        # Absence of the notice does not prove all checkout charges are included.
        details['total_payable']=None
        fare.fees=None
    fare.details=FareDetails.model_validate(details)
    return fare


async def enrich_families(eura,browser,card,request,families,guard):
    for fare in families:
        await guard()
        panel=card.locator('.pt-2.pb-1').filter(has=browser.page.get_by_text(fare.details.fare_family,exact=True))
        if await panel.count()!=1:
            raise DomainError('EXTRACTION_SCHEMA_FAILED','Akasa fare-family selector is ambiguous')
        await panel.locator('.text-typography-highlight').click(timeout=5000)
        await asyncio.sleep(1)
        await guard()
        if not await panel.evaluate("e=>e.parentElement.classList.contains('border-typography-highlight')"):
            raise DomainError('EXTRACTION_SCHEMA_FAILED','Akasa fare selection was not retained')
        await browser.page.get_by_role('button',name='View summary',exact=True).click(timeout=5000)
        dialog=browser.page.get_by_role('dialog')
        await dialog.wait_for(state='visible',timeout=5000)
        await guard()
        if not await eura.context_matches(browser,request):
            raise DomainError('EXTRACTION_SCHEMA_FAILED','Search context changed while reading the fare breakdown')
        parse_breakdown(await dialog.inner_html(),fare)
        await dialog.locator('#closePopUp').click(timeout=5000)
        await dialog.wait_for(state='hidden',timeout=5000)
        await guard()
    return families
