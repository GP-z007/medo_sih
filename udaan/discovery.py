"""Versioned airline availability; reference airports never manufacture routes."""
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import func, select, update

from udaan.contracts import DomainError, Strict
from udaan.db import (
    AirportAvailability,
    AirportReference,
    DiscoveryRun,
    Job,
    Recipe,
    RouteAvailability,
    Source,
    event,
    now,
)
from udaan.services import ACTIVE, get, serialize


class DiscoveredAirport(Strict):
    iata: str = Field(pattern=r'^[A-Z]{3}$')
    airport_name: str | None = Field(None, max_length=250)
    city: str | None = Field(None, max_length=250)
    country: str | None = Field(None, pattern=r'^[A-Z]{2}$')


class DiscoveryResult(Strict):
    origins: list[DiscoveredAirport] = Field(min_length=1, max_length=2000)
    destinations: dict[str, list[DiscoveredAirport]] = Field(default_factory=dict, max_length=2000)
    origins_complete: bool = False
    routes_complete: bool = False
    reason: str = Field(max_length=500)

    @model_validator(mode='after')
    def completeness(self):
        origins = {a.iata for a in self.origins}
        if len(origins) != len(self.origins) or set(self.destinations) - origins:
            raise ValueError('Duplicate or unknown origin in discovery')
        if self.routes_complete and (not self.origins_complete or set(self.destinations) != origins):
            raise ValueError('Complete routes require every discovered origin to be checked')
        for origin, destinations in self.destinations.items():
            codes = [a.iata for a in destinations]
            if origin in codes or len(codes) != len(set(codes)):
                raise ValueError('Destination pairs must be distinct and deduplicated')
        return self


class DiscoveryInput(Strict):
    scope: Literal['AIRPORTS', 'ROUTES'] = 'ROUTES'
    recipe_id: str | None = None


def queue_discovery(db, source_id, spec):
    source = get(db, Source, source_id, lock=True)
    if source.archived or not source.configuration.get('permitted_collection'):
        raise DomainError('SOURCE_NOT_VALIDATED', 'Source collection must be authorized before discovery')
    recipe = get(db, Recipe, spec.recipe_id or source.active_recipe_id)
    from udaan.eura import load_recipe
    eura = load_recipe(recipe, source)
    if recipe.source_id != source.id or not getattr(eura.manifest, 'discovery', None):
        raise DomainError('DISCOVERY_NOT_IMPLEMENTED', 'This recipe has no validated airport/route discovery contract')
    if eura.manifest.generated:
        raise DomainError('RECIPE_UNFINISHED_CANDIDATE', 'Generated recipes must finish isolated validation first')
    existing = db.scalar(select(Job).where(Job.source_id == source.id, Job.purpose == 'DISCOVERY', Job.state.in_(['QUEUED', *ACTIVE])))
    if existing:
        return existing
    job = Job(source_id=source.id, recipe_id=recipe.id, purpose='DISCOVERY', request={'scope': spec.scope}, checkpoint='Discovering airports')
    db.add(job)
    db.flush()
    db.add(DiscoveryRun(source_id=source.id, recipe_id=recipe.id, job_id=job.id, scope=spec.scope))
    event(db, job, 'JOB_QUEUED', purpose='DISCOVERY', scope=spec.scope)
    return job


def persist_discovery(db, run, result):
    """A complete snapshot retires old entries; partial attempts cannot erase them."""
    result = DiscoveryResult.model_validate(result)
    instant = now()
    domestic_origins = []
    airports = {}
    for origin in result.origins:
        ref = db.get(AirportReference, origin.iata)
        if (origin.country or (ref.country if ref else None)) == 'IN':
            domestic_origins.append(origin.iata)
            airports[origin.iata] = origin
    routes = set()
    for origin in domestic_origins:
        for dest in result.destinations.get(origin, []):
            ref = db.get(AirportReference, dest.iata)
            if (dest.country or (ref.country if ref else None)) == 'IN':
                airports[dest.iata] = dest
                routes.add((origin, dest.iata))
    if not domestic_origins:
        raise DomainError('AIRPORT_REFERENCE_REQUIRED', 'No discovered origins could be verified as Indian airports')
    if result.origins_complete:
        db.execute(update(AirportAvailability).where(AirportAvailability.source_id == run.source_id, AirportAvailability.recipe_id == run.recipe_id).values(active=False))
    if result.routes_complete:
        db.execute(update(RouteAvailability).where(RouteAvailability.source_id == run.source_id, RouteAvailability.recipe_id == run.recipe_id).values(active=False))
    for code, airport in airports.items():
        item = db.scalar(select(AirportAvailability).where(AirportAvailability.source_id == run.source_id, AirportAvailability.recipe_id == run.recipe_id, AirportAvailability.iata == code))
        if item is None:
            item = AirportAvailability(source_id=run.source_id, recipe_id=run.recipe_id, discovery_id=run.id, iata=code)
            db.add(item)
        ref = db.get(AirportReference, code)
        item.discovery_id, item.active, item.discovered_at = run.id, True, instant
        item.is_origin = code in domestic_origins or (not result.origins_complete and bool(item.is_origin))
        item.airport_name = airport.airport_name or (ref.airport_name if ref else None)
        item.city = airport.city or (ref.city if ref else None)
        item.country = airport.country or (ref.country if ref else None)
        item.state = ref.state if ref else None
    for origin, destination in sorted(routes):
        item = db.scalar(select(RouteAvailability).where(RouteAvailability.source_id == run.source_id, RouteAvailability.recipe_id == run.recipe_id, RouteAvailability.origin == origin, RouteAvailability.destination == destination))
        if item is None:
            item = RouteAvailability(source_id=run.source_id, recipe_id=run.recipe_id, discovery_id=run.id, origin=origin, destination=destination)
            db.add(item)
        item.discovery_id, item.active, item.discovered_at = run.id, True, instant
    run.airport_count, run.route_count = len(airports), len(routes)
    run.origins_checked = len(set(domestic_origins) & set(result.destinations))
    run.complete = result.routes_complete if run.scope == 'ROUTES' else result.origins_complete
    run.state, run.finished_at, run.reason = ('SUCCEEDED' if run.complete else 'PARTIAL'), instant, result.reason
    return run


def require_route(db, source, recipe, search):
    if recipe.manifest.get('contract', 1) < 3:
        return
    available = db.scalar(select(RouteAvailability.id).where(RouteAvailability.source_id == source.id, RouteAvailability.recipe_id == recipe.id, RouteAvailability.origin == search.origin, RouteAvailability.destination == search.destination, RouteAvailability.active.is_(True)))
    if not available:
        raise DomainError('ROUTE_NOT_OFFERED', 'This route has not been verified for this recipe. Test this route or refresh route discovery first.')


def discovery_summary(db, source):
    recipe_id = source.active_recipe_id
    active = db.get(Recipe, recipe_id) if recipe_id else None
    if not recipe_id or active.manifest.get("contract", 1) < source.configuration.get("recipe_contract_minimum", 1):
        recipe_id = db.scalar(select(Recipe.id).where(Recipe.source_id == source.id).order_by(Recipe.created_at.desc()).limit(1))
    recipe = db.get(Recipe, recipe_id) if recipe_id else None
    latest = db.scalar(select(DiscoveryRun).where(DiscoveryRun.source_id == source.id, DiscoveryRun.recipe_id == recipe_id).order_by(DiscoveryRun.created_at.desc()).limit(1)) if recipe_id else None
    counts = {}
    for name, model in [('airports', AirportAvailability), ('routes', RouteAvailability)]:
        counts[name] = db.scalar(select(func.count()).select_from(model).where(model.source_id == source.id, model.recipe_id == recipe_id, model.active.is_(True))) if recipe_id else 0
    last_job = db.scalar(select(Job).where(Job.source_id == source.id, Job.purpose.in_(['COLLECTION', 'RECIPE_VALIDATION'])).order_by(Job.created_at.desc()).limit(1))
    return {**counts, 'last_scrape': last_job.finished_at if last_job else None,
            'last_error': last_job.reason if last_job and last_job.state in {'FAILED','BLOCKED'} else None, 'recipe_id': recipe_id, 'recipe_version': recipe.version if recipe else None,
            'last_checked': latest.finished_at if latest else None, 'latest_run': serialize(latest) if latest else None,
            'discovery_supported': bool(recipe and recipe.manifest.get('discovery'))}
