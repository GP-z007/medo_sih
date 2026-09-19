"""WoW, MoM, and YoY inflation from coverage-qualified index series."""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl


@dataclass
class InflationReport:
    input_rows: int
    calculated_rows: int
    comparison_missing_rows: int
    insufficient_rows: int


def _empty_weekly() -> pl.DataFrame:
    return pl.DataFrame(schema={
        "week_start": pl.Date, "week_end": pl.Date,
        "weekly_airfare_index": pl.Float64, "previous_week_index": pl.Float64,
        "wow_inflation": pl.Float64, "wow_status": pl.String,
    })


def _empty_monthly() -> pl.DataFrame:
    return pl.DataFrame(schema={
        "month": pl.Date, "monthly_airfare_index": pl.Float64,
        "previous_month_index": pl.Float64, "same_month_previous_year_index": pl.Float64,
        "mom_inflation": pl.Float64, "mom_status": pl.String,
        "yoy_inflation": pl.Float64, "yoy_status": pl.String,
    })


def _report(frame: pl.DataFrame, statuses: list[str]) -> InflationReport:
    calculated = sum(frame.filter(pl.col(status) == "CALCULATED").height for status in statuses)
    missing = sum(frame.filter(pl.col(status) == "COMPARISON_MISSING").height for status in statuses)
    insufficient = sum(frame.filter(~pl.col(status).is_in(["CALCULATED", "COMPARISON_MISSING", "ZERO_DENOMINATOR"])) .height for status in statuses)
    return InflationReport(frame.height, calculated, missing, insufficient)


def calculate_weekly_inflation(weekly: pl.DataFrame) -> tuple[pl.DataFrame, InflationReport]:
    """Calculate WoW only for consecutive, coverage-qualified calendar weeks."""
    required = {"week_start", "week_end", "weekly_airfare_index", "weekly_index_status"}
    if not required.issubset(weekly.columns):
        return _empty_weekly(), InflationReport(weekly.height, 0, 0, weekly.height)
    current = weekly.with_columns(
        pl.col("week_start").cast(pl.String).str.slice(0, 10).str.to_date(strict=False).alias("week_start"),
        pl.col("weekly_airfare_index").cast(pl.Float64, strict=False).alias("weekly_airfare_index"),
    )
    previous = current.select("week_start", "weekly_airfare_index", "weekly_index_status").with_columns(
        (pl.col("week_start") + pl.duration(days=7)).cast(pl.Date).alias("week_start")
    ).rename({"weekly_airfare_index": "previous_week_index", "weekly_index_status": "_previous_status"})
    result = current.join(previous, on="week_start", how="left").with_columns(
        pl.when(pl.col("weekly_index_status") != "VALID").then(pl.lit("INSUFFICIENT_DATA"))
        .when(pl.col("previous_week_index").is_null() | (pl.col("_previous_status") != "VALID")).then(pl.lit("COMPARISON_MISSING"))
        .when(pl.col("previous_week_index") == 0).then(pl.lit("ZERO_DENOMINATOR"))
        .otherwise(pl.lit("CALCULATED")).alias("wow_status"),
    ).with_columns(
        pl.when(pl.col("wow_status") == "CALCULATED").then(
            ((pl.col("weekly_airfare_index") / pl.col("previous_week_index") - 1) * 100).round(2)
        ).otherwise(pl.lit(None, dtype=pl.Float64)).alias("wow_inflation")
    ).select(_empty_weekly().columns).sort("week_start")
    return result, _report(result, ["wow_status"])


def calculate_monthly_inflation(monthly: pl.DataFrame) -> tuple[pl.DataFrame, InflationReport]:
    """Calculate MoM and YoY only when both coverage-qualified months exist."""
    required = {"month", "monthly_airfare_index", "monthly_index_status"}
    if not required.issubset(monthly.columns):
        return _empty_monthly(), InflationReport(monthly.height, 0, 0, monthly.height)
    current = monthly.with_columns(
        pl.col("month").cast(pl.String).str.slice(0, 10).str.to_date(strict=False).alias("month"),
        pl.col("monthly_airfare_index").cast(pl.Float64, strict=False).alias("monthly_airfare_index"),
    )
    base = current.select("month", "monthly_airfare_index", "monthly_index_status")
    prior_month = base.with_columns(pl.col("month").dt.offset_by("1mo").alias("month")).rename({
        "monthly_airfare_index": "previous_month_index", "monthly_index_status": "_previous_month_status",
    })
    prior_year = base.with_columns(pl.col("month").dt.offset_by("12mo").alias("month")).rename({
        "monthly_airfare_index": "same_month_previous_year_index", "monthly_index_status": "_previous_year_status",
    })
    result = current.join(prior_month, on="month", how="left").join(prior_year, on="month", how="left").with_columns(
        pl.when(pl.col("monthly_index_status") != "VALID").then(pl.lit("INSUFFICIENT_DATA"))
        .when(pl.col("previous_month_index").is_null() | (pl.col("_previous_month_status") != "VALID")).then(pl.lit("COMPARISON_MISSING"))
        .when(pl.col("previous_month_index") == 0).then(pl.lit("ZERO_DENOMINATOR"))
        .otherwise(pl.lit("CALCULATED")).alias("mom_status"),
        pl.when(pl.col("monthly_index_status") != "VALID").then(pl.lit("INSUFFICIENT_DATA"))
        .when(pl.col("same_month_previous_year_index").is_null() | (pl.col("_previous_year_status") != "VALID")).then(pl.lit("COMPARISON_MISSING"))
        .when(pl.col("same_month_previous_year_index") == 0).then(pl.lit("ZERO_DENOMINATOR"))
        .otherwise(pl.lit("CALCULATED")).alias("yoy_status"),
    ).with_columns(
        pl.when(pl.col("mom_status") == "CALCULATED").then(
            ((pl.col("monthly_airfare_index") / pl.col("previous_month_index") - 1) * 100).round(2)
        ).otherwise(pl.lit(None, dtype=pl.Float64)).alias("mom_inflation"),
        pl.when(pl.col("yoy_status") == "CALCULATED").then(
            ((pl.col("monthly_airfare_index") / pl.col("same_month_previous_year_index") - 1) * 100).round(2)
        ).otherwise(pl.lit(None, dtype=pl.Float64)).alias("yoy_inflation"),
    ).select(_empty_monthly().columns).sort("month")
    return result, _report(result, ["mom_status", "yoy_status"])
