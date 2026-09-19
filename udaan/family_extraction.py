"""Canonical flight × fare-family extraction from the existing headed page."""
import asyncio
import re
from datetime import datetime

from pydantic import ValidationError

from udaan.canonical import BOOLEAN_DETAILS, INTEGER_DETAILS, MONEY_DETAILS, FareDetails
from udaan.contracts import DomainError, Fare
from udaan.eura import money


def read_one(root, css, *, required=False):
    nodes = root.css(css) if css else []
    texts = [n.get_all_text().strip() for n in nodes]
    texts = [x for x in texts if x]
    if len(texts) > 1:
        raise DomainError('EXTRACTION_SCHEMA_FAILED', 'A flight or fare-family field is ambiguous')
    if not texts and required:
        raise DomainError('EXTRACTION_SCHEMA_FAILED', 'A required flight or fare-family field is missing')
    return texts[0] if texts else None


def detail_value(name, text):
    if name in MONEY_DETAILS | {'base_fare', 'taxes', 'fees'}:
        return money(text, allow_zero=True)
    if name == "duration_minutes":
        match = re.fullmatch(r"(?:(\d+)h)?\s*(?:(\d+)m)?", text)
        return int(match.group(1) or 0) * 60 + int(match.group(2) or 0) if match and any(match.groups()) else None
    if name == "stops":
        if text.casefold() in {"non-stop", "non stop", "nonstop"}:
            return 0
        match = re.fullmatch(r"(\d+)\s+stops?", text, re.I)
        return int(match.group(1)) if match else None
    if name in INTEGER_DETAILS:
        if not re.fullmatch(r'\d+', text):
            return None
        return int(text)
    if name in BOOLEAN_DETAILS:
        token = text.strip().casefold()
        if token in {'yes','included','complimentary','refundable','true'}:
            return True
        if token in {'no','not included','non-refundable','non refundable','false'}:
            return False
        return None
    if name in {'departure_at','arrival_at'}:
        try:
            value = datetime.fromisoformat(text.replace('Z','+00:00'))
            return value if value.tzinfo else None
        except ValueError:
            return None
    return text


def airport_code(text):
    """Accept displayed IATA with an explicit terminal suffix; never nearby cities."""
    match = re.fullmatch(r"([A-Z]{3})(?:,\s*T[0-9A-Z]+)?", (text or "").strip())
    return match.group(1) if match else None


def actual_route(route):
    return tuple(airport_code(route.get('alternate_' + field) or route.get(field)) for field in ('origin','destination'))


def parse_family_card(manifest, html, request):
    from scrapling.parser import Selector
    card = Selector(html)
    f = manifest.families
    route = {name: read_one(card, css) for name, css in manifest.route_selectors.items()}
    actual = actual_route(route)
    if actual != (request.origin, request.destination):
        return []
    if manifest.flight_number_many:
        numbers = [' '.join(part.split()) for n in card.css(manifest.selectors['flight_number']) for part in n.get_all_text().split(',')]
        if not 1 <= len(numbers) <= 21 or any(not re.fullmatch(r'[A-Z0-9]{2}\s*\d{1,5}[A-Z]?', n) for n in numbers):
            raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Displayed itinerary flight numbers are invalid')
        flight = ' / '.join(numbers)
    else:
        flight = re.sub(r'\s+', ' ', read_one(card, manifest.selectors.get('flight_number'), required=True)).strip()
    airline = read_one(card, manifest.selectors.get('airline'))
    if airline:
        airline = re.sub(r'^Operated by\s+', '', airline, flags=re.I)
    else:
        prefix = re.match(r'^([A-Z0-9]{2})\s*\d', flight)
        airline = manifest.carrier_names.get(prefix.group(1)) if prefix else None
    if not airline:
        raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Displayed flight operator could not be established')
    flight_details = {}
    for key, css in f.flight_details.items():
        text = read_one(card, css)
        if text is not None:
            flight_details[key] = detail_value(key, text)
    rows = []
    for family in card.css(f.container):
        available = None
        if f.available_control:
            controls = family.css(f.available_control)
            if len(controls) != 1:
                raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Fare availability control is ambiguous')
            if 'disabled' in controls[0].attrib:
                continue
            available = 'AVAILABLE'
        if f.name_attribute:
            names = family.css(f.name)
            if len(names) != 1 or not names[0].attrib.get(f.name_attribute, '').strip():
                raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Displayed fare brand is ambiguous')
            name = names[0].attrib[f.name_attribute].strip()
        else:
            name = read_one(family, f.name, required=True)
        cabin = None
        if f.cabin:
            cabin_root = card if f.cabin_scope == 'card' else family
            cabin_nodes = cabin_root.css(f.cabin)
            if f.cabin_attribute:
                if len(cabin_nodes) != 1:
                    raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Selected fare cabin is ambiguous')
                cabin_label = cabin_nodes[0].attrib.get(f.cabin_attribute)
            else:
                cabin_label = read_one(cabin_root, f.cabin, required=True)
            cabin = f.cabin_names.get(cabin_label)
            if not cabin:
                raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Displayed fare cabin has no validated mapping')
        price = read_one(family, f.price, required=True)
        currency_text = read_one(family, f.currency, required=True)
        codes = set(re.findall(r'\b(?:INR|USD|EUR|GBP|AED|SGD|AUD|CAD)\b', currency_text.upper()))
        if '₹' in currency_text:
            codes.add('INR')
        if len(codes) != 1:
            raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Displayed fare currency is ambiguous')
        details = {**flight_details, 'availability_status': available or 'UNKNOWN', 'fare_family': name, 'trip_type':'ONE_WAY', 'extraction_method':'EURA_RECIPE', 'scrape_status':'SUCCESS' if cabin else 'PARTIAL'}
        for root, rows_css, label_css, value_css, labels in [
                (family, f.detail_rows, f.detail_label, f.detail_value, f.detail_labels),
                (card, f.shared_detail_rows, f.shared_detail_label, f.shared_detail_value, f.shared_detail_labels)]:
            for row in root.css(rows_css) if rows_css else []:
                label = read_one(row, label_css)
                value = read_one(row, value_css)
                if label in labels and value is not None:
                    details[labels[label]] = detail_value(labels[label], value)
        for key, css in f.details.items():
            value = read_one(family, css)
            if value is not None:
                details[key] = detail_value(key, value)
        if manifest.navigation == "indigo_booking":
            from udaan.indigo import fare_details
            details.update(fare_details(family))
        if manifest.navigation == 'akasa_booking':
            from udaan.akasa import fare_details
            details.update(fare_details(family))
        if manifest.navigation == 'aix_booking':
            from udaan.air_india_express import fare_details
            details.update(fare_details(family))
        data = {key: details.pop(key) for key in ['base_fare','taxes','fees','departure_at','arrival_at'] if key in details}
        try:
            rows.append(Fare(origin=request.origin, destination=request.destination, departure_date=request.departure_date,
                booking_window=request.booking_window, cabin=cabin, airline=airline, flight_number=flight,
                total_fare=money(price), currency=codes.pop(), details=FareDetails.model_validate(details), **data,
                extraction_metadata={'selector_version':manifest.version,'fare_basis':'displayed_fare_family',
                                     'passengers':{'adults':request.adults,'children':request.children,'infants':request.infants}}))
        except ValidationError as exc:
            raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Real fare-family details failed canonical validation') from exc
    return rows


async def extract_families(eura, browser, request):
    manifest = eura.manifest
    guard = eura._guard
    cards = browser.page.locator(manifest.selectors['cards'])
    count = await cards.count()
    if not 1 <= count <= 1000:
        raise DomainError('EXTRACTION_STRUCTURE_FAILED', 'Result flight-card count is missing or exceeds the validated bound')
    # Result ordering must still identify the same cards after an operator pause.
    identity_fields = {'flight': manifest.selectors['flight_number'], **manifest.route_selectors}
    identities = tuple(await cards.evaluate_all("(es, fields) => es.map(e => JSON.stringify(Object.fromEntries(Object.entries(fields).map(([key, css]) => [key, [...e.querySelectorAll(css)].map(n => n.textContent.trim())]))))", identity_fields))
    cache_key = (id(browser), request.origin, request.destination, request.departure_date, identities)
    if getattr(eura, "_family_cache_key", None) != cache_key:
        eura._family_cache_key, eura._family_rows, eura._family_next = cache_key, [], 0
        eura._family_pending, eura._family_itineraries = {}, {}
        eura._family_matched = 0
    result = eura._family_rows
    for index in range(eura._family_next, count):
        if getattr(eura, "validation_card_limit", None) and eura._family_matched >= eura.validation_card_limit:
            break
        await eura._checkpoint(f"Reading flight {index + 1}/{count}")
        await guard()
        card = cards.nth(index)
        from scrapling.parser import Selector
        document = Selector(await card.inner_html())
        route = {key: read_one(document, css) for key, css in manifest.route_selectors.items()}
        if actual_route(route) != (request.origin, request.destination):
            continue
        itinerary = eura._family_itineraries.get(index)
        if manifest.flight_details and itinerary is None:
            from udaan.flight_details import inspect_flight_details
            itinerary = await inspect_flight_details(eura, browser, card, request, guard)
            eura._family_itineraries[index] = itinerary
        pending = eura._family_pending.setdefault(index, {"next":0,"rows":[]})
        families = pending["rows"]
        if manifest.families.reveal:
            controls = card.locator(manifest.families.reveal)
            controls_count = await controls.count()
            if not 1 <= controls_count <= 12:
                raise DomainError('EXTRACTION_STRUCTURE_FAILED', 'Fare-family reveal controls changed')
            for control_index in range(pending["next"], controls_count):
                await guard()
                control = controls.nth(control_index)
                if not await control.is_visible() or not await control.is_enabled():
                    continue
                if manifest.navigation in {'akasa_booking', 'aix_booking'} and await card.locator(manifest.families.container).count():
                    already_open = True
                else:
                    already_open = manifest.families.selected and await control.evaluate("(e, css) => e.matches(css)", manifest.families.selected)
                if not already_open:
                    await control.click()
                await guard()
                await asyncio.sleep(max(manifest.stability_ms, manifest.families.minimum_interval_ms) / 1000)
                await guard()
                families.extend(parse_family_card(manifest, await card.inner_html(), request))
                pending["next"] = control_index + 1
        else:
            families = parse_family_card(manifest, await card.inner_html(), request)
        if not families:
            # Nearby airports are excluded explicitly; an exact-route missing family is a failure.
            from scrapling.parser import Selector
            document = Selector(await card.inner_html())
            route = {key: read_one(document, css) for key, css in manifest.route_selectors.items()}
            if actual_route(route) == (request.origin,request.destination):
                raise DomainError('EXTRACTION_SCHEMA_FAILED', 'An exact-route flight has no verified fare families')
        if itinerary:
            for fare in families:
                fare.flight_number = itinerary["flight_number"]
                fare.departure_at, fare.arrival_at = itinerary["departure_at"], itinerary["arrival_at"]
                fare.details = FareDetails.model_validate({**fare.details.model_dump(), **itinerary["details"]})
        if manifest.navigation in {'aix_booking', 'akasa_booking'}:
            if manifest.navigation == 'aix_booking':
                from udaan.air_india_express import enrich_families
            else:
                from udaan.akasa import enrich_families
            families = await enrich_families(eura, browser, card, request, families, guard)
        result.extend(families)
        eura._family_matched += 1
        eura._family_next = index + 1
        eura._family_pending.pop(index, None)
    await guard()
    if not result:
        raise DomainError('EXTRACTION_SCHEMA_FAILED', 'No exact-route fare families could be validated')
    if not await eura.context_matches(browser, request):
        raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Search context changed while reading fare families')
    if request.fare_scope == 'Selected Cabin':
        result = [fare for fare in result if fare.cabin == request.cabin]
        if not result:
            raise DomainError('CABIN_UNAVAILABLE', 'The selected cabin has no verified fare options')
    return result
