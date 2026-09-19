"""The standardized export contract. Missing source facts stay null."""
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

CANONICAL_COLUMNS = '''collection_timestamp source_airline source_url
origin_airport_name origin_iata origin_city origin_state
destination_airport_name destination_iata destination_city destination_state
departure_date booking_date booking_lead_days trip_type adults cabin_class airline
operating_airline flight_number departure_datetime arrival_datetime duration_minutes
stops segment_count aircraft_type fare_family booking_class displayed_fare base_fare
airline_surcharge fuel_surcharge GST UDF PSF ASF airport_fee convenience_fee other_tax
other_fee total_tax total_fees discount total_payable currency refundable
cancellation_fee change_fee cabin_baggage checkin_baggage meal_included seats_remaining
availability_status route_type direct_or_connecting scrape_status extraction_method
recipe_version'''.split()

Money = Annotated[Decimal, Field(ge=0, max_digits=16, decimal_places=2)]
ShortText = Annotated[str, Field(max_length=250)]


class FareDetails(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    origin_airport_name: ShortText | None = None
    origin_city: ShortText | None = None
    origin_state: ShortText | None = None
    destination_airport_name: ShortText | None = None
    destination_city: ShortText | None = None
    destination_state: ShortText | None = None
    trip_type: Literal['ONE_WAY','ROUND_TRIP'] | None = None
    operating_airline: ShortText | None = None
    duration_minutes: int | None = Field(None,ge=1,le=30000)
    stops: int | None = Field(None,ge=0,le=20)
    segment_count: int | None = Field(None,ge=1,le=21)
    aircraft_type: ShortText | None = None
    fare_family: ShortText | None = None
    booking_class: ShortText | None = None
    airline_surcharge: Money | None = None
    fuel_surcharge: Money | None = None
    GST: Money | None = None
    UDF: Money | None = None
    PSF: Money | None = None
    ASF: Money | None = None
    airport_fee: Money | None = None
    convenience_fee: Money | None = None
    other_tax: Money | None = None
    other_fee: Money | None = None
    discount: Money | None = None
    total_payable: Money | None = None
    refundable: bool | None = None
    cancellation_fee: ShortText | None = None
    change_fee: ShortText | None = None
    cabin_baggage: ShortText | None = None
    checkin_baggage: ShortText | None = None
    meal_included: bool | None = None
    seats_remaining: int | None = Field(None,ge=0,le=1000)
    availability_status: Literal['AVAILABLE','SOLD_OUT','WAITLIST','UNKNOWN'] | None = None
    route_type: Literal['DOMESTIC','INTERNATIONAL'] | None = None
    direct_or_connecting: Literal['DIRECT','CONNECTING'] | None = None
    scrape_status: Literal['SUCCESS','PARTIAL'] | None = None
    extraction_method: Literal['EURA_RECIPE','EURA_ADAPTIVE','EURA_AI_REPAIRED'] | None = None


MONEY_DETAILS = {'airline_surcharge','fuel_surcharge','GST','UDF','PSF','ASF','airport_fee',
                 'convenience_fee','other_tax','other_fee','discount','total_payable'}
INTEGER_DETAILS = {'duration_minutes','stops','segment_count','seats_remaining'}
BOOLEAN_DETAILS = {'refundable','meal_included'}
