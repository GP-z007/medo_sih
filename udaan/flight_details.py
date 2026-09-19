"""Displayed itinerary identity and dates, enriched with airport timezone metadata."""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from udaan.contracts import DomainError
from udaan.db import AirportReference, session
from udaan.family_extraction import read_one


def displayed_date(value, preferred):
    """Accept only the site's known unambiguous displayed date variants."""
    value = " ".join(value.replace("\u00a0", " ").split())
    value = re.sub(r'\bSept\b', 'Sep', value)
    formats = list(dict.fromkeys([preferred, "%a, %d %b %y", "%d %b %Y", "%a, %d %b %Y",
                                  "%d %b, %Y", "%a, %d %b, %Y", "%A, %d %b %Y",
                                  "%d %B %y", "%d %B %Y", "%d %B, %Y", "%a, %d %B %Y",
                                  "%a, %d %B, %Y", "%A, %d %B %Y", "%b %d, %Y", "%B %d, %Y"]))
    for format_ in formats:
        try:
            return datetime.strptime(value, format_).date()
        except ValueError:
            continue
    shown = value if re.fullmatch(r"[A-Za-z0-9 ,./'-]{1,40}", value) else '<redacted>'
    raise DomainError('EXTRACTION_SCHEMA_FAILED', f'Flight detail date has an unsupported displayed format: {shown}')


def parse_flight_details(spec, html, request, references):
    from scrapling.parser import Selector
    document = Selector(html)
    departures, arrivals = document.css(spec.departures), document.css(spec.arrivals)
    numbers = [x.get_all_text().strip() for x in document.css(spec.numbers)]
    if not 1 <= len(departures) == len(arrivals) == len(numbers) <= 21:
        raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Flight segment counts do not agree')
    def airport(node):
        code = read_one(node, spec.airport_code, required=True).strip('() ')
        if not re.fullmatch(r'[A-Z]{3}', code):
            raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Flight detail airport code is invalid')
        return code
    if (airport(departures[0]),airport(arrivals[-1])) != (request.origin,request.destination):
        raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Flight details belong to another route')
    parsed = []
    for index, (departure, arrival) in enumerate(zip(departures, arrivals, strict=True)):
        if index and airport(arrivals[index-1]) != airport(departure):
            raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Connecting flight segments do not join')
        pair = []
        for node in [departure, arrival]:
            day = displayed_date(read_one(node,spec.date,required=True), spec.date_format)
            time = datetime.strptime(read_one(node,spec.time,required=True),'%H:%M').time()
            ref = references.get(airport(node))
            instant = datetime.combine(day,time,tzinfo=ZoneInfo(ref['timezone'])) if ref and ref.get('timezone') else None
            pair.append((day,instant))
        if index == 0 and pair[0][0] != request.departure_date:
            raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Flight detail departure date does not match the requested date')
        if all(x[1] for x in pair) and pair[1][1] <= pair[0][1]:
            raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Flight segment arrival does not follow departure')
        if index and parsed[-1][1][1] and pair[0][1] and pair[0][1] < parsed[-1][1][1]:
            raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Connecting segment departure precedes arrival')
        parsed.append(pair)
    start,end = parsed[0][0][1],parsed[-1][1][1]
    operators = list(dict.fromkeys(re.sub(r'^Operated by\s+','',x.get_all_text().strip(),flags=re.I) for x in document.css(spec.operators)))
    aircraft = list(dict.fromkeys(x.get_all_text().strip() for x in document.css(spec.aircraft)))
    details = {'segment_count':len(numbers),'direct_or_connecting':'CONNECTING' if len(numbers)>1 else 'DIRECT',
        'operating_airline':' / '.join(operators) or None,'aircraft_type':' / '.join(aircraft) or None,
        'origin_airport_name':read_one(departures[0],spec.airport_name),'origin_city':read_one(departures[0],spec.city),
        'destination_airport_name':read_one(arrivals[-1],spec.airport_name),'destination_city':read_one(arrivals[-1],spec.city)}
    if all(references.get(airport(node),{}).get('country') == 'IN' for node in [*departures,*arrivals]):
        details['route_type'] = 'DOMESTIC'
    if start and end:
        details['duration_minutes'] = int((end-start).total_seconds()/60)
    return {'flight_number':' / '.join(numbers),'departure_at':start,'arrival_at':end,'details':details}


async def inspect_flight_details(eura,browser,card,request,guard):
    spec=eura.manifest.flight_details
    await guard()
    await card.locator(spec.reveal).click()
    await guard()
    dialog=browser.page.locator(spec.dialog)
    await dialog.wait_for(state='visible')
    await guard()
    with session() as db:
        from sqlalchemy import select
        refs={x.iata:{'timezone':x.timezone,'country':x.country} for x in db.scalars(select(AirportReference))}
    data=parse_flight_details(spec,await dialog.inner_html(),request,refs)
    await guard()
    if spec.close:
        close = dialog.locator(spec.close)
        if await close.count() != 1:
            raise DomainError('EXTRACTION_STRUCTURE_FAILED', 'Flight details close control is ambiguous')
        await close.click(timeout=5000)
    else:
        await browser.page.keyboard.press('Escape')
    await dialog.wait_for(state='hidden', timeout=5000)
    await guard()
    return data
