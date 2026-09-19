from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import lru_cache
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from udaan.config import settings


def now() -> datetime:
    return datetime.now(timezone.utc)


def uid() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Identity:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Source(Identity, Base):
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __tablename__ = "sources"
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    base_url: Mapped[str] = mapped_column(String(1000))
    source_type: Mapped[str] = mapped_column(String(30), default="airline")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(40), default="NO_RECIPE")
    configuration: Mapped[dict] = mapped_column(JSON, default=dict)
    active_recipe_id: Mapped[str | None] = mapped_column(String(36))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Recipe(Identity, Base):
    __tablename__ = "recipes"
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    version: Mapped[str] = mapped_column(String(80))
    path: Mapped[str] = mapped_column(String(500))
    checksum: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict] = mapped_column(JSON)
    health: Mapped[str] = mapped_column(String(30), default="VALIDATING")
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    live_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    successes: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (UniqueConstraint("source_id", "version"),)


class Schedule(Identity, Base):
    __tablename__ = "schedules"
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id"))
    request: Mapped[dict] = mapped_column(JSON)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    group_request: Mapped[dict | None] = mapped_column(JSON)
    timing: Mapped[dict | None] = mapped_column(JSON)
    cron: Mapped[str | None] = mapped_column(String(100))
    booking_windows: Mapped[list | None] = mapped_column(JSON)
    last_error: Mapped[str | None] = mapped_column(String(500))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class ScrapeGroup(Identity, Base):
    __tablename__ = "scrape_groups"
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id"))
    request: Mapped[dict] = mapped_column(JSON)
    skipped: Mapped[list] = mapped_column(JSON, default=list)
    idempotency_key: Mapped[str | None] = mapped_column(String(80), unique=True)
    schedule_id: Mapped[str | None] = mapped_column(ForeignKey("schedules.id"))
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("schedule_id", "scheduled_for"),)


class Job(Identity, Base):
    __tablename__ = "jobs"
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    recipe_id: Mapped[str | None] = mapped_column(ForeignKey("recipes.id"))
    group_id: Mapped[str | None] = mapped_column(ForeignKey("scrape_groups.id"), index=True)
    group_window: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    network_profile: Mapped[str] = mapped_column(String(64), default="automatic", server_default="automatic")
    recipe_candidate: Mapped[dict | None] = mapped_column(JSON)
    build_base_recipe_id: Mapped[str | None] = mapped_column(String(36))
    schedule_id: Mapped[str | None] = mapped_column(ForeignKey("schedules.id"))
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_of: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"))
    state: Mapped[str] = mapped_column(String(40), default="QUEUED", index=True)
    request: Mapped[dict] = mapped_column(JSON)
    checkpoint: Mapped[str] = mapped_column(String(80), default="search")
    owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wait_used_seconds: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=0)
    wait_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    challenge_id: Mapped[str | None] = mapped_column(String(36))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    reason: Mapped[str | None] = mapped_column(String(500))
    observation_count: Mapped[int] = mapped_column(Integer, default=0)
    purpose: Mapped[str] = mapped_column(String(30), default="COLLECTION")
    __table_args__ = (UniqueConstraint("schedule_id", "scheduled_for", "source_id", "group_window", name="jobs_schedule_source_window_key"),)


class JobEvent(Identity, Base):
    __tablename__ = "job_events"
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(60))
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class OperatorCommand(Identity, Base):
    __tablename__ = "operator_commands"
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    challenge_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(20))
    idempotency_key: Mapped[str] = mapped_column(String(80))
    state: Mapped[str] = mapped_column(String(30), default="PENDING")
    result: Mapped[str | None] = mapped_column(String(500))
    __table_args__ = (UniqueConstraint("job_id", "idempotency_key"),)


class Observation(Base):
    __tablename__ = "observations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, default=now)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    group_id: Mapped[str | None] = mapped_column(ForeignKey("scrape_groups.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    recipe_id: Mapped[str] = mapped_column(ForeignKey("recipes.id"))
    fingerprint: Mapped[str] = mapped_column(String(64))
    origin: Mapped[str] = mapped_column(String(3))
    destination: Mapped[str] = mapped_column(String(3))
    departure_date: Mapped[date] = mapped_column(Date)
    departure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    arrival_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    airline: Mapped[str] = mapped_column(String(120))
    flight_number: Mapped[str | None] = mapped_column(String(60))
    cabin: Mapped[str | None] = mapped_column(String(40))
    fare_class: Mapped[str | None] = mapped_column(String(80))
    booking_window: Mapped[int] = mapped_column(Integer)
    base_fare: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    taxes: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    fees: Mapped[Decimal | None] = mapped_column(Numeric(16, 2))
    total_fare: Mapped[Decimal] = mapped_column(Numeric(16, 2))
    currency: Mapped[str] = mapped_column(String(3))
    quality_flags: Mapped[list] = mapped_column(JSON, default=list)
    extraction_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    details: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    __table_args__ = (
        CheckConstraint("total_fare > 0", name="positive_fare"),
        CheckConstraint("origin <> destination", name="distinct_airports"),
        UniqueConstraint("job_id", "fingerprint", "collected_at"),
        # TimescaleDB creates this descending time index during hypertable conversion.
        Index("observations_collected_at_idx", collected_at.desc()),
        Index("ix_observation_route", "origin", "destination", "departure_date", "booking_window"),
        Index("ix_observation_source_time", "source_id", "collected_at"),
    )


class AirportReference(Base):
    __tablename__ = "airport_reference"
    iata: Mapped[str] = mapped_column(String(3), primary_key=True)
    airport_name: Mapped[str] = mapped_column(String(250))
    city: Mapped[str | None] = mapped_column(String(250))
    state: Mapped[str | None] = mapped_column(String(250))
    country: Mapped[str] = mapped_column(String(2))
    timezone: Mapped[str | None] = mapped_column(String(80))
    reference_url: Mapped[str] = mapped_column(String(1000))
    reference_checksum: Mapped[str] = mapped_column(String(64))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DiscoveryRun(Identity, Base):
    __tablename__ = "discovery_runs"
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    recipe_id: Mapped[str] = mapped_column(ForeignKey("recipes.id"))
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    scope: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(30), default="QUEUED")
    complete: Mapped[bool] = mapped_column(Boolean, default=False)
    airport_count: Mapped[int] = mapped_column(Integer, default=0)
    route_count: Mapped[int] = mapped_column(Integer, default=0)
    origins_checked: Mapped[int] = mapped_column(Integer, default=0)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(500))


class AirportAvailability(Identity, Base):
    __tablename__ = "airport_availability"
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    recipe_id: Mapped[str] = mapped_column(ForeignKey("recipes.id"))
    discovery_id: Mapped[str] = mapped_column(ForeignKey("discovery_runs.id"))
    iata: Mapped[str] = mapped_column(String(3))
    airport_name: Mapped[str | None] = mapped_column(String(250))
    city: Mapped[str | None] = mapped_column(String(250))
    state: Mapped[str | None] = mapped_column(String(250))
    country: Mapped[str | None] = mapped_column(String(2))
    is_origin: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("source_id","recipe_id","iata"),)


class RouteAvailability(Identity, Base):
    __tablename__ = "route_availability"
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    recipe_id: Mapped[str] = mapped_column(ForeignKey("recipes.id"))
    discovery_id: Mapped[str] = mapped_column(ForeignKey("discovery_runs.id"))
    origin: Mapped[str] = mapped_column(String(3))
    destination: Mapped[str] = mapped_column(String(3))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("source_id","recipe_id","origin","destination"),
                     CheckConstraint("origin <> destination",name="discovered_distinct_airports"),)


class Export(Identity, Base):
    __tablename__ = "exports"
    query: Mapped[dict] = mapped_column(JSON)
    scope: Mapped[str] = mapped_column(String(20), default="data", server_default="data")
    group_id: Mapped[str | None] = mapped_column(ForeignKey("scrape_groups.id"))
    format: Mapped[str] = mapped_column(String(10))
    state: Mapped[str] = mapped_column(String(30), default="QUEUED")
    matched_count: Mapped[int | None] = mapped_column(Integer)
    matched_source_count: Mapped[int | None] = mapped_column(Integer)
    matched_job_count: Mapped[int | None] = mapped_column(Integer)
    exported_count: Mapped[int] = mapped_column(Integer, default=0)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    path: Mapped[str | None] = mapped_column(String(500))
    error: Mapped[str | None] = mapped_column(String(300))
    owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Submission(Identity, Base):
    __tablename__ = "institutional_submissions"
    provider: Mapped[str] = mapped_column(String(120))
    transaction_id: Mapped[str] = mapped_column(String(80))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    test_only: Mapped[bool] = mapped_column(Boolean, default=True)
    payload: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(30), default="ACCEPTED_TEST_ONLY")
    __table_args__ = (UniqueConstraint("provider", "transaction_id"), CheckConstraint("test_only = true"))


class WeightDataset(Identity, Base):
    __tablename__ = "weight_datasets"
    name: Mapped[str] = mapped_column(String(120))
    provenance: Mapped[str] = mapped_column(Text)
    approved_by: Mapped[str] = mapped_column(String(120))
    checksum: Mapped[str] = mapped_column(String(64), unique=True)
    rows: Mapped[list] = mapped_column(JSON)


class IndexRun(Identity, Base):
    __tablename__ = "index_series"
    weight_dataset_id: Mapped[str] = mapped_column(ForeignKey("weight_datasets.id"))
    methodology: Mapped[str] = mapped_column(String(80))
    base_period: Mapped[str] = mapped_column(String(7))
    currency: Mapped[str] = mapped_column(String(3))
    values: Mapped[list] = mapped_column(JSON)
    inputs: Mapped[dict] = mapped_column(JSON)


class Repair(Identity, Base):
    __tablename__ = "repair_attempts"
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), unique=True)
    recipe_id: Mapped[str] = mapped_column(ForeignKey("recipes.id"))
    state: Mapped[str] = mapped_column(String(50), default="PENDING")
    candidate: Mapped[dict | None] = mapped_column(JSON)
    history: Mapped[list] = mapped_column(JSON, default=list)
    reason: Mapped[str | None] = mapped_column(String(300))
    candidate_recipe_id: Mapped[str | None] = mapped_column(ForeignKey("recipes.id"))
    validation_job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"))
    owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


@lru_cache
def engine():
    return create_engine(settings().database_url.get_secret_value(), pool_pre_ping=True, connect_args={"connect_timeout": 3})


@contextmanager
def session():
    with Session(engine(), expire_on_commit=False) as db:
        with db.begin():
            yield db


def event(db: Session, job: Job, kind: str, **details):
    item = JobEvent(job_id=job.id, kind=kind, details=details)
    db.add(item)
    db.flush()
    return item


class WorkerStatus(Base):
    __tablename__ = "worker_status"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(30), default="RUNNING")
