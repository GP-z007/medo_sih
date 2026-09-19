"""Discovery runs on the same leased worker and headed browser as collection."""
import asyncio
import re

from sqlalchemy import select

from udaan.browser import UdaanBrowser
from udaan.challenges import ChallengeFlow
from udaan.contracts import DomainError, Inspection, PageState
from udaan.db import DiscoveryRun, Job, Recipe, Source, event, now, session
from udaan.discovery import DiscoveredAirport, DiscoveryResult, persist_discovery
from udaan.eura import load_recipe
from udaan.services import ACTIVE, get


async def discover(eura, browser, guard, checkpoint, scope, progress=None):
    spec = eura.manifest.discovery
    if spec.mode == 'validated_routes':
        raise DomainError('DISCOVERY_UNVERIFIED', 'This recipe records routes from successful real searches; use Test Recipe for an additional route')
    if spec.catalog_url and not hasattr(browser, 'airport_catalog'):
        browser.watch_airport_catalog(spec)
    if browser.page.url == 'about:blank':
        await browser.navigate(eura.base_url)
    await guard()
    if spec.consent:
        consent = await browser.visible(spec.consent)
        if consent:
            await guard()
            await consent.click()
            await guard()
    if spec.mode == 'indigo_stations':
        from udaan.indigo import discover_airports
        return await discover_airports(eura, browser, guard, checkpoint, scope)
    if spec.mode == 'autocomplete_catalog':
        return await discover_catalog(eura, browser, guard, checkpoint, scope, progress)
    if spec.mode == 'autocomplete_scroll':
        return await discover_autocomplete(eura, browser, guard, checkpoint, scope)
    if spec.mode != 'native_select':
        raise DomainError('DISCOVERY_NOT_IMPLEMENTED', 'Complete discovery for this control is still being implemented')
    await browser.page.locator(spec.origin).wait_for(state='attached')
    await guard()
    def airport(option):
        city = re.sub(r'\s*\([A-Z]{3}\)\s*$', '', option['label']).strip()
        return DiscoveredAirport(iata=option['iata'], city=city or None)
    origins = [airport(x) for x in await browser.airport_options(spec.origin)]
    if not origins:
        raise DomainError('EXTRACTION_STRUCTURE_FAILED', 'Origin control contains no airport options')
    destinations = {}
    if scope == 'ROUTES':
        for index, origin in enumerate(origins):
            await guard()
            await checkpoint(f'Discovering routes {index + 1}/{len(origins)}')
            target = browser.page.locator(spec.origin)
            await target.select_option(value=origin.iata)
            await guard()
            if await target.input_value() != origin.iata:
                raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Selected discovery origin could not be verified')
            await asyncio.sleep(spec.wait_ms / 1000)
            # Require stable options after the origin-change handler has run.
            previous = None
            stable = 0
            for _ in range(10):
                await guard()
                options = await browser.airport_options(spec.destination)
                stable = stable + 1 if options == previous else 0
                if stable >= 2:
                    break
                previous = options
                await asyncio.sleep(spec.wait_ms / 1000)
            else:
                raise DomainError('DISCOVERY_UNSTABLE', 'Destination options did not stabilize for the selected origin')
            if any(x['iata'] == origin.iata for x in options):
                raise DomainError('DISCOVERY_UNVERIFIED', 'Destination options did not exclude the selected origin; route availability cannot be verified')
            destinations[origin.iata] = [airport(x) for x in options]
    return DiscoveryResult(origins=origins, destinations=destinations, origins_complete=True,
                           routes_complete=scope == 'ROUTES', reason='Read enabled booking-form options; destinations inspected separately after each origin selection')


async def run_discovery(worker, job_id):
    try:
        with session() as db:
            job = get(db, Job, job_id)
            source = get(db, Source, job.source_id)
            recipe = get(db, Recipe, job.recipe_id)
            if source.archived or not source.configuration.get('permitted_collection'):
                raise DomainError('SOURCE_NOT_VALIDATED', 'Source is archived or collection is not authorized')
            eura, scope = load_recipe(recipe, source), job.request['scope']
            run = db.scalar(select(DiscoveryRun).where(DiscoveryRun.job_id == job_id))
            run.state = 'RUNNING'
        async with UdaanBrowser(eura.manifest.allowed_hosts) as browser:
            worker.browsers[job_id] = browser
            async def inspect():
                barrier = await browser.security()
                if barrier:
                    return barrier
                if await browser.visible(eura.manifest.discovery.origin) is not None:
                    return Inspection(state=PageState.READY, reason='Expected airport discovery form verified', checkpoint='discovery')
                return Inspection(state=PageState.INVALID, reason='Expected airport discovery form is unavailable')
            flow = ChallengeFlow(job_id, worker.owner, browser, inspect)
            async def guard():
                with session() as db:
                    current = get(db, Job, job_id)
                    if current.owner != worker.owner or current.state not in ACTIVE or current.lease_until <= now():
                        raise DomainError('WORKER_LEASE_LOST', 'Worker no longer owns discovery')
                    if current.cancel_requested:
                        raise DomainError('OPERATOR_CANCELLED', 'Operator cancelled discovery')
                if not browser.alive:
                    raise DomainError('BROWSER_SESSION_LOST', 'Discovery browser session ended')
                barrier = await browser.security()
                if barrier:
                    await flow.wait(barrier)
                    # Re-enter from a verified form with the same browser and cookies.
                    from udaan.worker import ResumeCheckpoint
                    raise ResumeCheckpoint()
            async def checkpoint(value):
                with session() as db:
                    current = get(db, Job, job_id, lock=True)
                    current.checkpoint = value
                    event(db, current, 'RECIPE_CHECKPOINT', checkpoint=value)
            async def progress(result):
                with session() as db:
                    run = db.scalar(select(DiscoveryRun).where(DiscoveryRun.job_id == job_id).with_for_update())
                    persist_discovery(db, run, result)
                    run.state, run.finished_at = "RUNNING", None
            while True:
                from udaan.worker import ResumeCheckpoint
                try:
                    result = await discover(eura, browser, guard, checkpoint, scope, progress)
                    break
                except ResumeCheckpoint:
                    continue
            await guard()
            with session() as db:
                current = get(db, Job, job_id, lock=True)
                if current.owner != worker.owner or current.state != 'RUNNING' or current.lease_until <= now() or current.cancel_requested:
                    raise DomainError('WORKER_LEASE_LOST', 'Discovery ownership changed before persistence')
                run = db.scalar(select(DiscoveryRun).where(DiscoveryRun.job_id == job_id).with_for_update())
                persist_discovery(db, run, result)
                current.state, current.finished_at = 'SUCCEEDED', now()
                current.reason = f'Discovered {run.airport_count} airports and {run.route_count} routes; complete={run.complete}'
                event(db, current, 'DISCOVERY_SUCCEEDED', airport_count=run.airport_count, route_count=run.route_count, complete=run.complete)
    except asyncio.CancelledError:
        worker.fail(job_id, 'WORKER_INTERRUPTED', 'Discovery worker stopped; browser session ended')
        raise
    except DomainError as exc:
        worker.fail(job_id, exc.code, exc.message)
    except Exception:
        worker.fail(job_id, 'DISCOVERY_FAILED', 'Airport/route discovery failed; inspect source and browser health')
    finally:
        worker.browsers.pop(job_id, None)
        with session() as db:
            current = get(db, Job, job_id)
            run = db.scalar(select(DiscoveryRun).where(DiscoveryRun.job_id == job_id).with_for_update())
            if run and run.state in {'QUEUED','RUNNING'} and current.state in {'FAILED','BLOCKED','CANCELLED'}:
                run.state, run.reason, run.finished_at = current.state, current.reason, current.finished_at


async def discover_autocomplete(eura, browser, guard, checkpoint, scope):
    spec = eura.manifest.discovery
    async def options(control):
        await guard()
        await control.fill(spec.country_filter or '')
        await control.click()
        await asyncio.sleep(spec.wait_ms / 1000)
        seen = {}
        stable = 0
        for _ in range(spec.max_scrolls):
            await guard()
            box = browser.page.locator(spec.listbox)
            if await box.count() != 1:
                raise DomainError('DISCOVERY_UNVERIFIED', 'Expected airport options are unavailable or ambiguous')
            from scrapling.parser import Selector
            document = Selector(await box.inner_html())
            before = len(seen)
            for row in document.css(spec.options):
                def read(css):
                    nodes = row.css(css) if css else []
                    return nodes[0].get_all_text().strip() if len(nodes) == 1 else None
                code = read(spec.code)
                if not code or not re.fullmatch(r'[A-Z]{3}', code):
                    raise DomainError('DISCOVERY_UNVERIFIED', 'Airport option has no unambiguous IATA code')
                city = read(spec.city)
                country = 'IN' if city and re.search(r',\s*India,\s*IN$', city) else None
                seen[code] = DiscoveredAirport(iata=code, airport_name=read(spec.airport_name), city=city.split(',')[0] if city else None, country=country)
            stable = stable + 1 if len(seen) == before else 0
            at_bottom = await box.evaluate('e=>e.scrollTop+e.clientHeight>=e.scrollHeight-2')
            if at_bottom and stable >= 3:
                if len(seen) >= 50:
                    raise DomainError("DISCOVERY_INCOMPLETE", "Autocomplete may truncate results; a complete catalog must be verified")
                if not seen:
                    raise DomainError('DISCOVERY_UNVERIFIED', 'Empty autocomplete cannot establish a complete airport list')
                return list(seen.values())
            await box.evaluate('e=>e.scrollTo(0,e.scrollHeight)')
            await asyncio.sleep(spec.wait_ms / 1000)
        raise DomainError('DISCOVERY_INCOMPLETE', 'Airport list did not reach a stable end within the configured scroll budget')
    origin_control = browser.page.locator(spec.origin).nth(spec.origin_index)
    origins = await options(origin_control)
    destinations = {}
    if scope == 'ROUTES':
        for index, origin in enumerate(origins):
            await checkpoint(f'Discovering routes {index + 1}/{len(origins)}')
            await guard()
            await origin_control.fill(origin.iata)
            await asyncio.sleep(spec.wait_ms / 1000)
            choices = browser.page.locator(spec.listbox).locator(spec.options).filter(has_text=re.compile(r'\b'+origin.iata+r'\b'))
            if await choices.count() != 1:
                raise DomainError('DISCOVERY_UNVERIFIED', 'Origin selection is ambiguous')
            await choices.click()
            await guard()
            selected = await origin_control.input_value()
            if origin.iata not in selected:
                raise DomainError('DISCOVERY_UNVERIFIED', 'Selected origin does not match discovery context')
            dest_control = browser.page.locator(spec.destination).nth(spec.destination_index)
            offered = await options(dest_control)
            destinations[origin.iata] = [a for a in offered if a.iata != origin.iata]
            await guard()
            await browser.page.keyboard.press('Escape')
    return DiscoveryResult(origins=origins, destinations=destinations, origins_complete=True, routes_complete=scope == 'ROUTES',
        reason='Exhausted the live airport autocomplete, separately inspecting destination options after each origin selection; these are offered search routes, not a guarantee of flights on every date')


async def discover_catalog(eura, browser, guard, checkpoint, scope, progress=None):
    spec = eura.manifest.discovery
    for _ in range(60):
        await guard()
        if browser.airport_catalog:
            break
        await asyncio.sleep(.5)
    else:
        raise DomainError('DISCOVERY_CATALOG_UNAVAILABLE', 'The booking form did not load its configured public airport catalog')
    candidates = {a.iata:a for a in browser.airport_catalog}
    origin_control = browser.page.locator(spec.origin).nth(spec.origin_index)
    destination_control = browser.page.locator(spec.destination).nth(spec.destination_index)
    async def offered(control, code, select=False):
        await guard()
        await control.fill(code)
        await control.click()
        await asyncio.sleep(spec.wait_ms / 1000)
        await guard()
        choices = browser.page.locator(spec.listbox).locator(spec.options).filter(has_text=re.compile(r'\b'+code+r'\b'))
        count = await choices.count()
        if count > 1:
            raise DomainError('DISCOVERY_UNVERIFIED', 'Airport suggestion is ambiguous')
        if count == 0:
            return False
        if await choices.first.get_attribute('aria-disabled') == 'true':
            return False
        if select:
            await choices.first.click()
            await guard()
            if code not in await control.input_value():
                raise DomainError('DISCOVERY_UNVERIFIED', 'Selected origin does not match the inspected airport')
        return True
    origins = []
    for index, airport in enumerate(candidates.values()):
        await checkpoint(f'Verifying origins {index + 1}/{len(candidates)}')
        if await offered(origin_control, airport.iata):
            origins.append(airport)
    await browser.page.keyboard.press('Escape')
    result = DiscoveryResult(origins=origins, origins_complete=True, reason='Verified Indian airports from the booking-form catalog against actual origin suggestions')
    if progress:
        await progress(result)
    if scope == 'AIRPORTS':
        return result
    destinations = {}
    for index, origin in enumerate(origins):
        await checkpoint(f'Discovering routes {index + 1}/{len(origins)}')
        if not await offered(origin_control, origin.iata, select=True):
            raise DomainError('DISCOVERY_UNVERIFIED', 'Previously offered origin is no longer available')
        destinations[origin.iata] = []
        for code, airport in candidates.items():
            if code != origin.iata and await offered(destination_control, code):
                destinations[origin.iata].append(airport)
        await guard()
        await browser.page.keyboard.press('Escape')
        if progress:
            await progress(DiscoveryResult(origins=origins, destinations=dict(destinations), origins_complete=True,
                reason='Destination suggestions verified separately for each completed origin; discovery continues'))
    return DiscoveryResult(origins=origins, destinations=destinations, origins_complete=True, routes_complete=True,
        reason='Verified every Indian catalog airport in the origin control, then each offered destination separately after selecting each origin; no route Cartesian product generated')
