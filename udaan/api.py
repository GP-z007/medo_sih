import asyncio
import hmac
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from udaan.challenges import elapsed
from udaan.config import RESILIENCE, settings
from udaan.contracts import (
    DataQuery,
    DomainError,
    ExportInput,
    GroupExportInput,
    IndexInput,
    InstitutionalBatch,
    JobInput,
    OperatorAction,
    ScheduleInput,
    SearchRequest,
    SourceInput,
    Strict,
    WeightInput,
    canonical_source_url,
)
from udaan.data import build_query, dataset, query_data
from udaan.db import (
    Export,
    IndexRun,
    Job,
    JobEvent,
    Recipe,
    Repair,
    Schedule,
    Source,
    Submission,
    WeightDataset,
    now,
    session,
)
from udaan.discovery import DiscoveryInput
from udaan.eura import register_recipe
from udaan.groups import GroupInput, GroupScheduleInput, create_group, create_group_schedule, group_detail
from udaan.health import HealthMonitor
from udaan.processing_service import ProcessingInput, process_database_rows
from udaan.services import (
    cancel_job,
    checksum,
    create_job,
    create_schedule,
    dashboard,
    get,
    queue_operator_action,
    register_source,
    serialize,
    validate_source,
)


def database():
    with session() as db:
        yield db


class RecipeInput(Strict):
    source_id: str
    path: str = Field(max_length=500)


class AttachInput(Strict):
    recipe_id: str


def create_app():
    monitor = HealthMonitor()

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(monitor.loop())
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(title="Udaan", version="0.1.0", lifespan=lifespan)
    app.state.monitor = monitor

    @app.middleware("http")
    async def access_and_size(request: Request, call_next):
        secret = settings().api_token
        if secret and not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + secret.get_secret_value()):
            return JSONResponse({"code": "UNAUTHORIZED", "message": "API authentication required"}, status_code=401)
        if request.method in {"POST", "PUT", "PATCH"}:
            length = request.headers.get("content-length")
            if length and (not length.isdigit() or int(length) > 2_000_000):
                return JSONResponse({"code": "REQUEST_TOO_LARGE"}, status_code=413)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 2_000_000:
                    return JSONResponse({"code": "REQUEST_TOO_LARGE"}, status_code=413)
            request._body = bytes(body)
        return await call_next(request)

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"code": "VALIDATION_FAILED", "errors": [
            {"location": list(e["loc"]), "message": e["msg"].split("input_value=")[0]} for e in exc.errors()]}, status_code=422)

    @app.exception_handler(IntegrityError)
    async def conflict(request, exc):
        return JSONResponse({"code": "CONSTRAINT_CONFLICT", "message": "Record conflicts with existing data"}, status_code=409)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        return JSONResponse({"code": "DATABASE_UNAVAILABLE", "message": "Database operation failed; check connectivity and migrations"}, status_code=503)

    @app.get("/api/v1/health")
    def health():
        return {"api": "READY", **monitor.snapshot()}

    @app.post("/api/v1/health/refresh", status_code=202)
    async def refresh_health():
        # Async checks do not block TUI requests or deterministic collection.
        async def checks():
            await asyncio.gather(monitor.check_dependencies(), monitor.model_provider.check())
        settings().require_ai()
        monitor.model_provider.status = monitor.model_provider.snapshot("CHECKING")
        previous = getattr(app.state, "refresh_task", None)
        if previous is None or previous.done():
            app.state.refresh_task = asyncio.create_task(checks())
        return {"status": "CHECKING"}

    @app.get("/api/v1/dashboard")
    def dashboard_data(db=Depends(database)):
        return dashboard(db)

    @app.get("/api/v1/settings")
    def configuration():
        cfg = settings()
        return {"api_url": cfg.api_url, "browser_view_url": cfg.browser_view_url, "display": cfg.display,
                "operator_wait_seconds": cfg.operator_wait_seconds, "collection_concurrency": cfg.collection_concurrency,
                "resilience_profiles": RESILIENCE, "export_directory": str(cfg.export_directory),
                "configuration_source": ".env / process environment; restart services after changes",
                "ai_model": cfg.ai_model, "ai_base_url": cfg.ai_base_url,
                "ai_timeout_seconds": cfg.ai_timeout_seconds, "weaviate_url": cfg.weaviate_url,
                "ai_provider": cfg.ai_provider, "ai_provider_name": cfg.ai_provider_name,
                "ai_key_configured": bool(cfg.ai_api_key), "ai_configured": cfg.ai_configured,
                "ai_status": monitor.model_provider.status["connectivity"]}

    def list_records(db, model, limit, offset):
        return {"total": db.scalar(select(func.count()).select_from(model)),
                "items": [serialize(x) for x in db.scalars(select(model).order_by(model.created_at.desc(), model.id)
                                                           .limit(limit).offset(offset))]}

    @app.get("/api/v1/sources")
    def sources(limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0), include_archived: bool = False, db=Depends(database)):
        where = [] if include_archived else [Source.archived.is_(False)]
        records = list(db.scalars(select(Source).where(*where).order_by(Source.name, Source.id).limit(limit).offset(offset)))
        from udaan.services import source_readiness
        result = {'total':db.scalar(select(func.count()).select_from(Source).where(*where)), 'items':[]}
        for source in records:
            item = serialize(source)
            readiness = source_readiness(db, source)
            item["recipe_status"] = readiness["status"]
            item["recipe_status_reason"] = readiness["reason"]
            item["runtime_status"] = ("BACKING_OFF" if source.cooldown_until and source.cooldown_until > now()
                                      else "BLOCKED" if source.status == "BLOCKED" else "READY")
            item["status"] = item["runtime_status"] if item["runtime_status"] != "READY" else readiness["status"]
            result['items'].append(item)
        versions = dict(db.execute(select(Recipe.id, Recipe.version).where(Recipe.id.in_([item["active_recipe_id"] for item in result["items"] if item["active_recipe_id"]]))).all())
        for item in result["items"]:
            item["recipe_version"] = versions.get(item["active_recipe_id"])
            active_recipe = db.get(Recipe, item['active_recipe_id']) if item['active_recipe_id'] else None
            families = (active_recipe.manifest.get('families') or {}) if active_recipe else {}
            item['cabins'] = sorted(set(families.get('cabin_names', {}).values()))
            recent = db.scalars(select(Job).where(Job.source_id == item["id"]).order_by(Job.created_at.desc()).limit(5)).all()
            item["recent_jobs"] = [serialize(x) for x in recent]
            item["last_success"] = db.scalar(select(Job.finished_at).where(Job.source_id == item["id"], Job.state == "SUCCEEDED").order_by(Job.finished_at.desc()).limit(1))
        return result

    @app.get("/api/v1/sources/{identifier}/discovery")
    def source_discovery(identifier: str, db=Depends(database)):
        from udaan.discovery import discovery_summary
        return discovery_summary(db, get(db, Source, identifier))

    @app.post("/api/v1/sources/{identifier}/discovery", status_code=202)
    def refresh_discovery(identifier: str, data: DiscoveryInput, db=Depends(database)):
        from udaan.discovery import queue_discovery
        return serialize(queue_discovery(db, identifier, data))

    @app.get("/api/v1/sources/{identifier}/routes")
    def source_routes(identifier: str, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0), db=Depends(database)):
        from udaan.db import RouteAvailability
        source = get(db, Source, identifier)
        where = (RouteAvailability.source_id == source.id, RouteAvailability.recipe_id == source.active_recipe_id, RouteAvailability.active.is_(True))
        return {"matched_count": db.scalar(select(func.count()).select_from(RouteAvailability).where(*where)),
                "items": [serialize(x) for x in db.scalars(select(RouteAvailability).where(*where).order_by(RouteAvailability.origin, RouteAvailability.destination, RouteAvailability.id).limit(limit).offset(offset))]}

    @app.post("/api/v1/sources", status_code=201)
    def source_add(data: SourceInput, response: Response, db=Depends(database)):
        source, registration = register_source(db, data)
        if not registration["created"]:
            response.status_code = 200
        readiness = registration.pop("readiness")
        return {
            **serialize(source),
            **registration,
            "source_id": source.id,
            "source_status": source.status,
            "status": "READY" if readiness["status"] == "READY" else "RECIPE_NOT_READY",
            "recipe_status": readiness["status"],
            "recipe_status_reason": readiness["reason"],
        }

    @app.put("/api/v1/sources/{identifier}")
    def source_edit(identifier: str, data: SourceInput, db=Depends(database)):
        source = get(db, Source, identifier, lock=True)
        if source.active_recipe_id and (source.slug != data.slug or canonical_source_url(source.base_url) != canonical_source_url(data.base_url)):
            raise DomainError("SOURCE_IDENTITY_LOCKED", "Create another source to change an identity with an attached recipe")
        values = data.model_dump(mode="json")
        values["base_url"] = canonical_source_url(values["base_url"])
        for key, value in values.items():
            setattr(source, key, value)
        validate_source(db, source)
        return serialize(source)

    @app.post("/api/v1/sources/{identifier}/build-recipe", status_code=202)
    def build_recipe(identifier: str, search: SearchRequest, db=Depends(database)):
        settings().require_ai()
        from udaan.services import create_recipe_build
        return serialize(create_recipe_build(db, identifier, search))

    @app.get("/api/v1/sources/{identifier}/recipe-status")
    def recipe_status(identifier: str, db=Depends(database)):
        from udaan.eura_engine import recipe_summary
        return recipe_summary(db, get(db, Source, identifier))

    @app.post("/api/v1/sources/{identifier}/repair-recipe", status_code=202)
    def repair_source(identifier: str, db=Depends(database)):
        settings().require_ai()
        source = get(db, Source, identifier, lock=True)
        if source.archived or not source.enabled or not source.configuration.get("permitted_collection"):
            raise DomainError("SOURCE_NOT_VALIDATED", "Enable this source and confirm collection permission before repairing.")
        original = get(db, Recipe, source.active_recipe_id) if source.active_recipe_id else None
        if not original or original.health != "DEGRADED":
            raise DomainError("RECIPE_NOT_DEGRADED", "Test the current recipe first to capture its failure.")
        failure = db.scalar(select(Job).where(Job.source_id == source.id, Job.recipe_id == original.id,
            Job.error_code.in_(["EXTRACTION_STRUCTURE_FAILED", "EXTRACTION_SCHEMA_FAILED"]),
            Job.purpose != "REPAIR_VALIDATION").order_by(Job.created_at.desc()).limit(1))
        if not failure:
            raise DomainError("DIAGNOSTICS_UNAVAILABLE", "Test the recipe first so Eura can read the current failure.")
        item = db.scalar(select(Repair).where(Repair.job_id == failure.id))
        if item is None:
            item = Repair(job_id=failure.id, recipe_id=original.id)
            db.add(item)
            db.flush()
        elif item.state in {"FAILED", "BLOCKED"}:
            from udaan.agent import transition
            transition(db, item, "PENDING")
        return serialize(item)

    @app.post("/api/v1/sources/{identifier}/validate")
    def source_validate(identifier: str, db=Depends(database)):
        return validate_source(db, get(db, Source, identifier, lock=True))

    @app.post("/api/v1/sources/{identifier}/archive")
    def source_archive(identifier: str, db=Depends(database)):
        source = get(db, Source, identifier, lock=True)
        source.archived, source.enabled, source.status = True, False, "ARCHIVED"
        return serialize(source)

    @app.post("/api/v1/sources/{identifier}/recipe")
    def attach(identifier: str, data: AttachInput, db=Depends(database)):
        source = get(db, Source, identifier, lock=True)
        recipe = get(db, Recipe, data.recipe_id)
        from udaan.eura import attachment_problem
        problem = attachment_problem(recipe, source)
        if problem:
            raise DomainError(*problem)
        source.active_recipe_id = recipe.id
        validate_source(db, source)
        return serialize(source)

    @app.get("/api/v1/sources/{identifier}/recipes")
    def source_recipes(identifier: str, db=Depends(database)):
        from udaan.eura import attachment_problem
        source = get(db, Source, identifier)
        items = []
        for recipe in db.scalars(select(Recipe).order_by(Recipe.created_at.desc(), Recipe.id).limit(1000)):
            problem = attachment_problem(recipe, source)
            items.append({**serialize(recipe), "selectable": problem is None,
                          "compatibility_code": problem[0] if problem else None,
                          "compatibility_reason": problem[1] if problem else "Ready"})
        return {"items": items}

    @app.get("/api/v1/recipes")
    def recipes(limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0), db=Depends(database)):
        return list_records(db, Recipe, limit, offset)

    @app.post("/api/v1/recipes/{identifier}/test", status_code=202)
    def test_registered_recipe(identifier: str, search: SearchRequest, db=Depends(database)):
        from udaan.db import event
        from udaan.eura import load_recipe
        from udaan.services import resolve_search
        recipe = get(db, Recipe, identifier)
        source = get(db, Source, recipe.source_id, lock=True)
        if source.archived or not source.configuration.get("permitted_collection"):
            raise DomainError("SOURCE_NOT_VALIDATED", "Collection permission is required to test this recipe.")
        contract = load_recipe(recipe, source)
        if contract.manifest.generated and not recipe.live_validated_at:
            raise DomainError("RECIPE_UNFINISHED_CANDIDATE", "AI candidates must finish the isolated build pipeline before they can be used.")
        job = Job(source_id=source.id, recipe_id=recipe.id, request=resolve_search(search), purpose="RECIPE_VALIDATION")
        db.add(job)
        db.flush()
        event(db, job, "JOB_QUEUED", purpose="RECIPE_VALIDATION", recipe_id=recipe.id)
        return serialize(job)

    @app.get("/api/v1/recipes/assets")
    def recipe_assets():
        root = settings().recipe_directory
        return {"paths": sorted(str(p.relative_to(root)) for p in root.rglob("*.yaml") if p.is_file())[:1000]}

    @app.post("/api/v1/recipes", status_code=201)
    def recipe_add(data: RecipeInput, db=Depends(database)):
        return serialize(register_recipe(db, get(db, Source, data.source_id), data.path))

    @app.post("/api/v1/scrape-groups", status_code=201)
    def group_add(data: GroupInput, db=Depends(database)):
        return group_detail(db, create_group(db, data))

    @app.get("/api/v1/scrape-groups/{identifier}")
    def group_get(identifier: str, db=Depends(database)):
        from udaan.db import ScrapeGroup
        return group_detail(db, get(db, ScrapeGroup, identifier))

    @app.post("/api/v1/scrape-groups/{identifier}/export", status_code=202)
    def group_export(identifier: str, data: GroupExportInput, db=Depends(database)):
        from udaan.db import ScrapeGroup
        get(db, ScrapeGroup, identifier)
        spec = ExportInput(query=DataQuery(dataset="canonical", scrape_group_id=identifier),
                           format=data.format, rows=data.rows, scope="scrape_group", group_id=identifier)
        build_query(spec.query)
        item = Export(query=spec.model_dump(mode="json"), format=spec.format,
                      scope=spec.scope, group_id=identifier)
        db.add(item)
        db.flush()
        return serialize(item)

    @app.post("/api/v1/group-schedules", status_code=201)
    def group_schedule_add(data: GroupScheduleInput, db=Depends(database)):
        return serialize(create_group_schedule(db, data))

    @app.get("/api/v1/jobs")
    def jobs(limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0), db=Depends(database)):
        result = list_records(db, Job, limit, offset)
        names = dict(db.execute(select(Source.id, Source.name)).all())
        for item in result["items"]:
            item["source"] = names[item["source_id"]]
        return result

    @app.post("/api/v1/jobs", status_code=201)
    def job_add(data: JobInput, db=Depends(database)):
        return serialize(create_job(db, data))

    @app.get("/api/v1/jobs/{identifier}")
    def job_detail(identifier: str, db=Depends(database)):
        job = get(db, Job, identifier)
        return {**serialize(job), "source": get(db, Source, job.source_id).name,
                "wait_elapsed_seconds": elapsed(job),
                "wait_remaining_seconds": max(0, settings().operator_wait_seconds - elapsed(job)),
                "browser_view_url": settings().browser_view_url,
                "events": [serialize(x) for x in db.scalars(select(JobEvent).where(JobEvent.job_id == identifier)
                                                            .order_by(JobEvent.created_at, JobEvent.id).limit(500))]}

    @app.post("/api/v1/jobs/{identifier}/operator-action", status_code=202)
    def operator_action(identifier: str, data: OperatorAction, db=Depends(database)):
        return serialize(queue_operator_action(db, identifier, data))

    @app.post("/api/v1/jobs/{identifier}/cancel")
    def job_cancel(identifier: str, db=Depends(database)):
        return serialize(cancel_job(db, get(db, Job, identifier, lock=True)))

    @app.post("/api/v1/jobs/{identifier}/retry", status_code=201)
    def job_retry(identifier: str, db=Depends(database)):
        job = get(db, Job, identifier)
        if job.state not in {"FAILED", "CANCELLED", "BLOCKED"}:
            raise DomainError("INVALID_JOB_STATE", "Only terminal unsuccessful jobs may be retried")
        if job.purpose == "RECIPE_BUILD":
            from udaan.services import create_recipe_build
            settings().require_ai()
            retry = create_recipe_build(db, job.source_id, SearchRequest.model_validate(job.request))
            retry.retry_of = job.id
            return serialize(retry)
        if job.purpose == "RECIPE_VALIDATION":
            result = test_registered_recipe(job.recipe_id, SearchRequest.model_validate(job.request), db)
            retry = get(db, Job, result['id'])
            retry.retry_of = job.id
            return serialize(retry)
        if job.purpose == "DISCOVERY":
            from udaan.discovery import queue_discovery
            retry = queue_discovery(db, job.source_id, DiscoveryInput(scope=job.request["scope"], recipe_id=job.recipe_id))
            retry.retry_of = job.id
            return serialize(retry)
        return serialize(create_job(db, JobInput(source_id=job.source_id, search=job.request), retry_of=job.id, group_id=job.group_id, group_window=job.group_window, network_profile=job.network_profile))

    @app.get("/api/v1/schedules")
    def schedules(db=Depends(database)):
        return list_records(db, Schedule, 1000, 0)

    @app.post("/api/v1/schedules", status_code=201)
    def schedule_add(data: ScheduleInput, db=Depends(database)):
        return serialize(create_schedule(db, data))

    @app.post("/api/v1/schedules/{identifier}/disable")
    def schedule_disable(identifier: str, db=Depends(database)):
        item = get(db, Schedule, identifier, lock=True)
        item.enabled = False
        return serialize(item)

    @app.get("/api/v1/network-profiles")
    def network_profiles():
        from udaan.global_ip import GlobalIPDataset
        return GlobalIPDataset().status()

    @app.get("/api/v1/data/schema")
    def data_schema():
        return {name: list(dataset(DataQuery(dataset=name))[0]) for name in ("observations", "canonical", "indices")}

    @app.post("/api/v1/data/query")
    def data_query(query: DataQuery, db=Depends(database)):
        return query_data(db, query)

    @app.post("/api/v1/data/process")
    def data_process(data: ProcessingInput, db=Depends(database)):
        return process_database_rows(db, data)

    @app.get("/api/v1/data/canonical-schema")
    def canonical_schema():
        from udaan.canonical import CANONICAL_COLUMNS
        return {"version":1,"columns":CANONICAL_COLUMNS,"unavailable":None,"row":"flight × fare family"}

    @app.get("/api/v1/observations")
    def observations(limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0), db=Depends(database)):
        return query_data(db, DataQuery(limit=limit, offset=offset))

    @app.post("/api/v1/data/export", status_code=202)
    def data_export(spec: ExportInput, db=Depends(database)):
        if spec.query.dataset == 'canonical' and not (spec.query.group_by or spec.query.aggregates):
            from udaan.canonical import CANONICAL_COLUMNS
            spec.query.columns=list(CANONICAL_COLUMNS)
        build_query(spec.query)
        if spec.scope == "scrape_group":
            from udaan.db import ScrapeGroup
            get(db, ScrapeGroup, spec.group_id)
        item = Export(query=spec.model_dump(mode="json"), format=spec.format, scope=spec.scope, group_id=spec.group_id)
        db.add(item)
        db.flush()
        return serialize(item)

    @app.get("/api/v1/exports")
    def exports(db=Depends(database)):
        return list_records(db, Export, 100, 0)

    @app.get("/api/v1/exports/{identifier}/download")
    def export_download(identifier: str, db=Depends(database)):
        item = get(db, Export, identifier)
        if item.state != "SUCCEEDED" or not item.path:
            raise DomainError("EXPORT_NOT_READY", "Export has not completed")
        path = Path(item.path).resolve()
        if not path.is_relative_to(settings().export_directory.resolve()) or not path.is_file():
            raise DomainError("EXPORT_UNAVAILABLE", "Export artifact is unavailable", 404)
        return FileResponse(path, filename=path.name, media_type="text/csv" if item.format == "csv" else "application/vnd.apache.parquet")

    @app.get("/api/v1/weights")
    def weights(db=Depends(database)):
        return list_records(db, WeightDataset, 100, 0)

    @app.post("/api/v1/weights", status_code=201)
    def weights_add(data: WeightInput, db=Depends(database)):
        from udaan.index import import_weights
        return serialize(import_weights(db, data))

    @app.get("/api/v1/indices")
    def indices(db=Depends(database)):
        return list_records(db, IndexRun, 100, 0)

    @app.post("/api/v1/indices", status_code=201)
    def indices_calculate(data: IndexInput, db=Depends(database)):
        from udaan.index import calculate
        return serialize(calculate(db, data))

    @app.get("/api/v1/repairs")
    def repairs(db=Depends(database)):
        return list_records(db, Repair, 100, 0)

    @app.get("/api/v1/repairs/{identifier}")
    def repair_details(identifier: str, db=Depends(database)):
        return serialize(get(db, Repair, identifier))

    @app.post("/api/v1/repairs/{identifier}/retry")
    def repair_retry(identifier: str, db=Depends(database)):
        item = get(db, Repair, identifier, lock=True)
        if item.state not in {"BLOCKED", "FAILED"}:
            raise DomainError("INVALID_REPAIR_STATE", "Only blocked or failed repairs can be retried")
        settings().require_ai()
        from udaan.agent import transition
        transition(db, item, "PENDING")
        return serialize(item)

    @app.get("/api/v1/activity")
    def activity(db=Depends(database)):
        return list_records(db, JobEvent, 100, 0)

    @app.get("/institutional/v1/health")
    def gateway_health():
        return {"status": "FUTURE / DEMONSTRATION", "production_partners": [], "test_only": True}

    @app.get("/institutional/v1/schema")
    def gateway_schema():
        return InstitutionalBatch.model_json_schema()

    @app.post("/institutional/v1/fares", status_code=201)
    @app.post("/institutional/v1/batches", status_code=201)
    def submit(data: InstitutionalBatch, db=Depends(database)):
        db.execute(__import__("sqlalchemy").text("SELECT pg_advisory_xact_lock(82422)"))
        payload = data.model_dump(mode="json")
        existing = db.scalar(select(Submission).where(Submission.provider == data.provider, Submission.transaction_id == data.transaction_id))
        if existing:
            if checksum(existing.payload) != checksum(payload):
                raise DomainError("IDEMPOTENCY_CONFLICT", "Transaction already contains a different submission")
            return serialize(existing)
        item = Submission(provider=data.provider, transaction_id=data.transaction_id,
                          submitted_at=data.submitted_at, payload=payload, test_only=True)
        db.add(item)
        db.flush()
        return serialize(item)

    @app.get("/institutional/v1/submissions")
    def submissions(db=Depends(database)):
        return list_records(db, Submission, 100, 0)

    @app.get("/institutional/v1/submissions/{identifier}")
    def submission(identifier: str, db=Depends(database)):
        return serialize(get(db, Submission, identifier))

    return app
