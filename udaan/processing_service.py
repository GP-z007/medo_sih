"""Run the existing calculation pipeline over persisted Udaan observations."""
from datetime import date, timezone

import polars as pl
from pydantic import Field, model_validator

from udaan.canonical import CANONICAL_COLUMNS, FareDetails
from udaan.contracts import DataQuery, DomainError, Strict
from udaan.data import build_query
from udaan.processing.pipeline import PipelineConfig, run_pipeline

MAX_PROCESSING_ROWS = 10000
INPUT_COLUMNS = [
    'id', 'job_id', 'group_id', 'source_id', 'source', 'source_url', 'job_request',
    'collected_at', 'origin', 'destination', 'departure_date', 'departure_at',
    'arrival_at', 'airline', 'flight_number', 'cabin', 'booking_window',
    'total_fare', 'base_fare', 'taxes', 'fees', 'currency', 'details', 'recipe_version',
]


class ProcessingInput(Strict):
    query: DataQuery = Field(default_factory=lambda: DataQuery(dataset='canonical'))
    base_period_start: date
    base_period_end: date | None = None

    @model_validator(mode='after')
    def permitted_query(self):
        if self.query.dataset not in {'observations', 'canonical'} or self.query.group_by or self.query.aggregates:
            raise ValueError('Processing requires ungrouped airfare observations')
        return self


def processing_query(query: DataQuery):
    """Keep the Data query's filters and sort, but select raw fields for the pipeline."""
    return build_query(query.model_copy(update={'columns': INPUT_COLUMNS, 'offset': 0}), paginate=False)


def canonical_input(row):
    """Map known persisted facts and leave unavailable source facts missing."""
    details = row['details'] or {}
    collected = row['collected_at']
    collected = collected.replace(tzinfo=timezone.utc) if collected.tzinfo is None else collected.astimezone(timezone.utc)
    booking_date = collected.date()
    mapped = {name: None for name in CANONICAL_COLUMNS}
    mapped.update({name: details.get(name) for name in FareDetails.model_fields})
    mapped.update({
        'source_row_id': row['id'], 'job_id': row['job_id'], 'group_id': row['group_id'],
        'source_id': row['source_id'], 'requested_booking_window': row['booking_window'],
        'collection_timestamp': collected.isoformat(), 'source_airline': row['source'],
        'source_url': row['source_url'], 'adults': (row['job_request'] or {}).get('adults'),
        'origin_iata': row['origin'], 'destination_iata': row['destination'],
        'departure_date': row['departure_date'].isoformat(), 'booking_date': booking_date.isoformat(),
        'booking_lead_days': (row['departure_date'] - booking_date).days,
        'airline': row['airline'], 'flight_number': row['flight_number'], 'cabin_class': row['cabin'],
        'departure_datetime': row['departure_at'].isoformat() if row['departure_at'] else None,
        'arrival_datetime': row['arrival_at'].isoformat() if row['arrival_at'] else None,
        'displayed_fare': row['total_fare'], 'base_fare': row['base_fare'],
        'total_tax': row['taxes'], 'total_fees': row['fees'],
        'currency': row['currency'], 'recipe_version': row['recipe_version'],
        'scrape_status': details.get('scrape_status') or 'SUCCESS',
    })
    return mapped


def process_database_rows(db, data: ProcessingInput, *, all_rows=False):
    stmt, count = processing_query(data.query)
    matched = db.scalar(count) or 0
    if matched > MAX_PROCESSING_ROWS:
        raise DomainError('PROCESSING_SCOPE_TOO_LARGE', 'Filter to 10,000 or fewer observations for processing', 422)
    if not matched:
        return {'matched_count': 0, 'processed_count': 0, 'rows': [], 'invalid_rows': [],
                'summary': None, 'reports': {}}
    records = [canonical_input(row) for row in db.execute(stmt.limit(MAX_PROCESSING_ROWS + 1)).mappings()]
    if len(records) != matched:
        raise DomainError('PROCESSING_COUNT_MISMATCH', 'The observation set changed during processing; retry', 409)
    result = run_pipeline(pl.DataFrame(records), PipelineConfig(
        base_period_start=data.base_period_start.isoformat(),
        base_period_end=data.base_period_end.isoformat() if data.base_period_end else None))
    normalized = result.frames['outliers']
    invalid = result.frames['validation'].filter(pl.col('validation_status') == 'INVALID')
    offset, limit = (0, normalized.height) if all_rows else (data.query.offset, data.query.limit)
    return {'matched_count': matched, 'processed_count': normalized.height,
            'rows': normalized.slice(offset, limit).to_dicts(),
            'invalid_rows': invalid.select('source_row_id', 'validation_reason').slice(offset, limit).to_dicts(),
            'summary': result.summary(), 'reports': result.reports}
