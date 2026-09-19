"""Fare normalisation for the calculation pipeline.

``total_payable`` is the consumer-payable price and is deliberately the only
source eligible for a comparable fare. ``displayed_fare`` and every supplied
component remain in the output for audit, but are never used to invent or
replace a missing total.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import polars as pl


FARE_NUMERIC_COLUMNS = (
    "displayed_fare", "base_fare", "airline_surcharge", "fuel_surcharge",
    "GST", "UDF", "PSF", "ASF", "airport_fee", "convenience_fee",
    "other_tax", "other_fee", "total_tax", "total_fees", "discount",
    "total_payable",
)
SUPPORTED_CURRENCIES = {"INR"}
RECONCILIATION_TOLERANCE = 1.0


@dataclass
class FareNormalizationReport:
    input_rows: int = 0
    output_rows: int = 0
    numeric_columns_normalized: int = 0
    total_payable_used: int = 0
    # Retained for callers of the prior module. It must always be zero.
    displayed_fare_fallback_used: int = 0
    missing_comparable_fare: int = 0
    invalid_currency: int = 0
    fare_arithmetic_warnings: int = 0
    reconciliation_match: int = 0
    reconciliation_within_tolerance: int = 0
    reconciliation_mismatch: int = 0
    reconciliation_not_checkable: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _numeric(df: pl.DataFrame, column: str) -> pl.DataFrame:
    if column not in df.columns:
        return df
    return df.with_columns(
        pl.col(column).cast(pl.Float64, strict=False).alias(column)
    )


def _total_payable_columns(df: pl.DataFrame) -> pl.DataFrame:
    if "total_payable" not in df.columns:
        return df.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("comparable_fare"),
            pl.lit(None, dtype=pl.String).alias("comparable_fare_source"),
            pl.lit("MISSING").alias("total_payable_status"),
        )

    valid = pl.col("total_payable").is_not_null() & (pl.col("total_payable") > 0)
    return df.with_columns(
        pl.when(valid).then(pl.col("total_payable")).otherwise(
            pl.lit(None, dtype=pl.Float64)
        ).alias("comparable_fare"),
        pl.when(valid).then(pl.lit("TOTAL_PAYABLE")).otherwise(
            pl.lit(None, dtype=pl.String)
        ).alias("comparable_fare_source"),
        pl.when(pl.col("total_payable").is_null()).then(pl.lit("MISSING"))
        .when(pl.col("total_payable") <= 0).then(pl.lit("INVALID"))
        .otherwise(pl.lit("VALID")).alias("total_payable_status"),
    )


def _reconcile_components(df: pl.DataFrame) -> pl.DataFrame:
    """Compare a full supplied component breakdown without filling blanks.

    Detailed components are preferred. Aggregate tax/fee totals are used only
    when no detailed component columns are supplied, so totals are never
    double-counted.
    """
    if not {"base_fare", "total_payable"}.issubset(df.columns):
        return df.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("fare_reconciliation_difference"),
            pl.lit("NOT_CHECKABLE").alias("fare_reconciliation_status"),
            pl.lit(False).alias("fare_arithmetic_warning"),
        )

    detailed = [
        column for column in (
            "airline_surcharge", "fuel_surcharge", "GST", "UDF", "PSF", "ASF",
            "airport_fee", "convenience_fee", "other_tax", "other_fee",
        ) if column in df.columns
    ]
    aggregate = [] if detailed else [
        column for column in ("total_tax", "total_fees") if column in df.columns
    ]
    components = detailed or aggregate
    if not components:
        return df.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("fare_reconciliation_difference"),
            pl.lit("NOT_CHECKABLE").alias("fare_reconciliation_status"),
            pl.lit(False).alias("fare_arithmetic_warning"),
        )

    complete = pl.col("base_fare").is_not_null() & pl.col("total_payable").is_not_null()
    expected = pl.col("base_fare")
    for column in components:
        complete = complete & pl.col(column).is_not_null()
        expected = expected + pl.col(column)
    if "discount" in df.columns:
        complete = complete & pl.col("discount").is_not_null()
        expected = expected - pl.col("discount")

    difference = pl.when(complete).then(
        pl.col("total_payable") - expected
    ).otherwise(pl.lit(None, dtype=pl.Float64))
    return df.with_columns(
        difference.alias("fare_reconciliation_difference"),
    ).with_columns(
        pl.when(pl.col("fare_reconciliation_difference").is_null()).then(
            pl.lit("NOT_CHECKABLE")
        ).when(pl.col("fare_reconciliation_difference").abs() < 0.005).then(
            pl.lit("MATCH")
        ).when(pl.col("fare_reconciliation_difference").abs() <= RECONCILIATION_TOLERANCE).then(
            pl.lit("WITHIN_TOLERANCE")
        ).otherwise(pl.lit("MISMATCH")).alias("fare_reconciliation_status"),
    ).with_columns(
        (pl.col("fare_reconciliation_status") == "MISMATCH").alias("fare_arithmetic_warning"),
    )


def normalize_fares(df: pl.DataFrame) -> tuple[pl.DataFrame, FareNormalizationReport]:
    """Normalise supplied fare fields while preserving all input rows."""
    report = FareNormalizationReport(input_rows=df.height)
    processed = df.clone()
    for column in FARE_NUMERIC_COLUMNS:
        if column in processed.columns:
            processed = _numeric(processed, column)
            report.numeric_columns_normalized += 1
    if "currency" in processed.columns:
        processed = processed.with_columns(
            pl.col("currency").cast(pl.String, strict=False).str.strip_chars().str.to_uppercase().alias("currency")
        )
        report.invalid_currency = processed.filter(
            pl.col("currency").is_null() | ~pl.col("currency").is_in(SUPPORTED_CURRENCIES)
        ).height

    processed = _total_payable_columns(processed)
    processed = _reconcile_components(processed)
    report.output_rows = processed.height
    report.total_payable_used = processed.filter(
        pl.col("comparable_fare_source") == "TOTAL_PAYABLE"
    ).height
    report.missing_comparable_fare = processed.filter(
        pl.col("comparable_fare").is_null()
    ).height
    report.fare_arithmetic_warnings = processed.filter(
        pl.col("fare_arithmetic_warning")
    ).height
    for status, attribute in (
        ("MATCH", "reconciliation_match"),
        ("WITHIN_TOLERANCE", "reconciliation_within_tolerance"),
        ("MISMATCH", "reconciliation_mismatch"),
        ("NOT_CHECKABLE", "reconciliation_not_checkable"),
    ):
        setattr(report, attribute, processed.filter(
            pl.col("fare_reconciliation_status") == status
        ).height)
    if report.invalid_currency:
        report.warnings.append(
            f"{report.invalid_currency} rows have missing or unsupported currency."
        )
    if report.reconciliation_mismatch:
        report.warnings.append(
            f"{report.reconciliation_mismatch} rows have fare arithmetic reconciliation mismatches."
        )
    return processed, report
