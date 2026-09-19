import asyncio
import logging
import signal
from datetime import timedelta

from croniter import croniter
from sqlalchemy import select, text, update

from udaan.browser import UdaanBrowser
from udaan.challenges import ChallengeFlow, end_wait
from udaan.config import settings
from udaan.contracts import DomainError, JobInput, SearchRequest
from udaan.data import export_query, process_fares
from udaan.db import (
    Export,
    Job,
    Observation,
    Recipe,
    Repair,
    Schedule,
    ScrapeGroup,
    Source,
    engine,
    event,
    now,
    session,
    uid,
)
from udaan.eura import load_recipe
from udaan.services import ACTIVE, TERMINAL, create_job, get, resolve_search

log = logging.getLogger("udaan.worker")


class ResumeCheckpoint(Exception):
    pass


def schedule_due(db, instant=None):
    instant = instant or now()
    schedules = db.scalars(select(Schedule).where(Schedule.enabled.is_(True), Schedule.next_run_at <= instant)
                           .with_for_update(skip_locked=True).limit(100)).all()
    for schedule in schedules:
        occurrence = schedule.next_run_at
        if schedule.timing and schedule.timing['frequency'] not in {'once','now'}:
            from udaan.scheduling import occurrences
            occurrence = max(occurrence, occurrences(schedule.timing, instant, previous=True))
        if schedule.cron:
            # Coalesce missed executions to the most recent occurrence; never enqueue an unbounded backlog.
            latest = croniter(schedule.cron, instant + timedelta(microseconds=1)).get_prev(type(instant))
            occurrence = max(occurrence, latest)
        existing = db.scalar(select(ScrapeGroup.id).where(ScrapeGroup.schedule_id == schedule.id, ScrapeGroup.scheduled_for == occurrence)) or db.scalar(select(Job.id).where(Job.schedule_id == schedule.id, Job.scheduled_for == occurrence))
        if not existing:
            try:
                with db.begin_nested():
                    if schedule.group_request:
                        from udaan.groups import GroupInput, create_group
                        create_group(db, GroupInput.model_validate(schedule.group_request),
                                     schedule_id=schedule.id, scheduled_for=occurrence)
                    elif schedule.booking_windows:
                        from udaan.groups import GroupInput, create_group
                        create_group(db, GroupInput(source_id=schedule.source_id, search=schedule.request,
                                     booking_windows=schedule.booking_windows), schedule_id=schedule.id, scheduled_for=occurrence)
                    else:
                        create_job(db, JobInput(source_id=schedule.source_id, search=SearchRequest.model_validate(schedule.request)),
                                   schedule_id=schedule.id, scheduled_for=occurrence)
            except DomainError as exc:
                schedule.enabled = False
                schedule.last_error = f"{exc.code}: {exc.message}"
                log.warning("schedule_blocked schedule_id=%s code=%s", schedule.id, exc.code)
                continue
        schedule.last_error = None
        if schedule.timing and schedule.timing['frequency'] not in {'once','now'}:
            from udaan.scheduling import occurrences
            schedule.next_run_at = occurrences(schedule.timing, instant)
        else:
            schedule.next_run_at = croniter(schedule.cron, instant).get_next(type(instant)) if schedule.cron else None
        if schedule.next_run_at is None:
            schedule.enabled = False


def recover_expired(db, instant=None):
    instant = instant or now()
    for job in db.scalars(select(Job).where(Job.state.in_(ACTIVE), Job.lease_until < instant)
                           .with_for_update(skip_locked=True)):
        if job.state == "WAITING_FOR_OPERATOR":
            end_wait(db, job, "FAILED", "WORKER_SESSION_LOST", "Worker lease expired; browser session cannot be recovered", instant)
        else:
            job.state, job.finished_at = "FAILED", instant
            job.error_code, job.reason = "WORKER_SESSION_LOST", "Worker lease expired; retry creates a new session"
            event(db, job, "JOB_FAILED", code=job.error_code, reason=job.reason)
    for model in (Export, Repair):
        for item in db.scalars(select(model).where(model.owner.is_not(None), model.lease_until < instant)
                               .with_for_update(skip_locked=True)):
            if model is Export and item.state == "RUNNING":
                item.state, item.error = "FAILED", "Export worker interrupted; create a new export"
            elif model is Repair and item.state not in {"PROMOTED", "FAILED", "BLOCKED", "LIVE_VALIDATION"}:
                item.state, item.reason = "BLOCKED", "Repair worker interrupted; explicit retry required"
            item.owner, item.lease_until = None, None


def claim_job(db, owner):
    db.execute(text("SELECT pg_advisory_xact_lock(82421)"))
    active = list(db.scalars(select(Job).where(Job.state.in_(ACTIVE))))
    hard_limit = settings().collection_concurrency
    if len(active) >= hard_limit:
        return None
    for job in db.scalars(select(Job).where(Job.state == 'QUEUED').order_by(Job.created_at, Job.id)
                          .with_for_update(skip_locked=True).limit(100)):
        source = db.get(Source, job.source_id)
        if source.status == 'BLOCKED' and job.purpose == 'COLLECTION':
            job.state, job.error_code, job.reason, job.finished_at = 'BLOCKED', 'SOURCE_BLOCKED', 'Source needs a successful test after its access barrier', now()
            event(db, job, 'JOB_BLOCKED', reason=job.reason)
            continue
        if source.cooldown_until and source.cooldown_until > now():
            continue
        if job.group_id:
            group = db.get(ScrapeGroup, job.group_id)
            if sum(item.group_id == job.group_id for item in active) >= group.request.get('parallel_scrapes', 3):
                continue
        job.state, job.owner, job.started_at = 'RUNNING', owner, now()
        job.lease_until = now() + timedelta(seconds=settings().worker_lease_seconds)
        event(db, job, 'JOB_STARTED', owner=owner, recipe_id=job.recipe_id)
        return job.id
    return None


class Worker:
    def __init__(self):
        self.owner = uid()
        self.stopping = asyncio.Event()
        self.tasks: set[asyncio.Task] = set()
        self.browsers = {}
        from udaan.browser_pool import ParallelBrowserSlots
        self.slots = ParallelBrowserSlots()
        self.repair_task = None

    async def heartbeat(self):
        while not self.stopping.is_set():
            try:
                with session() as db:
                    from udaan.db import WorkerStatus
                    heartbeat = db.get(WorkerStatus, self.owner)
                    if heartbeat is None:
                        heartbeat = WorkerStatus(id=self.owner, state="RUNNING")
                        db.add(heartbeat)
                    heartbeat.heartbeat_at = now()
                    deadline = now() + timedelta(seconds=settings().worker_lease_seconds)
                    db.execute(update(Job).where(Job.owner == self.owner, Job.state.in_(ACTIVE)).values(lease_until=deadline))
                    for model in (Export, Repair):
                        db.execute(update(model).where(model.owner == self.owner).values(lease_until=deadline))
            except Exception:
                log.error("worker_heartbeat_failed code=DATABASE_UNAVAILABLE")
                # Stop automation immediately when ownership cannot be renewed.
                for task in list(self.tasks):
                    task.cancel()
            try:
                await asyncio.wait_for(self.stopping.wait(), settings().worker_lease_seconds / 3)
            except TimeoutError:
                pass

    def start_task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        def complete(done):
            self.tasks.discard(done)
            if not done.cancelled() and done.exception():
                log.error("worker_task_failed category=%s", type(done.exception()).__name__)
        task.add_done_callback(complete)

    async def collect(self, job_id):
        browser = None
        with session() as db:
            purpose = get(db, Job, job_id).purpose
        if purpose == "DISCOVERY":
            from udaan.discovery_runner import run_discovery
            return await run_discovery(self, job_id)
        if purpose == "RECIPE_BUILD":
            from udaan.agent import build_recipe
            return await build_recipe(self, job_id)
        try:
            with session() as db:
                job = get(db, Job, job_id)
                source = get(db, Source, job.source_id)
                recipe = get(db, Recipe, job.recipe_id)
                if source.archived or (not source.enabled and job.purpose != "RECIPE_VALIDATION"):
                    raise DomainError("SOURCE_DISABLED", "Source was disabled before execution")
                if not source.configuration.get("permitted_collection"):
                    raise DomainError("SOURCE_NOT_VALIDATED", "Collection permission was not established")
                request = SearchRequest.model_validate(resolve_search(SearchRequest.model_validate(job.request)))
                job.request = request.model_dump(mode="json")
                eura = load_recipe(recipe, source)
                eura.validation_card_limit = 3 if job.purpose == "RECIPE_VALIDATION" else None
                from udaan.global_ip import GlobalIPDataset
                egress = GlobalIPDataset().get(job.network_profile)
            async with UdaanBrowser(eura.manifest.allowed_hosts, request.resilience_profile, slots=self.slots, egress=egress) as browser:
                self.browsers[job_id] = browser
                with session() as db:
                    event(db, get(db, Job, job_id), "BROWSER_STARTED", mode="headed", visible=True, slot_id=job_id,
                          context_id=browser.context_id, isolated_context=True, network_profile=egress.id,
                          network_provider=egress.provider, network_routing="configured" if egress.proxy_server else "system")
                async def inspect():
                    return await eura.inspect(browser, request)
                flow = ChallengeFlow(job_id, self.owner, browser, inspect)
                async def guard():
                    with session() as db:
                        current = get(db, Job, job_id)
                        if current.owner != self.owner or current.state not in ACTIVE or current.lease_until <= now():
                            raise DomainError("WORKER_LEASE_LOST", "Worker no longer owns job")
                        if current.cancel_requested:
                            raise DomainError("OPERATOR_CANCELLED", "Operator cancelled job")
                        current_source = get(db, Source, current.source_id)
                        if current_source.cooldown_until and current_source.cooldown_until > now():
                            if current.error_code == 'SECURITY_BACKOFF':
                                raise DomainError('SECURITY_BACKOFF', current.reason)
                            raise DomainError('SOURCE_COOLDOWN', 'Source cooldown is active; no automated interaction permitted')
                    barrier = await browser.security()
                    if barrier:
                        await self.respond_to_barrier(job_id, browser, flow, barrier)
                        raise ResumeCheckpoint()
                    return False
                async def checkpoint(value):
                    with session() as db:
                        current = get(db, Job, job_id, lock=True)
                        current.checkpoint = value
                        event(db, current, "RECIPE_CHECKPOINT", checkpoint=value)
                recovery_attempt = 0
                while True:
                    try:
                        fares = await eura.collect(request, browser, guard, checkpoint)
                        await guard()
                        for _ in range(browser.profile.verification_passes - 1):
                            await asyncio.sleep(browser.profile.stability_ms / 1000)
                            await guard()
                            if not await eura.context_matches(browser, request):
                                raise DomainError('EXTRACTION_SCHEMA_FAILED', 'Search context changed during stabilization')
                        break
                    except ResumeCheckpoint:
                        continue
                    except Exception as exc:
                        try:
                            if browser.alive and getattr(exc, "code", None) not in {"SECURITY_BACKOFF", "SOURCE_COOLDOWN", "SOURCE_BLOCKED"}:
                                await guard()
                            if not isinstance(exc, DomainError):
                                from playwright.async_api import TimeoutError as BrowserTimeout
                                if isinstance(exc, BrowserTimeout):
                                    await guard()
                                    exc = DomainError('EXTRACTION_STRUCTURE_FAILED', 'A booking control timed out')
                                elif not browser.alive:
                                    exc = DomainError('BROWSER_SESSION_LOST', 'The browser page stopped')
                                else:
                                    raise
                            from udaan.recovery import recover_ordinary
                            if await recover_ordinary(browser, exc, recovery_attempt, guard):
                                recovery_attempt += 1
                                with session() as db:
                                    eura = load_recipe(get(db, Recipe, job.recipe_id), source)
                                    eura.validation_card_limit = 3 if job.purpose == 'RECIPE_VALIDATION' else None
                                    event(db, get(db, Job, job_id), 'ORDINARY_PAGE_RECOVERY', attempt=recovery_attempt, code=exc.code)
                                continue
                            # Structural context contains only allowlisted tag/class names and only after a barrier check.
                            structure = None
                            if browser.alive and not browser.barrier_seen:
                                structure = await browser.structure()
                            if structure:
                                with session() as db:
                                    event(db, get(db, Job, job_id), "STRUCTURE_DIAGNOSTIC", elements=structure)
                            raise exc
                        except ResumeCheckpoint:
                            continue
                await checkpoint(f"Checking {len(fares)} fare options")
                rows = process_fares(fares, request, all_cabins=eura.manifest.contract == 3)
                await checkpoint(f"Saving {len(rows)} fare options")
                with session() as db:
                    current = get(db, Job, job_id, lock=True)
                    if current.owner != self.owner or current.state != "RUNNING" or current.lease_until <= now():
                        raise DomainError("WORKER_LEASE_LOST", "Ownership lost before persistence")
                    if current.cancel_requested:
                        raise DomainError("OPERATOR_CANCELLED", "Operator cancelled before persistence")
                    if current.purpose in {"REPAIR_VALIDATION", "RECIPE_VALIDATION"} and not rows:
                        raise DomainError("REPAIR_VALIDATION_EMPTY", "Empty results cannot validate a repair")
                    collected_at = now()
                    for row in rows:
                        db.add(Observation(job_id=current.id, group_id=current.group_id, source_id=current.source_id,
                                           recipe_id=current.recipe_id, collected_at=collected_at, **row))
                    current.state, current.finished_at, current.observation_count = "SUCCEEDED", collected_at, len(rows)
                    current.reason = "Verified empty search" if not rows else "Validated real observations persisted"
                    recipe = get(db, Recipe, current.recipe_id, lock=True)
                    recipe.successes += 1
                    recipe.health, recipe.live_validated_at = "HEALTHY", collected_at
                    source = get(db, Source, current.source_id, lock=True)
                    if rows and eura.manifest.contract == 3:
                        from udaan.db import DiscoveryRun
                        from udaan.discovery import DiscoveredAirport, DiscoveryResult, persist_discovery
                        run = DiscoveryRun(source_id=source.id, recipe_id=recipe.id, job_id=current.id, scope="ROUTES")
                        db.add(run)
                        db.flush()
                        persist_discovery(db, run, DiscoveryResult(
                            origins=[DiscoveredAirport(iata=request.origin)],
                            destinations={request.origin: [DiscoveredAirport(iata=request.destination)]},
                            reason="Exact route verified through real search and validated fare results; this is a partial network sample."))
                        event(db, current, "VALIDATION_SCOPE", matched_cards=getattr(eura, "_family_matched", None),
                              card_limit=eura.validation_card_limit, network_complete=False)
                    if source.active_recipe_id == recipe.id:
                        from udaan.services import validate_source
                        validate_source(db, source)
                    event(db, current, "JOB_SUCCEEDED", observation_count=len(rows), verified_empty=not rows)
        except asyncio.CancelledError:
            self.fail(job_id, "WORKER_INTERRUPTED", "Worker stopped; browser session has ended")
            raise
        except DomainError as exc:
            self.fail(job_id, exc.code, exc.message)
        except Exception as exc:
            import traceback
            frames = [{"function": frame.name, "line": frame.lineno, "file": __import__("pathlib").Path(frame.filename).name} for frame in traceback.extract_tb(exc.__traceback__) if "/udaan/" in frame.filename]
            log.error("job_failed job_id=%s category=%s frames=%s", job_id, type(exc).__name__, frames)
            self.fail(job_id, "COLLECTION_FAILED", "Collection failed; inspect dependency and recipe health")
        finally:
            self.browsers.pop(job_id, None)

    async def respond_to_barrier(self, job_id, browser, flow, barrier):
        profile = browser.profile
        if barrier.challenge_type == 'RATE_LIMIT' or profile.challenge_behavior == 'back_off':
            with session() as db:
                current = get(db, Job, job_id, lock=True)
                source = get(db, Source, current.source_id, lock=True)
                until = barrier.retry_after or now() + timedelta(seconds=300)
                source.cooldown_until = max(source.cooldown_until or until, until)
                event(db, current, 'SOURCE_BACKOFF', reason=barrier.reason, retry_after=until.isoformat())
                if profile.challenge_behavior == 'back_off':
                    source.status = 'BLOCKED'
                    current.error_code, current.reason = 'SECURITY_BACKOFF', barrier.reason + '; Udaan stopped and backed off'
            if profile.challenge_behavior == 'back_off':
                raise DomainError('SECURITY_BACKOFF', barrier.reason + '; Udaan stopped and backed off')
        await flow.wait(barrier)

    def fail(self, job_id, code, reason):
        try:
            with session() as db:
                job = get(db, Job, job_id, lock=True)
                if job.state in TERMINAL or job.owner != self.owner:
                    return
                state = "CANCELLED" if code == "OPERATOR_CANCELLED" else "BLOCKED" if code in {"SECURITY_BACKOFF", "SOURCE_COOLDOWN", "SOURCE_BLOCKED"} or (job.purpose == "RECIPE_BUILD" and code.endswith("UNAVAILABLE")) else "FAILED"
                if job.state == "WAITING_FOR_OPERATOR":
                    end_wait(db, job, state, code, reason)
                else:
                    job.state, job.error_code, job.reason, job.finished_at = state, code, reason, now()
                    event(db, job, f"JOB_{state}", code=code, reason=reason)
                browser = self.browsers.get(job_id)
                event(db, job, "JOB_FAILURE_CONTEXT", code=code, stage=job.checkpoint, slot_id=job.id,
                      context_id=getattr(browser, "context_id", None), network_profile=job.network_profile,
                      isolated_context=bool(getattr(browser, "context", None)))
                if job.purpose not in {"RECIPE_BUILD", "DISCOVERY"} and code in {"EXTRACTION_STRUCTURE_FAILED", "EXTRACTION_SCHEMA_FAILED"}:
                    recipe = get(db, Recipe, job.recipe_id, lock=True)
                    recipe.failures += 1
                    # Keep durable validation separate from a later runtime failure.
                    if not recipe.live_validated_at or recipe.successes == 0:
                        recipe.health = "DEGRADED"
                    source = get(db, Source, job.source_id, lock=True)
                    if source.active_recipe_id == recipe.id:
                        from udaan.services import validate_source
                        validate_source(db, source)
                    if job.purpose != "REPAIR_VALIDATION" and not db.scalar(select(Repair.id).where(Repair.job_id == job.id)):
                        db.add(Repair(job_id=job.id, recipe_id=job.recipe_id))
        except Exception:
            log.error("job_failure_persistence_failed job_id=%s code=DATABASE_UNAVAILABLE", job_id)

    async def export(self, identifier):
        def run_export():
            from udaan.contracts import ExportInput
            with session() as db:
                item = get(db, Export, identifier)
                spec = ExportInput.model_validate(item.query)
            with engine().connect().execution_options(isolation_level="REPEATABLE READ") as connection:
                with connection.begin():
                    return export_query(connection, spec.query, spec.format, spec.rows, identifier,
                                        processing=spec.processing)
        try:
            result = await asyncio.to_thread(run_export)
            with session() as db:
                item = get(db, Export, identifier, lock=True)
                if item.owner == self.owner and item.state == "RUNNING":
                    for key, value in result.items():
                        setattr(item, key, value)
                    item.state, item.owner, item.lease_until = "SUCCEEDED", None, None
                    log.info("export_succeeded export_id=%s rows=%s sources=%s jobs=%s scope=%s",
                             item.id, item.exported_count, item.matched_source_count, item.matched_job_count, item.scope)
        except Exception:
            with session() as db:
                item = get(db, Export, identifier, lock=True)
                item.state, item.error, item.owner = "FAILED", "Export failed; inspect query and database health", None

    async def run(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stopping.set)
        heartbeat = asyncio.create_task(self.heartbeat())
        try:
            while not self.stopping.is_set():
                try:
                    with session() as db:
                        recover_expired(db)
                        schedule_due(db)
                        identifier = claim_job(db, self.owner)
                    if identifier:
                        self.start_task(self.collect(identifier))
                    if len(self.tasks) < settings().collection_concurrency + 2:
                        with session() as db:
                            export = db.scalar(select(Export).where(Export.state == "QUEUED")
                                                .with_for_update(skip_locked=True).limit(1))
                            export_id = None
                            if export:
                                export.state, export.owner = "RUNNING", self.owner
                                export.lease_until = now() + timedelta(seconds=settings().worker_lease_seconds)
                                export_id = export.id
                        if export_id:
                            self.start_task(self.export(export_id))
                        if self.repair_task is None or self.repair_task.done():
                            from udaan.agent import repair_tick
                            self.repair_task = asyncio.create_task(repair_tick(self.owner))
                            self.tasks.add(self.repair_task)
                            self.repair_task.add_done_callback(self.tasks.discard)
                except Exception as exc:
                    log.error("worker_poll_failed category=%s", type(exc).__name__)
                try:
                    await asyncio.wait_for(self.stopping.wait(), 1)
                except TimeoutError:
                    pass
        finally:
            self.stopping.set()
            for task in list(self.tasks):
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            await self.slots.close()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            try:
                with session() as db:
                    from udaan.db import WorkerStatus
                    status = db.get(WorkerStatus, self.owner)
                    if status:
                        status.state = "STOPPED"
                        status.heartbeat_at = now()
            except Exception:
                log.error("worker_shutdown_record_failed code=DATABASE_UNAVAILABLE")
