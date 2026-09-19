"""Common discovery and flight × fare-family contract for deterministic Eura."""
from typing import Literal

from pydantic import Field, field_validator

from udaan.contracts import Strict


def selector(value):
    from lxml.cssselect import CSSSelector
    if not isinstance(value, str) or not 1 <= len(value) <= 500:
        raise ValueError('Selector must be bounded CSS')
    CSSSelector(value)
    return value


class DiscoveryContract(Strict):
    mode: Literal['native_select', 'autocomplete_scroll', 'autocomplete_catalog', 'indigo_stations', 'validated_routes']
    catalog_url: str | None = Field(None, max_length=500)
    catalog_path: list[str] = Field(default_factory=list, max_length=8)
    catalog_fields: dict[str, str] = Field(default_factory=dict, max_length=4)
    origin: str
    destination: str
    origin_index: int = Field(0, ge=0, le=4)
    destination_index: int = Field(0, ge=0, le=4)
    country_filter: str | None = Field(None, max_length=60)
    options: str | None = None
    listbox: str | None = None
    code: str | None = None
    airport_name: str | None = None
    city: str | None = None
    consent: str | None = None
    wait_ms: int = Field(1000, ge=500, le=10000)
    max_scrolls: int = Field(100, ge=1, le=500)

    @field_validator('catalog_fields')
    @classmethod
    def airport_projection(cls, value):
        if set(value) - {'iata', 'airport_name', 'city', 'country'}:
            raise ValueError('Only airport metadata may be retained')
        return value

    @field_validator('origin', 'destination', 'options', 'listbox', 'code', 'airport_name', 'city', 'consent')
    @classmethod
    def css(cls, value):
        return selector(value) if value is not None else None


class FamilyContract(Strict):
    """Every family within every actual card, after ordinary reveal actions."""
    container: str
    name: str
    name_attribute: Literal["alt", "aria-label"] | None = None
    available_control: str | None = None
    price: str
    currency: str
    cabin: str | None = None
    cabin_scope: Literal["family", "card"] = "family"
    cabin_attribute: Literal["aria-label"] | None = None
    detail_rows: str | None = None
    detail_label: str | None = None
    detail_value: str | None = None
    detail_labels: dict[str, str] = Field(default_factory=dict, max_length=45)
    shared_detail_rows: str | None = None
    shared_detail_label: str | None = None
    shared_detail_value: str | None = None
    shared_detail_labels: dict[str, str] = Field(default_factory=dict, max_length=20)
    cabin_names: dict[str, Literal['Economy','Premium Economy','Business','First']] = Field(default_factory=dict)
    reveal: str | None = None
    selected: str | None = None
    minimum_interval_ms: int = Field(3000, ge=500, le=60000)
    details_reveal: str | None = None
    details: dict[str, str] = Field(default_factory=dict, max_length=45)
    flight_details: dict[str, str] = Field(default_factory=dict, max_length=15)

    @field_validator('available_control','container','name','price','currency','cabin','reveal','selected','details_reveal','detail_rows','detail_label','detail_value','shared_detail_rows','shared_detail_label','shared_detail_value')
    @classmethod
    def css(cls, value):
        return selector(value) if value is not None else None

    @field_validator('details','flight_details')
    @classmethod
    def detail_selectors(cls, value):
        from udaan.canonical import FareDetails
        allowed = set(FareDetails.model_fields) | {'base_fare','taxes','fees','departure_at','arrival_at'}
        if set(value) - allowed:
            raise ValueError('Only canonical flight/fare details may be extracted')
        for css in value.values():
            selector(css)
        return value

    @field_validator('detail_labels','shared_detail_labels')
    @classmethod
    def canonical_labels(cls, value):
        from udaan.canonical import FareDetails
        if set(value.values()) - set(FareDetails.model_fields):
            raise ValueError('Labels must map to canonical fields')
        return value


class FlightDetailsContract(Strict):
    close: str | None = None
    reveal: str
    dialog: str
    numbers: str
    aircraft: str
    operators: str
    departures: str
    arrivals: str
    airport_code: str
    airport_name: str
    city: str
    date: str
    time: str
    date_format: Literal['%d %b %y', '%d %b %Y', '%Y-%m-%d']

    @field_validator('close','reveal','dialog','numbers','aircraft','operators','departures','arrivals','airport_code','airport_name','city','date','time')
    @classmethod
    def css(cls, value):
        return selector(value) if value is not None else None
