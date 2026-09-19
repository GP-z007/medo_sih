import hashlib
import json
import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from udaan.contracts import DomainError, JobInput, OperatorAction, ScheduleInput, SearchRequest, SourceInput, canonical_source_url
from udaan.db import Job, JobEvent, Observation, OperatorCommand, Recipe, Schedule, Source, event, now

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "BLOCKED"}
ACTIVE = {"RUNNING", "WAITING_FOR_OPERATOR"}
logger = logging.getLogger(__name__)


def get(db: Session, model, identifier: str, lock=False):
    query = select(model).where(model.id == identifier)
    if lock:
        query = query.with_for_update()
    item = db.scalar(query)
    if item is None:
        raise DomainError("NOT_FOUND", f"{model.__name__} does not exist", 404)
    return item


def serialize(item):
    return {c.key: getattr(item, c.key) for c in item.__table__.columns}


def add_source(db: Session, data: SourceInput):
    conflict = db.scalar(select(Source).where(Source.slug == data.slug))
    if conflict:
        logger.info("source slug conflict", extra={"source_id": conflict.id, "reason": "slug"})
        raise DomainError(
            "SOURCE_SLUG_CONFLICT",
            f"Short name is already used by source {conflict.id}; choose a different short name.",
        )
    values = data.model_dump(mode="json")
    values["base_url"] = canonical_source_url(values["base_url"])
    source = Source(**values)
    db.add(source)
    db.flush()
    return source


def register_source(db: Session, data: SourceInput):
    """Idempotently create or reactivate a source by canonical website identity."""
    canonical_url = canonical_source_url(data.base_url)
    matches = [source for source in db.scalars(select(Source))
               if canonical_source_url(source.base_url) == canonical_url]
    if matches:
        # Prefer the operational row when legacy data contains an archived placeholder.
        source = min(matches, key=lambda item: (
            item.archived, not item.enabled, item.active_recipe_id is None,
            item.created_at, item.id,
        ))
        was_inactive = source.archived or not source.enabled
        permission = source.configuration.get("permitted_collection") is True
        if data.configuration.get("permitted_collection") is True and not permission:
            source.configuration = {**source.configuration, "permitted_collection": True}
            permission = True
        source.archived = False
        if permission:
            source.enabled = True
        if was_inactive:
            validate_source(db, source)
        readiness = source_readiness(db, source)
        logger.info("source registration reused existing row", extra={
            "source_id": source.id,
            "reason": "canonical_url",
            "submitted_name": data.name,
            "canonical_url": canonical_url,
            "enabled": source.enabled,
            "archived": source.archived,
            "recipe_status": readiness["status"],
        })
        return source, {
            "created": False,
            "existing": True,
            "reactivated": was_inactive and not source.archived and source.enabled,
            "match_reason": "canonical_url",
            "canonical_url": canonical_url,
            "readiness": readiness,
        }
    source = add_source(db, data)
    readiness = source_readiness(db, source)
    return source, {
        "created": True,
        "existing": False,
        "reactivated": False,
        "match_reason": None,
        "canonical_url": canonical_url,
        "readiness": readiness,
    }


def gate(db: Session, source: Source, *, require_enabled=True):
    if source.archived or (require_enabled and not source.enabled):
        raise DomainError("SOURCE_DISABLED", "Source is disabled or archived")
    SourceInput.model_validate({key: getattr(source, key) for key in SourceInput.model_fields})
    if not source.configuration.get("permitted_collection"):
        raise DomainError("SOURCE_NOT_VALIDATED", "Record permitted_collection after reviewing source access conditions")
    if not source.active_recipe_id:
        raise DomainError("RECIPE_MISSING", "NOT READY — NO VALID EURA RECIPE")
    recipe = get(db, Recipe, source.active_recipe_id)
    if recipe.source_id != source.id or not recipe.validated_at:
        raise DomainError("RECIPE_INVALID", "Attached Eura recipe has not passed contract validation")
    from udaan.eura import load_recipe
    loaded = load_recipe(recipe, source)
    if recipe.manifest.get("contract", 1) < source.configuration.get("recipe_contract_minimum", 1):
        raise DomainError("RECIPE_UPGRADE_REQUIRED", "The current recipe does not implement complete airport, route and fare-family collection")
    if loaded.manifest.contract == 3 and not loaded.manifest.families:
        raise DomainError("RECIPE_NOT_READY", "Complete fare-family extraction is not implemented for this recipe")
    if loaded.manifest.contract == 3 and (recipe.health != 'HEALTHY' or recipe.successes < 2):
        raise DomainError('RECIPE_NOT_READY', 'This recipe needs two successful real collection tests before normal scraping')
    return recipe


def source_readiness(db: Session, source: Source):
    """Derive durable recipe readiness without changing runtime backoff state."""
    try:
        recipe = gate(db, source, require_enabled=False)
        if not recipe.live_validated_at or recipe.health != "HEALTHY":
            raise DomainError("RECIPE_NOT_READY", "Recipe has not passed real fare validation")
        if recipe.manifest.get("contract") == 3:
            from udaan.db import RouteAvailability
            verified = db.scalar(select(RouteAvailability.id).where(
                RouteAvailability.recipe_id == recipe.id, RouteAvailability.active.is_(True)).limit(1))
            if not verified:
                raise DomainError("RECIPE_NOT_READY", "No route has passed real search validation")
    except DomainError as exc:
        status = {
            "RECIPE_MISSING": "NO_RECIPE",
            "RECIPE_UPGRADE_REQUIRED": "NOT_READY", "RECIPE_NOT_READY": "NOT_READY",
            "RECIPE_INVALID": "INVALID_RECIPE", "SOURCE_NOT_VALIDATED": "REGISTERED",
        }.get(exc.code, "FAILED")
        return {"status": status, "reason": exc.message}
    return {"status": "READY", "reason": "Recipe and live collection validated"}


def validate_source(db: Session, source: Source):
    result = source_readiness(db, source)
    source.status = result["status"]
    return result


def reconcile_recipe_evidence(db: Session, source: Source, recipe: Recipe):
    """Promote only evidence already persisted for this exact immutable recipe."""
    if recipe.source_id != source.id or not recipe.validated_at:
        raise DomainError("RECONCILIATION_FAILED", "Recipe identity or contract validation is missing")
    from udaan.eura import load_recipe
    load_recipe(recipe, source)  # Rechecks the current file hash and source identity.
    successful = list(db.scalars(select(Job).where(
        Job.recipe_id == recipe.id, Job.source_id == source.id, Job.state == "SUCCEEDED",
        Job.finished_at.is_not(None), Job.observation_count > 0).order_by(Job.finished_at)))
    proven = []
    for job in successful:
        stored = db.scalar(select(func.count()).select_from(Observation).where(
            Observation.job_id == job.id, Observation.recipe_id == recipe.id,
            Observation.source_id == source.id))
        success_event = db.scalar(select(JobEvent.id).where(
            JobEvent.job_id == job.id, JobEvent.kind == "JOB_SUCCEEDED").limit(1))
        if stored == job.observation_count and success_event:
            proven.append(job)
    required = 2 if recipe.manifest.get("contract") == 3 else 1
    if len(proven) < required:
        raise DomainError("RECONCILIATION_FAILED",
                          f"Only {len(proven)} persisted successful run(s) match this recipe; {required} required")
    if recipe.manifest.get("contract") == 3:
        from udaan.db import RouteAvailability
        route = db.scalar(select(RouteAvailability.id).where(
            RouteAvailability.recipe_id == recipe.id, RouteAvailability.active.is_(True)).limit(1))
        if not route:
            raise DomainError("RECONCILIATION_FAILED", "No persisted real route validation matches this recipe")
    recipe.health = "HEALTHY"
    recipe.successes = max(recipe.successes, len(proven))
    recipe.live_validated_at = max(job.finished_at for job in proven)
    source.active_recipe_id = recipe.id
    result = validate_source(db, source)
    if result["status"] != "READY":
        raise DomainError("RECONCILIATION_FAILED", result["reason"])
    return {"source": source.name, "recipe": recipe.version, "successful_runs": len(proven),
            "observations": sum(job.observation_count for job in proven),
            "validated_at": recipe.live_validated_at, "status": source.status}


def resolve_search(request: SearchRequest, instant=None):
    instant = instant or now()
    data = request.model_dump(mode="json")
    departure = request.departure_date or instant.date() + timedelta(days=request.booking_window)
    if departure < instant.date():
        raise DomainError("INVALID_DEPARTURE_DATE", "Departure date is in the past", 422)
    window = (departure - instant.date()).days
    if not 1 <= window <= 366:
        raise DomainError("INVALID_BOOKING_WINDOW", "Choose a future travel date within the next 366 days", 422)
    data.update(departure_date=departure.isoformat(), booking_window=window)
    return data


def create_job(db: Session, data: JobInput, **extra):
    source = get(db, Source, data.source_id, lock=True)
    recipe = gate(db, source)
    from udaan.discovery import require_route
    require_route(db, source, recipe, data.search)
    job = Job(source_id=source.id, recipe_id=recipe.id, request=resolve_search(data.search), **extra)
    db.add(job)
    db.flush()
    event(db, job, "JOB_QUEUED", recipe_id=recipe.id, recipe_version=recipe.version)
    return job


def create_schedule(db: Session, data: ScheduleInput):
    source = get(db, Source, data.source_id)
    recipe = gate(db, source)
    from udaan.discovery import require_route
    require_route(db, source, recipe, data.search)
    if data.execute_at < now() - timedelta(seconds=30):
        raise DomainError("INVALID_EXECUTION_TIME", "Execution time is in the past", 422)
    resolve_search(data.search, data.execute_at)
    schedule = Schedule(source_id=source.id, request=data.search.model_dump(mode="json"),
                        next_run_at=data.execute_at, cron=data.cron)
    db.add(schedule)
    db.flush()
    return schedule


def cancel_job(db: Session, job: Job):
    if job.state in TERMINAL:
        return job
    if job.state == "QUEUED":
        job.state, job.finished_at = "CANCELLED", now()
        event(db, job, "JOB_CANCELLED", cancelled_at=job.finished_at.isoformat())
    else:
        job.cancel_requested = True
        event(db, job, "CANCELLATION_REQUESTED")
    return job


def queue_operator_action(db: Session, job_id: str, data: OperatorAction):
    job = get(db, Job, job_id, lock=True)
    existing = db.scalar(select(OperatorCommand).where(
        OperatorCommand.job_id == job_id, OperatorCommand.idempotency_key == data.idempotency_key))
    if existing:
        if (existing.action, existing.challenge_id) != (data.action, data.challenge_id):
            raise DomainError("IDEMPOTENCY_CONFLICT", "Key already used for a different action")
        return existing
    if job.state != "WAITING_FOR_OPERATOR" or job.challenge_id != data.challenge_id:
        raise DomainError("STALE_OPERATOR_ACTION", "This challenge is no longer waiting for operator action")
    if db.scalar(select(func.count()).select_from(OperatorCommand).where(
        OperatorCommand.job_id == job_id, OperatorCommand.state == "PENDING")) >= 10:
        raise DomainError("ACTION_QUEUE_FULL", "Wait for pending rechecks to complete", 429)
    if data.action == "CANCEL":
        job.cancel_requested = True
    command = OperatorCommand(job_id=job_id, **data.model_dump())
    db.add(command)
    db.flush()
    return command


def dashboard(db: Session):
    counts = dict(db.execute(select(Job.state, func.count()).group_by(Job.state)).all())
    return {
        "jobs": counts,
        "observations": db.scalar(select(func.count()).select_from(__import__("udaan.db", fromlist=["Observation"]).Observation)),
        "active_sources": db.scalar(select(func.count()).select_from(Source).where(Source.enabled.is_(True), Source.archived.is_(False))),
        "recipes": dict(db.execute(select(Recipe.health, func.count()).group_by(Recipe.health)).all()),
        "latest_success": db.scalar(select(Job.finished_at).where(Job.state == "SUCCEEDED").order_by(Job.finished_at.desc()).limit(1)),
        "latest_failure": db.scalar(select(Job.finished_at).where(Job.state.in_(["FAILED", "BLOCKED"])).order_by(Job.finished_at.desc()).limit(1)),
        "recent_events": [serialize(e) for e in db.scalars(select(JobEvent).order_by(JobEvent.created_at.desc()).limit(20))],
    }


def checksum(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def create_recipe_build(db, source_id, request):
    source = get(db, Source, source_id, lock=True)
    if source.archived or not source.configuration.get("permitted_collection"):
        raise DomainError("SOURCE_NOT_VALIDATED", "Confirm collection permission in Edit → Advanced before building a recipe")
    active = db.scalar(select(Job).where(Job.source_id == source.id, Job.purpose == "RECIPE_BUILD", Job.state.in_(["QUEUED", *ACTIVE])))
    if active:
        return active
    job = Job(source_id=source.id, purpose="RECIPE_BUILD", request=resolve_search(request),
              build_base_recipe_id=source.active_recipe_id, checkpoint="Opening website")
    db.add(job)
    db.flush()
    event(db, job, "JOB_QUEUED", purpose="RECIPE_BUILD")
    return job
