"""Constrained recipe repair. Model output is data and never executable host code."""
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from datetime import timedelta

import httpx
import yaml
from sqlalchemy import func, select

from udaan.config import settings
from udaan.contracts import DomainError, Inspection, PageState, SearchRequest
from udaan.db import Job, JobEvent, Observation, Recipe, Repair, Source, event, now, session
from udaan.eura import Eura, Manifest, RecipePatch, recipe_file, register_recipe
from udaan.health import ModelProvider, command_ok, weaviate_health
from udaan.services import get


def transition(db, repair, state, reason=None):
    repair.state, repair.reason = state, reason
    repair.history = [*repair.history, {"state": state, "at": now().isoformat(), "reason": reason}]
    event(db, get(db, Job, repair.job_id), "REPAIR_TRANSITION", repair_id=repair.id, state=state, reason=reason)


class Memory:
    def client(self):
        cfg = settings()
        headers = {"Authorization": f"Bearer {cfg.weaviate_api_key.get_secret_value()}"} if cfg.weaviate_api_key else {}
        return httpx.AsyncClient(base_url=cfg.weaviate_url + "/v1/", headers=headers, timeout=5)

    async def ensure(self):
        async with self.client() as client:
            for name in ("SiteKnowledge", "RepairMemory"):
                response = await client.get(f"schema/{name}")
                if response.status_code == 404:
                    response = await client.post("schema", json={"class": name, "vectorizer": "none",
                        "properties": [{"name": "source_id", "dataType": ["text"], "tokenization": "field"},
                                       {"name": "content", "dataType": ["text"]}]})
                response.raise_for_status()

    async def search(self, source_id, error_code):
        # JSON quoting is valid GraphQL string quoting; no arbitrary query fragments are accepted.
        knowledge = []
        async with self.client() as client:
            for collection in ("SiteKnowledge", "RepairMemory"):
                query = '{Get{' + collection + '(bm25:{query:' + json.dumps(error_code) + '},where:{path:["source_id"],operator:Equal,valueText:' + json.dumps(source_id) + '},limit:2){content}}}'
                response = await client.post("graphql", json={"query": query})
                response.raise_for_status()
                payload = response.json()
                if payload.get("errors"):
                    raise DomainError("WEAVIATE_UNAVAILABLE", "Knowledge retrieval failed")
                knowledge.extend({"collection": collection, "content": item["content"]}
                                 for item in payload["data"]["Get"][collection])
        return [{**item, "content": item["content"][:700]} for item in knowledge[:3]]

    async def remember(self, source_id, kind, goal, evidence):
        allowed = {"action", "label", "tag", "role", "verified", "reason", "pattern", "repeated", "fields", "state", "version", "rows"}
        if kind not in {"control", "results", "mapping", "failure", "recipe", "repair"} or set(evidence)-allowed:
            raise DomainError("MEMORY_CONTENT_REJECTED", "Memory must contain bounded semantic evidence.")
        from urllib.parse import urlsplit
        with session() as db:
            source = get(db, Source, source_id)
            domain = urlsplit(source.base_url).hostname
        value = {"domain":domain,"kind":kind,"goal":goal[:80],"evidence":evidence}
        if len(json.dumps(value))>6000:
            raise DomainError("MEMORY_CONTENT_REJECTED", "Semantic memory exceeds its bounded size.")
        await self.store("RepairMemory" if kind in {"failure","repair"} else "SiteKnowledge", source_id, value)

    async def store(self, collection, source_id, value):
        from uuid import NAMESPACE_URL, uuid5
        content = json.dumps(value, sort_keys=True)
        identifier = str(uuid5(NAMESPACE_URL, collection+source_id+content))
        async with self.client() as client:
            exists = await client.get(f"objects/{collection}/{identifier}")
            if exists.status_code == 200:
                return
            if exists.status_code != 404:
                exists.raise_for_status()
            response = await client.post("objects", json={"class": collection, "id": identifier,
                "properties": {"source_id": source_id, "content": content}})
            if response.status_code == 422:
                check = await client.get(f"objects/{collection}/{identifier}")
                if check.status_code == 200:
                    return
            response.raise_for_status()


async def sandbox_validate(patch: dict, structure: list, mode="extraction"):
    if await command_ok("docker", "image", "inspect", settings().sandbox_image):
        args = ["docker", "run", "--rm", "-i", "--network", "none", "--read-only", "--user", "65534:65534",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "64",
                "--memory", "256m", "--cpus", "1", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
                settings().sandbox_image]
    else:
        validator = Path(__file__).resolve().parents[1] / "infra" / "validate_patch.py"
        if not validator.is_file():
            raise DomainError("SANDBOX_UNAVAILABLE", "Trusted declarative recipe validator is unavailable")
        args = [sys.executable, "-I", str(validator)]
    proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(json.dumps({"patch": patch, "structure": structure, "mode": mode}).encode()), 30)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise DomainError("SANDBOX_TIMEOUT", "Isolated candidate validation exceeded its time limit") from exc
    if proc.returncode != 0 or len(out) > 10000:
        raise DomainError("ISOLATED_TEST_FAILED", "Candidate failed isolated selector execution")
    try:
        result = json.loads(out)
        if result != {"valid": True}:
            raise ValueError("Invalid sandbox result")
    except (ValueError, TypeError) as exc:
        raise DomainError("ISOLATED_TEST_FAILED", "Sandbox returned an invalid validation result") from exc


async def repair_tick(owner):
    # Resolve real live-validation jobs before selecting another generation attempt.
    promoted = []
    with session() as db:
        for repair in db.scalars(select(Repair).where(Repair.state == "LIVE_VALIDATION")
                                 .with_for_update(skip_locked=True).limit(20)):
            validation = get(db, Job, repair.validation_job_id)
            if validation.state == "SUCCEEDED" and validation.observation_count > 0:
                required = {"GENERATED", "STATIC_VALIDATION", "ISOLATED_TEST", "CONTRACT_VALIDATION", "LIVE_VALIDATION"}
                evidence = db.scalar(select(func.count()).select_from(Observation).where(
                    Observation.job_id == validation.id, Observation.recipe_id == repair.candidate_recipe_id))
                if not required.issubset({item["state"] for item in repair.history}) or evidence != validation.observation_count:
                    transition(db, repair, "FAILED", "Complete validation history and persisted live observations are required")
                    continue
                original = get(db, Recipe, repair.recipe_id)
                candidate = get(db, Recipe, repair.candidate_recipe_id)
                source = get(db, Source, original.source_id, lock=True)
                if (source.active_recipe_id != original.id or source.archived or not source.enabled
                        or not source.configuration.get("permitted_collection")):
                    transition(db, repair, "BLOCKED", "Source or active recipe changed during validation")
                    continue
                # Re-read the immutable asset before promotion.
                raw = recipe_file(candidate.path).read_bytes()
                if hashlib.sha256(raw).hexdigest() != candidate.checksum:
                    transition(db, repair, "FAILED", "Candidate asset changed during validation")
                    continue
                transition(db, repair, "APPROVED", "All validation gates passed on real observations")
                source.active_recipe_id = candidate.id
                from udaan.services import validate_source
                validate_source(db, source)
                transition(db, repair, "PROMOTED", f"Activated recipe {candidate.version}")
                promoted.append((source.id, repair.id, repair.job_id, {
                    "error_code": get(db, Job, repair.job_id).error_code, "state": "PROMOTED",
                    "repair_id": repair.id, "candidate": repair.candidate, "recipe_version": candidate.version}))
                repair.owner, repair.lease_until = None, None
            elif validation.state in {"FAILED", "CANCELLED", "BLOCKED", "SUCCEEDED"}:
                transition(db, repair, "FAILED", "Live validation did not pass")
                repair.owner, repair.lease_until = None, None
    # Memory availability cannot roll back an already verified database promotion.
    for source_id, repair_id, job_id, value in promoted:
        try:
            await Memory().store("RepairMemory", source_id, value)
        except Exception:
            with session() as db:
                event(db, get(db, Job, job_id), "REPAIR_MEMORY_WRITE_FAILED", repair_id=repair_id,
                      reason="Promotion persisted; successful repair memory could not be indexed")
    if not settings().ai_configured:
        return
    with session() as db:
        repair = db.scalar(select(Repair).where(Repair.state == "PENDING")
                            .with_for_update(skip_locked=True).limit(1))
        if not repair:
            return
        repair.owner, repair.lease_until = owner, now() + timedelta(seconds=settings().worker_lease_seconds)
        transition(db, repair, "GENERATING")
        identifier = repair.id
        job = get(db, Job, repair.job_id)
        original = get(db, Recipe, repair.recipe_id)
        source_id, error_code = job.source_id, job.error_code
        diagnostic = db.scalar(select(JobEvent).where(JobEvent.job_id == job.id, JobEvent.kind == "STRUCTURE_DIAGNOSTIC")
                               .order_by(JobEvent.created_at.desc()).limit(1))
        structure = diagnostic.details.get("elements", []) if diagnostic else []
        original_manifest = original.manifest
    try:
        if not structure:
            raise DomainError("DIAGNOSTICS_UNAVAILABLE", "No safe structural diagnostics were captured")
        provider = ModelProvider()
        health = await provider.check()
        if health["connectivity"] != "READY":
            code = "MODEL_TIMEOUT" if "timed out" in (health.get("last_error") or "") else "MODEL_UNAVAILABLE" if health["connectivity"] == "UNREACHABLE" else "MODEL_INVALID_RESPONSE"
            raise DomainError(code, health.get("last_error") or "Repair model health check failed")
        if (await weaviate_health())["status"] != "READY":
            raise DomainError("WEAVIATE_UNAVAILABLE", "Repair memory is unavailable")
        memory = Memory()
        await memory.ensure()
        with session() as db:
            transition(db, get(db, Repair, identifier), "MEMORY")
        knowledge = await memory.search(source_id, error_code)
        await memory.store("SiteKnowledge", source_id, {"error_code": error_code, "structure": structure})
        with session() as db:
            transition(db, get(db, Repair, identifier), "ASKING_AI")
        from udaan.eura_engine import EuraEngine
        content = await EuraEngine().repair(provider, json.dumps({"error_code": error_code, "manifest": original_manifest,
                                                     "structure": structure, "previous_repairs": knowledge}))
        if not isinstance(content, str) or len(content) > 10000:
            raise DomainError("STATIC_VALIDATION_FAILED", "Model patch exceeds contract limits")
        patch = RecipePatch.model_validate_json(content)
        with session() as db:
            repair = get(db, Repair, identifier, lock=True)
            repair.candidate = patch.model_dump()
            transition(db, repair, "GENERATED")
            transition(db, repair, "STATIC_VALIDATION")
        manifest = {**original_manifest, "selectors": {**original_manifest["selectors"], **patch.selectors},
                    "stability_ms": patch.stability_ms, "version": "repair-" + identifier[:12]}
        Manifest.model_validate(manifest)
        with session() as db:
            transition(db, get(db, Repair, identifier), "ISOLATED_TEST")
        await sandbox_validate(patch.model_dump(), structure)
        with session() as db:
            repair = get(db, Repair, identifier, lock=True)
            if repair.owner != owner or repair.lease_until <= now():
                raise DomainError("WORKER_LEASE_LOST", "Repair worker ownership expired")
            transition(db, repair, "CONTRACT_VALIDATION")
            source = get(db, Source, source_id)
            directory = settings().recipe_directory / source.slug / manifest["version"]
            directory.mkdir(parents=True, exist_ok=False)
            path = directory / "manifest.yaml"
            path.write_text(yaml.safe_dump(manifest, sort_keys=False))
            candidate = register_recipe(db, source, str(path.relative_to(settings().recipe_directory)))
            repair.candidate_recipe_id = candidate.id
            original_job = get(db, Job, repair.job_id)
            from udaan.contracts import SearchRequest
            from udaan.services import resolve_search
            request = resolve_search(SearchRequest.model_validate(original_job.request))
            validation = Job(source_id=source.id, recipe_id=candidate.id, request=request, purpose="REPAIR_VALIDATION")
            db.add(validation)
            db.flush()
            repair.validation_job_id = validation.id
            event(db, validation, "JOB_QUEUED", purpose="REPAIR_VALIDATION", repair_id=repair.id)
            transition(db, repair, "LIVE_VALIDATION")
            repair.owner, repair.lease_until = None, None
        await memory.store("RepairMemory", source_id, {"error_code": error_code, "candidate": patch.model_dump(),
                                                     "state": "LIVE_VALIDATION", "repair_id": identifier})
    except Exception as exc:
        code = exc.code if isinstance(exc, DomainError) else "REPAIR_VALIDATION_FAILED"
        reason = exc.message if isinstance(exc, DomainError) else "Repair candidate failed validation"
        with session() as db:
            repair = get(db, Repair, identifier, lock=True)
            if repair.state != "LIVE_VALIDATION":
                transition(db, repair, "BLOCKED" if code.endswith("UNAVAILABLE") else "FAILED", reason)
                repair.owner, repair.lease_until = None, None
            else:
                event(db, get(db, Job, repair.job_id), "REPAIR_MEMORY_WRITE_FAILED", repair_id=repair.id,
                      reason="Candidate is awaiting live validation; memory update failed")


# New recipes share the repair pipeline's constrained data, isolation and live gates.


def build_stage(identifier, stage, **details):
    with session() as db:
        job = get(db, Job, identifier, lock=True)
        job.checkpoint = stage
        event(db, job, 'RECIPE_BUILD_STAGE', stage=stage, **details)



async def build_recipe(worker, identifier):
    from urllib.parse import urlsplit

    from udaan.browser import UdaanBrowser
    from udaan.challenges import ChallengeFlow
    from udaan.data import process_fares
    from udaan.services import ACTIVE, resolve_search

    try:
        with session() as db:
            job = get(db, Job, identifier)
            source = get(db, Source, job.source_id)
            if source.archived or not source.configuration.get('permitted_collection'):
                raise DomainError('SOURCE_NOT_VALIDATED', 'Collection permission is no longer available')
            source_id, slug, base = source.id, source.slug, source.base_url
            previous = job.build_base_recipe_id
            request = SearchRequest.model_validate(resolve_search(SearchRequest.model_validate(job.request)))
        if (request.adults, request.children, request.infants) != (1, 0, 0):
            raise DomainError('UNSUPPORTED_PASSENGERS', 'Build a recipe using one adult first')
        host = urlsplit(base).hostname
        root_host = host.removeprefix('www.')
        hosts = list(dict.fromkeys([host, root_host, 'www.' + root_host]))
        build_stage(identifier, 'Opening website')
        async with UdaanBrowser(hosts, request.resilience_profile) as browser:
            worker.browsers[identifier] = browser
            current_recipe = None

            async def inspect():
                barrier = await browser.security()
                if barrier:
                    return barrier
                if urlsplit(browser.page.url).hostname not in hosts:
                    return Inspection(state=PageState.INVALID, reason='Browser is outside the expected website')
                if current_recipe:
                    return await current_recipe.inspect(browser, request)
                controls = await browser.recipe_context()
                if controls and any(x.get('tag') in {'form', 'input', 'select', 'button'} for x in controls):
                    return Inspection(state=PageState.READY, reason='Website controls are available for inspection', checkpoint='Reading page')
                return Inspection(state=PageState.INVALID, reason='Expected website controls are not available')

            flow = ChallengeFlow(identifier, worker.owner, browser, inspect)

            async def guard():
                with session() as db:
                    job = get(db, Job, identifier)
                    source = get(db, Source, source_id)
                    if job.owner != worker.owner or job.state not in ACTIVE or job.lease_until <= now():
                        raise DomainError('WORKER_LEASE_LOST', 'Recipe worker ownership expired')
                    if job.cancel_requested:
                        raise DomainError('OPERATOR_CANCELLED', 'Recipe creation cancelled')
                    if source.archived or not source.configuration.get('permitted_collection'):
                        raise DomainError('SOURCE_NOT_VALIDATED', 'Source permission changed')
                barrier = await browser.security()
                if barrier:
                    await flow.wait(barrier)
                    return True
                return False

            await browser.navigate(base)
            await guard()
            from udaan.eura_engine import EuraRecipeBuilder
            learn = EuraRecipeBuilder().learn
            provider = ModelProvider()
            if not settings().ai_configured:
                raise DomainError('MODEL_NOT_CONFIGURED', 'AI provider is not configured.')
            if (await weaviate_health())['status'] != 'READY':
                raise DomainError('WEAVIATE_UNAVAILABLE', 'AI Memory is unavailable. Run udaan ser and retry.')
            memory = Memory()
            await memory.ensure()
            def record(kind, **details):
                with session() as db:
                    event(db, get(db, Job, identifier), kind, **details)
            def persist(value):
                with session() as db:
                    get(db, Job, identifier).recipe_candidate = value
            manifest, result_structure = await learn(browser, request, provider, memory, source_id, guard, flow,
                lambda value, **detail: build_stage(identifier, value, **detail), record, persist, sandbox_validate)
            manifest = Manifest.model_validate({**manifest.model_dump(), 'source_slug':slug, 'version':'ai-'+identifier[:12]})
            persist(manifest.model_dump())
            current_recipe = Eura(manifest, base)
            build_stage(identifier, 'Testing recipe')
            await guard()
            rows = process_fares(await current_recipe.extract(browser, request), request)
            if not rows:
                raise DomainError('RECIPE_VALIDATION_EMPTY', 'No verified fares; an empty result cannot validate a recipe')
            await guard()
            with session() as db:
                event(db, get(db, Job, identifier), 'RECIPE_VALIDATION_METRICS', cards_tested=len(rows), valid_total_fares=len(rows),
                    valid_flight_numbers=sum(bool(x.get('flight_number')) for x in rows),
                    valid_departure_times=sum(bool(x.get('extraction_metadata',{}).get('departure_time_local')) for x in rows),
                    valid_arrival_times=sum(bool(x.get('extraction_metadata',{}).get('arrival_time_local')) for x in rows),
                    route_date_verified=True)
            build_stage(identifier, 'Saving recipe')
            with session() as db:
                job = get(db, Job, identifier, lock=True)
                source = get(db, Source, source_id, lock=True)
                if job.owner != worker.owner or job.state != 'RUNNING' or job.cancel_requested or job.lease_until <= now():
                    raise DomainError('WORKER_LEASE_LOST', 'Recipe worker ownership or cancellation state changed')
                if source.active_recipe_id != previous or source.archived or not source.configuration.get('permitted_collection'):
                    raise DomainError('SOURCE_CHANGED', 'Source changed while the recipe was being tested')
                directory = settings().recipe_directory / slug / manifest.version
                directory.mkdir(parents=True, exist_ok=False)
                path = directory / 'manifest.yaml'
                path.write_text(yaml.safe_dump(manifest.model_dump(), sort_keys=False))
                recipe = register_recipe(db, source, str(path.relative_to(settings().recipe_directory)))
                instant = now()
                for row in rows:
                    db.add(Observation(job_id=job.id, source_id=source.id, recipe_id=recipe.id, collected_at=instant, **row))
                recipe.health, recipe.live_validated_at, recipe.successes = 'HEALTHY', instant, 1
                job.recipe_id, job.state, job.finished_at = recipe.id, 'SUCCEEDED', instant
                if previous is None:
                    job.observation_count, job.checkpoint, job.reason = len(rows), 'New recipe ready', 'Eura tested real fares and saved the recipe'
                    source.active_recipe_id, source.enabled = recipe.id, True
                    from udaan.services import validate_source
                    validate_source(db, source)
                    final_stage = 'Recipe ready'
                else:
                    job.observation_count, job.checkpoint, job.reason = len(rows), 'Candidate validated', 'Eura tested real fares and saved a non-active candidate'
                    final_stage = 'Candidate validated'
                event(db, job, 'RECIPE_BUILD_STAGE', stage=final_stage, recipe_id=recipe.id,
                      active=source.active_recipe_id == recipe.id)
                event(db, job, 'JOB_SUCCEEDED', observation_count=len(rows), purpose='RECIPE_BUILD')
            try:
                await memory.remember(source_id, 'recipe', 'validated recipe', {'version':manifest.version,'rows':len(rows),'state':'READY'})
            except Exception:
                with session() as db:
                    event(db, get(db, Job, identifier), 'REPAIR_MEMORY_WRITE_FAILED', reason='Recipe saved; memory update unavailable')
    except asyncio.CancelledError:
        worker.fail(identifier, 'WORKER_INTERRUPTED', 'Worker stopped during recipe creation')
        raise
    except Exception as exc:
        code = exc.code if isinstance(exc, DomainError) else 'RECIPE_BUILD_FAILED'
        reason = exc.message if isinstance(exc, DomainError) else 'Recipe creation failed its browser or validation checks'
        if code == 'SANDBOX_UNAVAILABLE':
            reason = 'Isolated recipe testing is unavailable in this container. The candidate was not activated.'
        worker.fail(identifier, code, reason)
    finally:
        worker.browsers.pop(identifier, None)
