"""One orchestration path from a scraper CSV to APIx and inflation.

This module is intentionally calculation-only: it reads one CSV and returns
in-memory Polars frames. It has no database, web API, or deployment concerns.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from udaan.processing.advance_window import assign_advance_windows
from udaan.processing.cleaning import clean_airfare_quotes
from udaan.processing.daily_representative import create_daily_representative
from udaan.processing.fare_groups import assign_fare_groups
from udaan.processing.fare_normalization import normalize_fares
from udaan.processing.index_calculation import IndexWeights, calculate_airfare_index
from udaan.processing.inflation import calculate_monthly_inflation, calculate_weekly_inflation
from udaan.processing.monthly_index import calculate_monthly_index
from udaan.processing.outlier_detection import detect_outliers
from udaan.processing.price_relative import calculate_price_relative
from udaan.processing.representative_fare import calculate_representative_fare
from udaan.processing.validation import annotate_validation
from udaan.processing.weekly_index import calculate_weekly_index


SCHEMA_ALIASES = {
    "origin_iata": ("origin", "origin_code", "departure_iata"),
    "destination_iata": ("destination", "destination_code", "arrival_iata"),
    "cabin_class": ("cabin",),
    "collection_timestamp": ("scrape_timestamp", "collected_at"),
    "availability_status": ("status",),
}


# ---------------------------------------------------------------------------
# VERIFIED ROUTE WEIGHTS
# Based on DGCA passenger traffic.
# Directional routes are intentionally kept separate.
# ---------------------------------------------------------------------------
ROUTE_WEIGHTS = {
    "DEL-BOM": 0.228895,
    "BOM-DEL": 0.228789,
    "DEL-BLR": 0.156997,
    "BOM-BLR": 0.135674,
    "DEL-CCU": 0.090393,
    "BLR-HYD": 0.077037,
    "MAA-DEL": 0.082215,
}


# ---------------------------------------------------------------------------
# ADVANCE-WINDOW WEIGHTS
# T+1 = 5
# T+7 = 5
# T+15 = 10
# T+30 = 30
# T+45 = 50
#
# They sum to 100 and are converted to relative weights by the
# weighted calculation.
# ---------------------------------------------------------------------------
ADVANCE_WINDOW_WEIGHTS = {
    "T+1": 5,
    "T+7": 10,
    "T+15": 15,
    "T+30": 20,
    "T+45": 50,
}


@dataclass(frozen=True)
class PipelineConfig:
    """Explicit, reproducible calculation parameters."""

    base_period_start: str
    base_period_end: str | None = None
    min_base_observations: int = 1
    weights: IndexWeights = field(default_factory=IndexWeights)
    min_daily_weight_coverage: float = 0.8
    weekly_min_valid_days: int = 4
    monthly_min_coverage: float = 0.7


@dataclass
class PipelineResult:
    frames: dict[str, pl.DataFrame]
    reports: dict[str, Any]
    configuration: PipelineConfig

    def summary(self) -> dict[str, Any]:
        validation = self.frames["validation"]
        normalized = self.frames["normalization"]
        windows = self.frames["advance_windows"]
        grouped = self.frames["fare_groups"]
        representative = self.frames["representative_fares"]
        daily = self.frames["daily_representatives"]
        price_relatives = self.frames["price_relatives"]
        daily_index = self.frames["daily_index"]

        return {
            "input_rows": validation.height,
            "valid_rows": validation.filter(
                pl.col("validation_status") == "VALID"
            ).height,
            "invalid_rows": validation.filter(
                pl.col("validation_status") == "INVALID"
            ).height,
            "normalized_fare_count": normalized.filter(
                pl.col("comparable_fare").is_not_null()
            ).height,
            "advance_window_counts": {
                row["advance_window"]: row["len"]
                for row in windows
                .filter(pl.col("advance_window").is_not_null())
                .group_by("advance_window")
                .len()
                .iter_rows(named=True)
            },
            "fare_group_count": grouped
                .filter(pl.col("fare_group_id").is_not_null())
                .select("fare_group_id")
                .n_unique(),
            "representative_fare_count": representative
                .filter(pl.col("representative_fare_status") == "CALCULATED")
                .select("fare_group_id")
                .n_unique(),
            "daily_representative_count": daily
                .filter(pl.col("daily_representative_status") == "CALCULATED")
                .height,
            "price_relative_count": price_relatives
                .filter(pl.col("price_relative_status") == "CALCULATED")
                .height,
            "daily_index": daily_index.to_dicts(),
            "weekly_index": self.frames["weekly_index"].to_dicts(),
            "monthly_index": self.frames["monthly_index"].to_dicts(),
            "weekly_inflation": self.frames["weekly_inflation"].to_dicts(),
            "monthly_inflation": self.frames["monthly_inflation"].to_dicts(),
        }


def map_scraper_schema(df: pl.DataFrame) -> pl.DataFrame:
    """Map known aliases only when the source did not already supply canonical names."""
    rename: dict[str, str] = {}

    for canonical, aliases in SCHEMA_ALIASES.items():
        if canonical in df.columns:
            continue

        source = next(
            (alias for alias in aliases if alias in df.columns),
            None,
        )

        if source:
            rename[source] = canonical

    return df.rename(rename) if rename else df


def _report_value(report: Any) -> Any:
    if hasattr(report, "summary"):
        return report.summary()

    if hasattr(report, "__dict__"):
        return (
            asdict(report)
            if hasattr(report, "__dataclass_fields__")
            else report.__dict__.copy()
        )

    return report


def run_pipeline(
    df: pl.DataFrame,
    config: PipelineConfig,
) -> PipelineResult:
    """Run all calculation stages once, preserving the record-level audit frame."""

    mapped = map_scraper_schema(df)

    validated = annotate_validation(mapped)

    valid_for_processing = validated.filter(
        pl.col("validation_status") == "VALID"
    )

    cleaned, cleaning_report = clean_airfare_quotes(valid_for_processing)

    normalized, normalization_report = normalize_fares(cleaned)

    windowed, window_report = assign_advance_windows(normalized)

    grouped, group_report = assign_fare_groups(windowed)

    outliers, outlier_report = detect_outliers(grouped)

    representative, representative_report = calculate_representative_fare(
        outliers
    )

    daily, daily_report = create_daily_representative(representative)

    price_relatives, price_relative_report = calculate_price_relative(
        daily,
        base_period_start=config.base_period_start,
        base_period_end=config.base_period_end,
        min_base_observations=config.min_base_observations,
    )

    # Use the configured verified route weights.
    # The equal-weight fallback is retained only when no route weights
    # are supplied by the caller.
    index_weights = config.weights

    if index_weights.route_weights is None:
        route_rows = windowed.filter(
            pl.col("origin_iata").is_not_null()
            & pl.col("destination_iata").is_not_null()
        ).select(
            (
                pl.col("origin_iata").cast(pl.String)
                + pl.lit("-")
                + pl.col("destination_iata").cast(pl.String)
            ).alias("route")
        ).unique()

        route_list = route_rows.get_column("route").to_list()

        inferred = (
            {route: 1.0 / len(route_list) for route in route_list}
            if route_list
            else None
        )

        index_weights = IndexWeights(
            route_weights=inferred,
            advance_window_weights=config.weights.advance_window_weights,
            label=config.weights.label,
        )

    daily_index, index_report = calculate_airfare_index(
        price_relatives,
        route_weights=index_weights.route_weights,
        advance_window_weights=index_weights.advance_window_weights,
        min_weight_coverage=config.min_daily_weight_coverage,
        weight_set=index_weights.label,
    )

    weekly, weekly_report = calculate_weekly_index(
        daily_index,
        config.weekly_min_valid_days,
    )

    monthly, monthly_report = calculate_monthly_index(
        daily_index,
        config.monthly_min_coverage,
    )

    weekly_inflation, weekly_inflation_report = calculate_weekly_inflation(
        weekly
    )

    monthly_inflation, monthly_inflation_report = calculate_monthly_inflation(
        monthly
    )

    frames = {
        "validation": validated,
        "cleaning": cleaned,
        "normalization": normalized,
        "advance_windows": windowed,
        "fare_groups": grouped,
        "outliers": outliers,
        "representative_fares": representative,
        "daily_representatives": daily,
        "price_relatives": price_relatives,
        "daily_index": daily_index,
        "weekly_index": weekly,
        "monthly_index": monthly,
        "weekly_inflation": weekly_inflation,
        "monthly_inflation": monthly_inflation,
    }

    reports = {
        "validation": {
            "input_rows": validated.height,
            "valid_rows": validated.filter(
                pl.col("validation_status") == "VALID"
            ).height,
            "invalid_rows": validated.filter(
                pl.col("validation_status") == "INVALID"
            ).height,
        },
        "cleaning": _report_value(cleaning_report),
        "normalization": _report_value(normalization_report),
        "advance_windows": _report_value(window_report),
        "fare_groups": _report_value(group_report),
        "outliers": _report_value(outlier_report),
        "representative_fares": _report_value(representative_report),
        "daily_representatives": _report_value(daily_report),
        "price_relatives": _report_value(price_relative_report),
        "daily_index": _report_value(index_report),
        "weekly_index": _report_value(weekly_report),
        "monthly_index": _report_value(monthly_report),
        "weekly_inflation": _report_value(weekly_inflation_report),
        "monthly_inflation": _report_value(monthly_inflation_report),
    }

    return PipelineResult(
        frames=frames,
        reports=reports,
        configuration=config,
    )


def run_pipeline_from_csv(
    path: str | Path,
    config: PipelineConfig,
) -> PipelineResult:
    """Read the scraper file exactly once and run the complete pipeline."""

    return run_pipeline(
        pl.read_csv(path, try_parse_dates=False),
        config,
    )


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the SIH airfare calculation pipeline."
    )

    parser.add_argument("input_csv", type=Path)

    parser.add_argument(
        "--base-start",
        required=True,
        help="YYYY-MM-DD",
    )

    parser.add_argument(
        "--base-end",
        help="YYYY-MM-DD; defaults to base-start",
    )

    args = parser.parse_args()

    # Use the verified route weights and configured advance-window weights.
    config = PipelineConfig(
        base_period_start=args.base_start,
        base_period_end=args.base_end,
        weights=IndexWeights(
            route_weights=ROUTE_WEIGHTS,
            advance_window_weights=ADVANCE_WINDOW_WEIGHTS,
            label="DGCA_ROUTE_AND_ADVANCE_WINDOW_WEIGHTS_2026",
        ),
    )

    result = run_pipeline_from_csv(
        args.input_csv,
        config,
    )

    print(
        json.dumps(
            result.summary(),
            default=str,
            indent=2,
        )
    )


if __name__ == "__main__":
    _main()