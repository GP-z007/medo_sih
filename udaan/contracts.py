import ipaddress
import re
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from udaan.canonical import CANONICAL_COLUMNS, FareDetails


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


_TRACKING_QUERY_KEYS = {
    "dclid", "fbclid", "gclid", "mc_cid", "mc_eid", "msclkid",
    "ref", "referrer", "source", "_ga",
}


def canonical_source_url(value) -> str:
    """Return the stable public-site identity used by every source operation."""
    raw = str(value).strip()
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlsplit(raw)
    if parsed.username or parsed.password:
        raise ValueError("Source URL must not contain credentials")
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"}:
        raise ValueError("Source URL must use HTTP or HTTPS")
    # Public websites are identified independently of an HTTP redirect to HTTPS.
    scheme = "https"
    host = (parsed.hostname or "").casefold().encode("idna").decode("ascii")
    if host.startswith("www."):
        host = host[4:]
    if not host:
        raise ValueError("Source URL must include a hostname")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Source URL has an invalid port") from exc
    netloc = f"[{host}]" if ":" in host else host
    if port not in {None, 80, 443}:
        netloc += f":{port}"
    path = parsed.path.rstrip("/")
    query = urlencode(sorted(
        (key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not (key.casefold().startswith("utm_") or key.casefold() in _TRACKING_QUERY_KEYS)
    ))
    return urlunsplit((scheme, netloc, path, query, ""))


class SourceInput(Strict):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=80)
    base_url: HttpUrl
    source_type: Literal["airline", "OTA", "institutional", "other"] = "airline"
    enabled: bool = False
    configuration: dict = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def clean_name(cls, v):
        return v.strip()

    @field_validator("base_url", mode="before")
    @classmethod
    def canonical_url(cls, v):
        return canonical_source_url(v)

    @field_validator("base_url")
    @classmethod
    def public_source(cls, v):
        p = urlsplit(str(v))
        if p.username or p.password or p.query or p.fragment:
            raise ValueError("Source URL must not contain credentials, query, or fragment")
        host = p.hostname or ""
        if host == "localhost" or host.endswith((".internal", ".local")):
            raise ValueError("Source must be a public website")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return v
        if not address.is_global:
            raise ValueError("Source must be a public website")
        return v

    @field_validator("configuration")
    @classmethod
    def config_keys(cls, v):
        allowed = {"permitted_collection", "notes", "recipe_contract_minimum", "booking_hosts"}
        if set(v) - allowed:
            raise ValueError("Supported configuration: permitted_collection, notes; no credentials")
        if "permitted_collection" in v and not isinstance(v["permitted_collection"], bool):
            raise ValueError("permitted_collection must be boolean")
        if "recipe_contract_minimum" in v and (type(v["recipe_contract_minimum"]) is not int or v["recipe_contract_minimum"] not in {1, 2, 3}):
            raise ValueError("recipe_contract_minimum must be 1, 2 or 3")
        if "booking_hosts" in v:
            hosts = v["booking_hosts"]
            if not isinstance(hosts, list) or len(hosts) > 10:
                raise ValueError("booking_hosts must be at most ten explicit public hosts")
            for host in hosts:
                if not isinstance(host, str) or not re.fullmatch(r"[a-z0-9.-]+", host) or "." not in host or host.endswith((".internal", ".local")):
                    raise ValueError("Booking hosts must be explicit public DNS names")
                try:
                    address = ipaddress.ip_address(host)
                except ValueError:
                    continue
                if not address.is_global:
                    raise ValueError("Booking hosts must be public")
        if "notes" in v and (not isinstance(v["notes"], str) or len(v["notes"]) > 500):
            raise ValueError("notes must be a string of at most 500 characters")
        return v


class SearchRequest(Strict):
    origin: str = Field(pattern=r"^[A-Z]{3}$")
    destination: str = Field(pattern=r"^[A-Z]{3}$")
    departure_date: date | None = None
    booking_window: int = Field(7, ge=1, le=366)
    adults: int = Field(1, ge=1, le=9)
    children: int = Field(0, ge=0, le=8)
    infants: int = Field(0, ge=0, le=9)
    cabin: Literal["Economy", "Premium Economy", "Business", "First"] = "Economy"
    fare_scope: Literal["All Available", "Selected Cabin"] = "All Available"
    resilience_profile: Literal["Conservative", "Standard", "Robust"] = "Standard"

    @model_validator(mode="after")
    def valid_search(self):
        if self.origin == self.destination:
            raise ValueError("Origin and destination must differ")
        if self.adults + self.children > 9 or self.infants > self.adults:
            raise ValueError("Passenger combination is invalid")
        return self


class JobInput(Strict):
    source_id: str
    search: SearchRequest


class ScheduleInput(JobInput):
    execute_at: datetime
    cron: str | None = None

    @field_validator("execute_at")
    @classmethod
    def timezone_required(cls, v):
        if v.tzinfo is None:
            raise ValueError("Execution time must include timezone")
        return v

    @model_validator(mode="after")
    def recurring(self):
        if self.cron:
            from croniter import croniter
            if len(self.cron.split()) != 5 or not croniter.is_valid(self.cron):
                raise ValueError("Use a valid five-field UTC cron expression")
            if self.search.departure_date:
                raise ValueError("Recurring searches derive departure date from booking_window")
        return self


class OperatorAction(Strict):
    action: Literal["CONFIRM", "RECHECK", "CANCEL", "OPEN_TAB"]
    challenge_id: str
    idempotency_key: str = Field(min_length=1, max_length=80)


class PageState(StrEnum):
    READY = "READY"
    CHALLENGE = "CHALLENGE"
    MANUAL_ACTION_REQUIRED = "MANUAL_ACTION_REQUIRED"
    INVALID = "INVALID"


class Inspection(Strict):
    state: PageState
    reason: str = Field(max_length=500)
    challenge_type: str | None = None
    checkpoint: str | None = None
    retry_after: datetime | None = None




class Fare(Strict):
    origin: str = Field(pattern=r"^[A-Z]{3}$")
    destination: str = Field(pattern=r"^[A-Z]{3}$")
    departure_date: date
    departure_at: datetime | None = None
    arrival_at: datetime | None = None
    airline: str = Field(min_length=1, max_length=120)
    flight_number: str | None = Field(None, max_length=60)
    cabin: str | None = Field(None, max_length=40)
    fare_class: str | None = Field(None, max_length=80)
    booking_window: int = Field(ge=0, le=366)
    base_fare: Decimal | None = Field(None, ge=0, max_digits=16, decimal_places=2)
    taxes: Decimal | None = Field(None, ge=0, max_digits=16, decimal_places=2)
    fees: Decimal | None = Field(None, ge=0, max_digits=16, decimal_places=2)
    total_fare: Decimal = Field(gt=0, max_digits=16, decimal_places=2)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    quality_flags: list[str] = Field(default_factory=list)
    extraction_metadata: dict = Field(default_factory=dict)
    details: FareDetails = Field(default_factory=FareDetails)


    @model_validator(mode="after")
    def canonical_consistency(self):
        if self.origin == self.destination:
            raise ValueError("Origin and destination must differ")
        for value in (self.departure_at, self.arrival_at):
            if value is not None and value.tzinfo is None:
                raise ValueError("Flight timestamps require an explicit timezone")
        if self.departure_at and self.arrival_at and self.arrival_at < self.departure_at:
            raise ValueError("Arrival precedes departure")
        return self


class Filter(Strict):
    column: str
    op: Literal["eq", "in", "gte", "lte", "contains"] = "eq"
    value: str | int | float | list[str | int] | None


class Aggregate(Strict):
    column: str = "total_fare"
    function: Literal["count", "min", "max", "avg", "sum"] = "avg"


class DataQuery(Strict):
    dataset: Literal["observations", "canonical", "indices"] = "observations"
    columns: list[str] = Field(default_factory=lambda: ["collected_at", "airline", "origin", "destination", "departure_date", "total_fare", "currency"] , min_length=1, max_length=80)
    filters: list[Filter] = Field(default_factory=list, max_length=20)
    sort: str = "collected_at"
    descending: bool = True
    limit: int = Field(100, ge=1, le=1000)
    offset: int = Field(0, ge=0, le=1000000)
    group_by: list[str] = Field(default_factory=list, max_length=8)
    aggregates: list[Aggregate] = Field(default_factory=list, max_length=8)
    scrape_group_id: str | None = Field(None, pattern=r"^[0-9a-f-]{36}$")

    @model_validator(mode="after")
    def canonical_defaults(self):
        if self.dataset == "canonical" and "columns" not in self.model_fields_set:
            self.columns = list(CANONICAL_COLUMNS)
        return self


class ProcessingOptions(Strict):
    base_period_start: date
    base_period_end: date | None = None


class ExportInput(Strict):
    query: DataQuery
    format: Literal["csv", "parquet"] = "csv"
    rows: int | None = Field(None, ge=1, le=10000000)
    processing: ProcessingOptions | None = None
    scope: Literal["data", "scrape_group"] = "data"
    group_id: str | None = Field(None, pattern=r"^[0-9a-f-]{36}$")

    @model_validator(mode="after")
    def explicit_scope(self):
        if self.scope == "scrape_group":
            if not self.group_id:
                raise ValueError("Scrape-group export requires a group id")
            self.query.scrape_group_id = self.group_id
        elif self.group_id or self.query.scrape_group_id:
            raise ValueError("Group membership requires scrape_group scope")
        if self.processing and (self.query.dataset not in {'observations', 'canonical'}
                                or self.query.group_by or self.query.aggregates):
            raise ValueError('Processed exports require ungrouped airfare observations')
        return self


class GroupExportInput(Strict):
    format: Literal["csv", "parquet"] = "csv"
    rows: int | None = Field(None, ge=1, le=10000000)


class InstitutionalBatch(Strict):
    provider: str = Field(min_length=1, max_length=120)
    transaction_id: str = Field(min_length=1, max_length=80)
    submitted_at: datetime
    test_only: Literal[True] = True
    fares: list[Fare] = Field(min_length=1, max_length=1000)

    @field_validator("submitted_at")
    @classmethod
    def zoned(cls, v):
        if v.tzinfo is None:
            raise ValueError("Timestamp requires timezone")
        return v


class WeightRow(Strict):
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    origin: str = Field(pattern=r"^[A-Z]{3}$")
    destination: str = Field(pattern=r"^[A-Z]{3}$")
    cabin: str
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    booking_window: Literal[1, 7, 15, 30, 45]
    quantity: Decimal = Field(gt=0)


class WeightInput(Strict):
    name: str = Field(min_length=1, max_length=120)
    provenance: str = Field(min_length=10, max_length=5000)
    approved_by: str = Field(min_length=1, max_length=120)
    rows: list[WeightRow] = Field(min_length=1, max_length=100000)


class IndexInput(Strict):
    weight_dataset_id: str
    base_period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    end_period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    currency: str = Field(pattern=r"^[A-Z]{3}$")
