"""Coverage-aware weekly aggregation of daily airfare indices."""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl


@dataclass
class WeeklyIndexReport:
    input_rows: int
    output_rows: int
    valid_daily_observations: int
    weeks_calculated: int
    weeks_without_valid_data: int
    duplicate_dates: int
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return self.__dict__.copy()


def _empty() -> pl.DataFrame:
    return pl.DataFrame(schema={
        "week_start": pl.Date,
        "week_end": pl.Date,
        "weekly_airfare_index": pl.Float64,
        "valid_daily_observations": pl.Int64,
        "expected_daily_observations": pl.Int64,
        "coverage": pl.Float64,
        "weekly_index_status": pl.String,
    })


def calculate_weekly_index(
    df: pl.DataFrame,
    min_valid_days: int = 4,
) -> tuple[pl.DataFrame, WeeklyIndexReport]:
    """Take the arithmetic mean of valid daily values for Monday--Sunday.

    A week with fewer than ``min_valid_days`` is retained with a null index and
    ``LOW_COVERAGE`` status. Missing days are never zero-filled or carried
    forward.
    """
    if not 1 <= min_valid_days <= 7:
        raise ValueError("min_valid_days must be between 1 and 7.")
    required = {"observation_date", "national_airfare_index"}
    missing = sorted(required - set(df.columns))
    if missing:
        return _empty(), WeeklyIndexReport(df.height, 0, 0, 0, 0, 0, [f"Missing required columns: {', '.join(missing)}"])
    working = df.with_columns(
        pl.col("observation_date").cast(pl.String).str.slice(0, 10).str.to_date(strict=False).alias("observation_date"),
        pl.col("national_airfare_index").cast(pl.Float64, strict=False).alias("national_airfare_index"),
    )
    status_valid = pl.lit(True) if "index_status" not in working.columns else (pl.col("index_status") == "VALID")
    valid = working.filter(
        pl.col("observation_date").is_not_null() & pl.col("national_airfare_index").is_not_null()
        & (pl.col("national_airfare_index") > 0) & status_valid
    )
    duplicates = valid.group_by("observation_date").len().filter(pl.col("len") > 1).height
    # The pipeline emits one daily value. If a caller sends duplicates, retain
    # the first deterministic record rather than letting a day receive extra
    # weight, and expose the condition in the report.
    valid = valid.sort("observation_date").unique(subset=["observation_date"], keep="first", maintain_order=True)
    if valid.is_empty():
        return _empty(), WeeklyIndexReport(df.height, 0, 0, 0, 0, duplicates, ["No valid daily indices available."])
    weekly = valid.with_columns(
        (pl.col("observation_date") - pl.duration(days=pl.col("observation_date").dt.weekday() - 1)).cast(pl.Date).alias("week_start")
    ).with_columns((pl.col("week_start") + pl.duration(days=6)).cast(pl.Date).alias("week_end"))
    result = weekly.group_by("week_start", "week_end").agg(
        pl.col("national_airfare_index").mean().alias("_weekly_value"),
        pl.len().cast(pl.Int64).alias("valid_daily_observations"),
    ).with_columns(
        pl.lit(7, dtype=pl.Int64).alias("expected_daily_observations"),
    ).with_columns(
        (pl.col("valid_daily_observations").cast(pl.Float64) / pl.col("expected_daily_observations")).alias("coverage")
    ).with_columns(
        pl.when(pl.col("valid_daily_observations") >= min_valid_days).then(pl.lit("VALID")).otherwise(pl.lit("LOW_COVERAGE")).alias("weekly_index_status"),
    ).with_columns(
        pl.when(pl.col("weekly_index_status") == "VALID").then(pl.col("_weekly_value").round(2)).otherwise(pl.lit(None, dtype=pl.Float64)).alias("weekly_airfare_index")
    ).select(_empty().columns).sort("week_start")
    return result, WeeklyIndexReport(
        input_rows=df.height,
        output_rows=result.height,
        valid_daily_observations=valid.height,
        weeks_calculated=result.filter(pl.col("weekly_index_status") == "VALID").height,
        weeks_without_valid_data=result.filter(pl.col("weekly_index_status") != "VALID").height,
        duplicate_dates=duplicates,
        warnings=[f"{duplicates} duplicate daily date(s) were de-weighted."] if duplicates else [],
    )
