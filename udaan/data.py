import csv
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import Boolean, Date, DateTime, Integer, Numeric, String, cast, func, literal, select, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import aliased

from udaan.canonical import BOOLEAN_DETAILS, CANONICAL_COLUMNS, INTEGER_DETAILS, MONEY_DETAILS, FareDetails
from udaan.config import settings
from udaan.contracts import DataQuery, DomainError, Fare, ProcessingOptions, SearchRequest
from udaan.db import AirportReference, IndexRun, Job, Observation, Recipe, Source


def process_fares(fares: list[Fare], request: SearchRequest, *, all_cabins=False) -> list[dict]:
    unique = {}
    for fare in fares:
        if (fare.origin, fare.destination, fare.departure_date, fare.booking_window) != (
            request.origin, request.destination, request.departure_date, request.booking_window):
            raise DomainError("EXTRACTION_SCHEMA_FAILED", "Extracted fare does not match the requested search")
        if not all_cabins and fare.cabin != request.cabin:
            raise DomainError("EXTRACTION_SCHEMA_FAILED", "Extracted cabin does not match the requested search")
        if all_cabins and not fare.details.fare_family:
            raise DomainError("EXTRACTION_SCHEMA_FAILED", "Every common-contract fare requires its displayed fare family")
        row = fare.model_dump()
        row["details"] = fare.details.model_dump(mode="json", exclude_none=True)
        # Only vetted extraction provenance is retained, never arbitrary page text.
        row["extraction_metadata"] = {k: v for k, v in row["extraction_metadata"].items()
                                      if k in {"selector_version", "fare_basis", "passengers", "source_page", "anomaly", "carrier_basis"}}
        components = [row[x] for x in ("base_fare", "taxes", "fees")]
        if all(x is not None for x in components) and sum(components) != row["total_fare"]:
            row["quality_flags"] = [*row["quality_flags"], "COMPONENT_TOTAL_MISMATCH"]
        key = {k: v for k, v in row.items() if k not in {"quality_flags", "extraction_metadata"}}
        fingerprint = hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()
        row["fingerprint"] = fingerprint
        unique[fingerprint] = row
    rows = list(unique.values())
    if len(rows) >= 5:
        frame = pl.DataFrame({"i": list(range(len(rows))), "price": [float(r["total_fare"]) for r in rows],
                              "currency": [r["currency"] for r in rows]})
        scored = frame.with_columns(
            ((pl.col("price") - pl.col("price").mean().over("currency")) /
             pl.col("price").std(ddof=0).over("currency")).alias("z"))
        for row in scored.iter_rows(named=True):
            if row["z"] is not None and abs(row["z"]) > 3:
                target = rows[row["i"]]
                target["quality_flags"] = [*target["quality_flags"], "PRICE_ANOMALY"]
                target["extraction_metadata"]["anomaly"] = {"method": "batch_currency_zscore", "threshold": 3,
                                                            "score": row["z"], "policy": "retained"}
    return rows


def dataset(query: DataQuery):
    if query.dataset in {"observations","canonical"}:
        columns = {c.key: getattr(Observation, c.key) for c in Observation.__table__.columns}
        columns.update(source=Source.name, source_url=Source.base_url, job_request=Job.request,
                       recipe_version=Recipe.version,
                       route=(Observation.origin + literal(" → ") + Observation.destination),
                       collection_date=cast(Observation.collected_at, Date))
        source = (Observation.__table__.join(Source, Observation.source_id == Source.id)
                  .join(Recipe, Observation.recipe_id == Recipe.id).join(Job, Observation.job_id == Job.id))
        if query.dataset == "canonical":
            origin_ref,destination_ref=aliased(AirportReference),aliased(AirportReference)
            source = source.outerjoin(origin_ref,Observation.origin==origin_ref.iata).outerjoin(destination_ref,Observation.destination==destination_ref.iata)
            canonical = {
                "collection_timestamp":Observation.collected_at,"source_airline":Source.name,"source_url":Source.base_url,
                "origin_iata":Observation.origin,"destination_iata":Observation.destination,
                "departure_date":Observation.departure_date,"booking_date":cast(func.timezone('UTC',Observation.collected_at),Date),
                "booking_lead_days":cast(Observation.departure_date-cast(func.timezone('UTC',Observation.collected_at),Date),Integer),
                "adults":cast(cast(Job.request,JSONB)["adults"].astext,Integer),
                "cabin_class":Observation.cabin,"airline":Observation.airline,"flight_number":Observation.flight_number,
                "departure_datetime":Observation.departure_at,"arrival_datetime":Observation.arrival_at,
                "displayed_fare":Observation.total_fare,"base_fare":Observation.base_fare,
                "total_tax":Observation.taxes,"total_fees":Observation.fees,"currency":Observation.currency,
                "recipe_version":Recipe.version,
            }
            for key in FareDetails.model_fields:
                type_ = Numeric(16,2) if key in MONEY_DETAILS else Integer if key in INTEGER_DETAILS else Boolean if key in BOOLEAN_DETAILS else String
                canonical[key]=cast(cast(Observation.details,JSONB)[key].astext,type_)
            for prefix,reference in [('origin',origin_ref),('destination',destination_ref)]:
                for field,attribute in [('airport_name','airport_name'),('city','city'),('state','state')]:
                    key=prefix+'_'+field
                    canonical[key]=func.coalesce(canonical[key],getattr(reference,attribute))
            canonical['scrape_status']=func.coalesce(canonical['scrape_status'],literal('SUCCESS'))
            canonical['extraction_method']=func.coalesce(canonical['extraction_method'],literal('EURA_RECIPE'))
            assert set(CANONICAL_COLUMNS)==set(canonical)
            columns.update(canonical)
        return columns, source, [Observation.collected_at, Observation.id]
    points = func.jsonb_array_elements(cast(IndexRun.values, JSONB)).table_valued("value").lateral("points")
    value = cast(points.c.value, JSONB)
    columns = {
        "id": IndexRun.id, "created_at": IndexRun.created_at, "series": IndexRun.methodology,
        "base_period": IndexRun.base_period, "currency": IndexRun.currency,
        "period": value["period"].astext, "index": cast(value["index"].astext, Numeric(24, 10)),
        "inflation": cast(value["inflation"].astext, Numeric(24, 10)),
        "year": cast(func.substr(value["period"].astext, 1, 4), Integer),
        "month": cast(func.substr(value["period"].astext, 6, 2), Integer),
        "base_year": cast(func.substr(IndexRun.base_period, 1, 4), Integer),
    }
    for unavailable in ["state", "sector", "division", "group", "class", "sub_class", "item", "code", "imputation"]:
        columns[unavailable] = cast(literal(None), String)
    return columns, IndexRun.__table__.join(points, true()), [IndexRun.id, columns["period"]]


def coerce(column, value):
    if value is None:
        return None
    try:
        if isinstance(column.type, DateTime):
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if not parsed.tzinfo:
                raise ValueError("Timezone required")
            return parsed
        if isinstance(column.type, Date):
            return date.fromisoformat(str(value))
        if isinstance(column.type, Boolean):
            if str(value).lower() not in {"true","false"}:
                raise ValueError("Expected boolean")
            return str(value).lower()=="true"
        if isinstance(column.type, Integer):
            return int(value)
        if isinstance(column.type, Numeric):
            result = Decimal(str(value))
            if not result.is_finite():
                raise ValueError("Finite numeric required")
            return result
        if not isinstance(column.type, String):
            raise ValueError("Filtering this structured column is not supported")
        if not isinstance(value, str) or len(value) > 500:
            raise ValueError("Expected bounded string")
        return value
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise DomainError("INVALID_QUERY", "Filter value does not match the selected column", 422) from exc


def build_query(query: DataQuery, paginate=True):
    columns, source, ties = dataset(query)
    def column(name):
        if name not in columns:
            raise DomainError("INVALID_QUERY", f"Unsupported column: {name[:60]}", 422)
        return columns[name]
    grouped = bool(query.group_by or query.aggregates)
    if grouped:
        selected = {name: column(name) for name in query.group_by}
        for agg in query.aggregates:
            col = column(agg.column)
            if agg.function != "count" and not isinstance(col.type, (Numeric, Integer)):
                raise DomainError("INVALID_QUERY", "Numeric aggregates require numeric columns", 422)
            name = f"{agg.function}_{agg.column}"
            value = getattr(func, agg.function)(col)
            if agg.function == "avg":
                value = cast(value, Numeric(38, 10))
            elif agg.function == "sum" and isinstance(col.type, Numeric):
                value = cast(value, Numeric(38, col.type.scale or 0))
            selected[name] = value
        if not selected:
            raise DomainError("INVALID_QUERY", "Select grouping columns or aggregates", 422)
    else:
        selected = {name: column(name) for name in query.columns}
    stmt = select(*(col.label(name) for name, col in selected.items())).select_from(source)
    if query.scrape_group_id:
        if query.dataset not in {"observations", "canonical"}:
            raise DomainError("INVALID_QUERY", "Scrape-group scope is available only for fare observations", 422)
        stmt = stmt.where(Observation.group_id == query.scrape_group_id)
    for item in query.filters:
        col = column(item.column)
        if item.op == "contains":
            if item.column != "quality_flags" or query.dataset not in {"observations","canonical"} or not isinstance(item.value, str) or not 1 <= len(item.value) <= 100:
                raise DomainError("INVALID_QUERY", "Contains requires a single quality flag", 422)
            condition = cast(col, JSONB).contains([item.value])
        elif item.op == "in":
            if not isinstance(item.value, list) or not 1 <= len(item.value) <= 100:
                raise DomainError("INVALID_QUERY", "IN requires 1 to 100 values", 422)
            condition = col.in_([coerce(col, value) for value in item.value])
        else:
            if isinstance(item.value, list):
                raise DomainError("INVALID_QUERY", "Scalar operator requires one value", 422)
            value = coerce(col, item.value)
            if value is None and item.op != "eq":
                raise DomainError("INVALID_QUERY", "Only equality supports NULL", 422)
            condition = {"eq": lambda: col == value, "gte": lambda: col >= value, "lte": lambda: col <= value}[item.op]()
        stmt = stmt.where(condition)
    if grouped:
        stmt = stmt.group_by(*(column(name) for name in query.group_by))
    count = select(func.count()).select_from(stmt.subquery())
    if grouped:
        if query.sort not in selected:
            raise DomainError("INVALID_QUERY", "Grouped sort must name a grouping or aggregate output", 422)
        sort = selected[query.sort]
        ties = [column(name) for name in query.group_by]
    else:
        sort = column(query.sort)
    stmt = stmt.order_by(sort.desc().nulls_last() if query.descending else sort.asc().nulls_last(), *ties)
    if paginate:
        stmt = stmt.offset(query.offset).limit(query.limit)
    return stmt, count


def query_data(db, query: DataQuery):
    stmt, count = build_query(query)
    return {"matched_count": db.scalar(count), "limit": query.limit, "offset": query.offset,
            "rows": [dict(r) for r in db.execute(stmt).mappings()]}


def export_query(connection, query: DataQuery, format: str, rows: int | None, identifier: str,
                 *, processing: ProcessingOptions | None = None):
    if processing:
        from udaan.processing_service import ProcessingInput, process_database_rows
        processed = process_database_rows(connection, ProcessingInput(
            query=query, base_period_start=processing.base_period_start,
            base_period_end=processing.base_period_end), all_rows=True)
        matched = processed['matched_count']
        records = processed['rows']
        selected = records[:rows] if rows is not None else records
        schema = (pa.Table.from_pylist(selected).schema if selected else
                  pa.schema([pa.field(name, pa.string()) for name in
                             ['source_row_id', 'job_id', 'group_id', 'source_id', *CANONICAL_COLUMNS]]))
        batches = (selected[start:start + 2000] for start in range(0, len(selected), 2000))
        expected = len(selected)
    else:
        stmt, count = build_query(query, paginate=False)
        matched = connection.scalar(count)
        fields = []
        for col in stmt.selected_columns:
            t = col.type
            arrow_type = (pa.timestamp("us", tz="UTC") if isinstance(t, DateTime) else
                          pa.date32() if isinstance(t, Date) else
                          pa.bool_() if isinstance(t, Boolean) else
                          pa.int64() if isinstance(t, Integer) else
                          pa.decimal128(t.precision or 38, t.scale if t.scale is not None else 10) if isinstance(t, Numeric) else pa.string())
            fields.append(pa.field(col.key, arrow_type))
        schema = pa.schema(fields)
        batches = connection.execute(stmt.offset(0).limit(rows).execution_options(stream_results=True)).mappings().partitions(2000)
        expected = min(rows, matched) if rows is not None else matched
    matched_sources = matched_jobs = 0
    if query.dataset in {"observations", "canonical"}:
        ownership_query = query.model_copy(update={"columns": ["source_id", "job_id"], "group_by": [],
                                                   "aggregates": [], "sort": "collected_at", "offset": 0})
        ownership_stmt, _ = build_query(ownership_query, paginate=False)
        ownership = ownership_stmt.order_by(None).subquery()
        matched_sources = connection.scalar(select(func.count(func.distinct(ownership.c.source_id)))) or 0
        matched_jobs = connection.scalar(select(func.count(func.distinct(ownership.c.job_id)))) or 0
    root = settings().export_directory.resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{identifier}.{format}"
    temp = root / f"{identifier}.{format}.partial"
    exported, writer, handle = 0, None, None
    try:
        if format == "csv":
            handle = temp.open("w", newline="", encoding="utf-8")
            writer = csv.DictWriter(handle, fieldnames=schema.names)
            writer.writeheader()
        else:
            writer = pq.ParquetWriter(temp, schema)
        for batch in batches:
            normalized = []
            for row in batch:
                item = dict(row)
                for field in schema:
                    value = item[field.name]
                    if pa.types.is_string(field.type) and value is not None:
                        item[field.name] = json.dumps(value, default=str) if isinstance(value, (dict, list)) else str(value)
                normalized.append(item)
            if format == "csv":
                # Spreadsheet formula protection changes presentation only, not database values.
                writer.writerows({k: ("'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v)
                                  for k, v in row.items()} for row in normalized)
            else:
                writer.write_table(pa.Table.from_pylist(normalized, schema=schema))
            exported += len(normalized)
        if handle:
            handle.close()
        else:
            writer.close()
        path_tmp = temp.replace(path)
        if exported != expected:
            path.unlink(missing_ok=True)
            raise DomainError("EXPORT_COUNT_MISMATCH", "Exported row count does not match the data query")
        return {"path": str(path_tmp), "matched_count": matched, "matched_source_count": matched_sources,
                "matched_job_count": matched_jobs, "exported_count": exported, "size_bytes": path.stat().st_size}
    except BaseException:
        if handle:
            handle.close()
        elif writer:
            writer.close()
        temp.unlink(missing_ok=True)
        raise
