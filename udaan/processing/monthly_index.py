"""Coverage-aware monthly aggregation of daily airfare indices."""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl


@dataclass
class MonthlyIndexReport:
    input_rows: int
    output_rows: int
    valid_daily_observations: int
    months_calculated: int
    low_coverage_months: int
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return self.__dict__.copy()


def _empty() -> pl.DataFrame:
    return pl.DataFrame(schema={
        "month": pl.Date,
        "monthly_airfare_index": pl.Float64,
        "valid_daily_observations": pl.Int64,
        "expected_daily_observations": pl.Int64,
        "coverage": pl.Float64,
        "monthly_index_status": pl.String,
    })


def calculate_monthly_index(
    df: pl.DataFrame,
    min_coverage: float = 0.7,
) -> tuple[pl.DataFrame, MonthlyIndexReport]:
    """Aggregate daily values by calendar month without filling missing days."""
    if not 0 < min_coverage <= 1:
        raise ValueError("min_coverage must be in (0, 1].")
    required = {"observation_date", "national_airfare_index"}
    missing = sorted(required - set(df.columns))
    if missing:
        return _empty(), MonthlyIndexReport(df.height, 0, 0, 0, 0, [f"Missing required columns: {', '.join(missing)}"])
    working = df.with_columns(
        pl.col("observation_date").cast(pl.String).str.slice(0, 10).str.to_date(strict=False).alias("observation_date"),
        pl.col("national_airfare_index").cast(pl.Float64, strict=False).alias("national_airfare_index"),
    )
    status_valid = pl.lit(True) if "index_status" not in working.columns else (pl.col("index_status") == "VALID")
    valid = working.filter(
        pl.col("observation_date").is_not_null() & pl.col("national_airfare_index").is_not_null()
        & (pl.col("national_airfare_index") > 0) & status_valid
    ).sort("observation_date").unique(subset=["observation_date"], keep="first", maintain_order=True)
    if valid.is_empty():
        return _empty(), MonthlyIndexReport(df.height, 0, 0, 0, 0, ["No valid daily indices available."])
    monthly = valid.with_columns(pl.col("observation_date").dt.truncate("1mo").cast(pl.Date).alias("month"))
    result = monthly.group_by("month").agg(
        pl.col("national_airfare_index").mean().alias("_monthly_value"),
        pl.len().cast(pl.Int64).alias("valid_daily_observations"),
    ).with_columns(
        (pl.col("month").dt.offset_by("1mo") - pl.col("month")).dt.total_days().cast(pl.Int64).alias("expected_daily_observations")
    ).with_columns(
        (pl.col("valid_daily_observations").cast(pl.Float64) / pl.col("expected_daily_observations")).alias("coverage")
    ).with_columns(
        pl.when(pl.col("coverage") >= min_coverage).then(pl.lit("VALID")).otherwise(pl.lit("LOW_COVERAGE")).alias("monthly_index_status"),
    ).with_columns(
        pl.when(pl.col("monthly_index_status") == "VALID").then(pl.col("_monthly_value").round(2)).otherwise(pl.lit(None, dtype=pl.Float64)).alias("monthly_airfare_index")
    ).select(_empty().columns).sort("month")
    return result, MonthlyIndexReport(
        input_rows=df.height,
        output_rows=result.height,
        valid_daily_observations=valid.height,
        months_calculated=result.filter(pl.col("monthly_index_status") == "VALID").height,
        low_coverage_months=result.filter(pl.col("monthly_index_status") != "VALID").height,
    )
