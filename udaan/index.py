"""Udaan monthly matched-strata chained Fisher v1; no imputation."""
from collections import defaultdict
from decimal import Decimal, localcontext

from sqlalchemy import cast, func, select
from sqlalchemy.dialects.postgresql import JSONB

from udaan.contracts import DomainError, IndexInput, WeightInput
from udaan.db import IndexRun, Job, Observation, WeightDataset
from udaan.services import checksum, get

DIMENSIONS = ("origin", "destination", "cabin", "currency", "booking_window")
METHODOLOGY = "udaan-monthly-matched-fisher-v1"


def stratum(row):
    return tuple(row[key] for key in DIMENSIONS)


def fisher(p0, p1, q0, q1):
    keys = set(p0)
    if not keys or any(set(values) != keys for values in (p1, q0, q1)):
        raise DomainError("INDEX_UNAVAILABLE", "Comparable prices and approved weights are incomplete")
    if any(not Decimal(v).is_finite() or Decimal(v) <= 0 for values in (p0, p1, q0, q1) for v in values.values()):
        raise DomainError("INDEX_UNAVAILABLE", "Prices and weights must be finite and positive")
    with localcontext() as ctx:
        ctx.prec = 34
        laspeyres = sum(p1[k] * q0[k] for k in keys) / sum(p0[k] * q0[k] for k in keys)
        paasche = sum(p1[k] * q1[k] for k in keys) / sum(p0[k] * q1[k] for k in keys)
        return (laspeyres * paasche).sqrt()


def import_weights(db, data: WeightInput):
    rows = data.model_dump(mode="json")["rows"]
    keys = [(r["period"], stratum(r)) for r in rows]
    if len(set(keys)) != len(keys):
        raise DomainError("INVALID_WEIGHTS", "Duplicate period/stratum weights", 422)
    digest = checksum(rows)
    existing = db.scalar(select(WeightDataset).where(WeightDataset.checksum == digest))
    if existing:
        return existing
    item = WeightDataset(**data.model_dump(mode="json"), checksum=digest)
    db.add(item)
    db.flush()
    return item


def periods_between(start, end):
    if end < start:
        raise DomainError("INVALID_PERIOD", "End period precedes base period", 422)
    year, month = map(int, start.split("-"))
    periods = []
    while f"{year:04}-{month:02}" <= end:
        periods.append(f"{year:04}-{month:02}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
        if len(periods) > 120:
            raise DomainError("INVALID_PERIOD", "Limit each index run to 120 months", 422)
    return periods


def calculate(db, data: IndexInput):
    weights = get(db, WeightDataset, data.weight_dataset_id)
    periods = periods_between(data.base_period, data.end_period)
    quantities = defaultdict(dict)
    for row in weights.rows:
        if row["currency"] == data.currency and row["period"] in periods:
            quantities[row["period"]][stratum(row)] = Decimal(row["quantity"])
    if any(not quantities[p] for p in periods):
        raise DomainError("INDEX_UNAVAILABLE", "INDEX UNAVAILABLE — REQUIRED WEIGHTS MISSING")
    keys = set(quantities[periods[0]])
    if any(set(quantities[p]) != keys for p in periods):
        raise DomainError("INDEX_UNAVAILABLE", "Approved weights must cover identical strata in every period")
    collected_period = func.to_char(Observation.collected_at.op("AT TIME ZONE")("UTC"), "YYYY-MM")
    cols = [getattr(Observation, key) for key in DIMENSIONS]
    search = cast(Job.request, JSONB)
    single_adult = ((search["adults"].astext == "1") & (search["children"].astext == "0") &
                    (search["infants"].astext == "0"))
    stmt = select(collected_period.label("period"), *cols, func.avg(Observation.total_fare).label("price"),
                  func.count().label("count")).join(Job, Observation.job_id == Job.id).where(single_adult,
                      collected_period.in_(periods), Observation.currency == data.currency).group_by(collected_period, *cols)
    prices, coverage = defaultdict(dict), {}
    for row in db.execute(stmt).mappings():
        key = stratum(row)
        if key in keys:
            prices[row["period"]][key] = row["price"]
            coverage[f"{row['period']}|{'|'.join(map(str, key))}"] = row["count"]
    if any(set(prices[p]) != keys for p in periods):
        raise DomainError("INDEX_UNAVAILABLE", "Real observations do not cover every approved stratum")
    chain, values = Decimal(100), []
    for i, period in enumerate(periods):
        relative = None if i == 0 else fisher(prices[periods[i - 1]], prices[period], quantities[periods[i - 1]], quantities[period])
        if relative is not None:
            chain *= relative
        values.append({"period": period, "index": str(chain),
                       "inflation": str((relative - 1) * 100) if relative is not None else None})
    identifiers = []
    for row in db.execute(select(Observation.id, Observation.collected_at, *cols).join(Job, Observation.job_id == Job.id).where(single_adult,
        collected_period.in_(periods), Observation.currency == data.currency)).mappings():
        if stratum(row) in keys:
            identifiers.append({"id": row["id"], "collected_at": row["collected_at"].isoformat()})
    item = IndexRun(weight_dataset_id=weights.id, methodology=METHODOLOGY,
                    base_period=data.base_period, currency=data.currency, values=values,
                    inputs={"observations": identifiers, "weight_checksum": weights.checksum,
                            "coverage": coverage, "inclusion_policy": "validated single-adult searches; anomalies retained",
                            "representative_price": "monthly arithmetic mean", "imputation": None})
    db.add(item)
    db.flush()
    return item
