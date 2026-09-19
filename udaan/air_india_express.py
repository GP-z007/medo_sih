"""Inspected public AIX controls; no model, credentials, or booking submission."""
import asyncio
import re
from decimal import Decimal
from urllib.parse import urlsplit

from udaan.contracts import DomainError


async def search(eura, browser, request, guard, checkpoint):
    if (request.adults, request.children, request.infants) != (1, 0, 0):
        raise DomainError('UNSUPPORTED_PASSENGERS', 'AIX currently validates one adult')
    async def visible(css):
        for _ in range(20):
            await guard()
            control = await browser.visible(css)
            if control is not None:
                return control
            await asyncio.sleep(.25)
        raise DomainError('EXTRACTION_STRUCTURE_FAILED', 'An AIX booking control is unavailable')
    await checkpoint('Selecting airports')
    await (await visible('#new-flight-search-origin-field-text')).click()
    for field, code in [('origin', request.origin), ('destination', request.destination)]:
        control = await visible('#basic-url-' + field)
        await control.fill(code)
        await asyncio.sleep(.7)
        await guard()
        options = browser.page.get_by_text(code, exact=True)
        for _ in range(30):
            await guard()
            matches = [options.nth(i) for i in range(await options.count()) if await options.nth(i).is_visible()]
            if len(matches) == 1:
                await matches[0].click(timeout=5000)
                break
            await asyncio.sleep(.3)
        else:
            raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Exact airport option is missing or ambiguous')
        await asyncio.sleep(.7)
    await checkpoint('Selecting departure date')
    await (await visible('#start-date-input-button')).click()
    prefix = request.departure_date.strftime('%-d %B %Y') + ' '
    for _ in range(13):
        await guard()
        cell = await browser.visible(f'.new-day[aria-label^="{prefix}"]')
        if cell is not None:
            if 'disabled' in (await cell.get_attribute('class') or '').split():
                raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Requested departure date is disabled')
            await cell.click(timeout=5000)
            break
        await (await visible('.flipper-button.right:not(.disabled)')).click()
    else:
        raise DomainError('EXTRACTION_STRUCTURE_FAILED', 'Requested calendar month is unavailable')
    await (await visible('#calendar-confirm')).click()
    await (await visible('#flight-search-passenger-count')).click()
    for name, count in [('adult', '1'), ('child', '0'), ('infant', '0'), ('extra', '0'), ('senior', '0'), ('student', '0'), ('minor', '0')]:
        node = await visible('.new_passenger_count_text.' + name + '-count')
        if (await node.inner_text()).strip() != count:
            raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Passenger or special-fare selection does not match the job')
    if await browser.page.locator('.passenger-checkbox input:checked').count():
        raise DomainError('EXTRACTION_SCHEMA_FAILED', 'A special eligibility fare is selected')
    await guard()
    await browser.page.get_by_text('Done', exact=True).click(timeout=5000)
    await checkpoint('Searching flights')
    await (await visible('.new-search-flight-button-container')).click()
    for _ in range(120):
        await guard()
        if await browser.visible(eura.manifest.result_ready) is not None:
            eura._verified_search = (id(browser), request.model_dump(mode='json'))
            if not await context_matches(eura, browser, request):
                raise DomainError('EXTRACTION_SCHEMA_FAILED', 'AIX returned another search context')
            return
        await asyncio.sleep(.5)
    raise DomainError('EXTRACTION_STRUCTURE_FAILED', 'AIX did not display verified flight results')


async def context_matches(eura, browser, request):
    if getattr(eura, '_verified_search', None) != (id(browser), request.model_dump(mode='json')):
        return False
    url = urlsplit(browser.page.url)
    parts = url.query.split('/')
    expected = ['', request.origin, request.destination, request.departure_date.isoformat(), 'N', '1', '0', '0', '0', '0', '0', '0', 'O', 'N', 'INR', 'ST', '0']
    if url.path != '/flight-availability' or parts != expected:
        return False
    text = await browser.page.locator('body').inner_text()
    return (f'{request.origin} - {request.destination}' in text and request.departure_date.strftime('%d %b %Y') in text)


def fare_details(family):
    result = {}
    for item in family.css('.addon-inclusion-text'):
        value = ' '.join(item.get_all_text().split())
        if 'Cabin Baggage' in value:
            result['cabin_baggage'] = value
        elif 'Check-In Baggage' in value:
            result['checkin_baggage'] = value
        elif value.startswith('Cancellation Fee'):
            result['cancellation_fee'] = value
        elif value.startswith('Date Change Fee'):
            result['change_fee'] = value
        elif value == 'Zero Convenience Fee':
            result['convenience_fee'] = Decimal('0')
    return result


def parse_breakdown(html, fare):
    from scrapling.parser import Selector

    from udaan.eura import money
    from udaan.family_extraction import read_one
    doc = Selector(html)
    amounts = {}
    for row in doc.css('.fare-breakup-details-row'):
        label, price = read_one(row, '.info'), read_one(row, '.price')
        if label and price:
            amounts[label] = amounts.get(label, Decimal('0')) + money(price, allow_zero=True)
    if amounts.get('Balance Payable') != fare.total_fare or amounts.get('Total Fare') != fare.total_fare:
        raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Selected fare and payable summary disagree')
    # The UI separates the one-adult fare from a fully expanded tax/fee list.
    base, combined, convenience = (amounts.get(k) for k in ['Adult', 'Taxes and Fees', 'Convenience Fee'])
    if any(x is None for x in [base, combined, convenience]) or base + combined + convenience != fare.total_fare:
        raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Fare summary components do not reconcile')
    tax_names = {'India CGST', 'India SGST', 'India IGST'}
    fee_names = {'Fuel Surcharge (YQ)', 'Common Use Terminal Equipment Fee', 'Regional Connectivity Scheme Fee', 'User Development Fee', 'Aviation Security Fee'}
    components = {k: v for k, v in amounts.items() if k not in {'Adult', 'Taxes and Fees', 'Convenience Fee', 'Total Fare', 'Balance Payable'}}
    if set(components) - tax_names - fee_names or sum(components.values()) != combined:
        raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Tax and fee breakdown is incomplete or has unclassified charges')
    fare.base_fare = base
    fare.taxes = sum((v for k, v in components.items() if k in tax_names), Decimal('0'))
    # total_fees includes named carrier surcharges; detailed fields remain separately queryable.
    fare.fees = sum((v for k, v in components.items() if k in fee_names), convenience)
    details = fare.details.model_dump()
    details.update(total_payable=amounts['Balance Payable'], GST=fare.taxes, convenience_fee=convenience)
    for label, key in [('Fuel Surcharge (YQ)', 'fuel_surcharge'), ('User Development Fee', 'UDF'), ('Aviation Security Fee', 'ASF')]:
        if label in amounts:
            details[key] = amounts[label]
    from udaan.canonical import FareDetails
    fare.details = FareDetails.model_validate(details)
    return fare


async def enrich_families(eura, browser, card, request, families, guard):
    for fare in families:
        await guard()
        # Image alt is the public fare brand, unlike the site's stale radio aria labels.
        panel = card.locator('.xpress-wrapper').filter(has=browser.page.locator(f'.voucher-name-and-price img[alt="{fare.details.fare_family}"]'))
        if await panel.count() != 1:
            # The Flex image contains trailing whitespace; compare normalized public names.
            matches = []
            for i in range(await card.locator('.xpress-wrapper').count()):
                item = card.locator('.xpress-wrapper').nth(i)
                if (await item.locator('.voucher-name-and-price img').get_attribute('alt') or '').strip() == fare.details.fare_family:
                    matches.append(item)
            if len(matches) != 1:
                raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Fare brand selection is ambiguous')
            panel = matches[0]
        radio = panel.locator('input[type=radio]')
        if not await radio.is_checked():
            await panel.locator('.voucher-name-and-price').click(timeout=5000)
        await asyncio.sleep(3)
        await guard()
        if not await radio.is_checked():
            raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Fare selection was not retained')
        await browser.page.locator('.fare-summary-footer').click(timeout=5000)
        sidebar = browser.page.locator('aside.flight-availability-sidebar.open')
        await sidebar.wait_for(state='visible', timeout=5000)
        await guard()
        text = await sidebar.inner_text()
        numbers = re.findall(r'IX\s*\d+', fare.flight_number)
        if not all(number in text for number in numbers) or request.departure_date.strftime('%d %b %Y') not in text:
            raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Fare summary belongs to another itinerary')
        await sidebar.locator('.fare-breakup-details-row > .info').filter(has_text=re.compile(r'^Taxes and Fees$')).click(timeout=5000)
        await guard()
        parse_breakdown(await sidebar.inner_html(), fare)
        await sidebar.locator('[aria-label="close-sidebar"]').click(timeout=5000)
        await browser.page.locator('aside.flight-availability-sidebar').wait_for(state='hidden', timeout=10000)
        await guard()
    return families
