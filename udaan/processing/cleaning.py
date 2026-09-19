"""
Airfare quote cleaning module for SIH 2026.

Problem Statement:
    Real-time Airfare Price Index for India

Purpose:
    Clean airfare observations received from the scraper service before
    they enter fare normalization, advance-window grouping, fare grouping,
    outlier detection, and index calculation.

Input:
    Polars DataFrame following the scraper-to-processing contract.

Cleaning responsibilities:
    1. Normalize text fields.
    2. Normalize and retain scraper/availability status.
    3. Validate observed displayed fare.
    4. Keep only INR observations.
    5. Validate route identity.
    6. Validate stops.
    7. Normalize airline, cabin, trip type, fare family, and flight number.
    8. Remove exact duplicate records.
    9. Preserve missing optional fare components as missing.

Important:
    - Missing fare components are NOT converted to zero.
    - PARTIAL scraper observations are retained.
    - SOLD_OUT and CANCELLED observations are retained.
    - UNKNOWN availability is not assumed to mean SOLD_OUT or AVAILABLE.
    - FAILED scraper observations are retained for audit/history.
    - Index eligibility is decided downstream and is NOT silently
      implemented as row deletion in this cleaning module.

Out of scope:
    - Fare normalization / comparable fare construction.
    - Advance-window assignment.
    - Comparable fare grouping.
    - Statistical outlier detection.
    - Representative fare calculation.
    - Price relatives.
    - Route weighting.
    - National airfare index calculation.
    - Final index eligibility classification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import polars as pl


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_CURRENCIES = {"INR"}

VALID_CABIN_CLASSES = {
    "ECONOMY",
    "PREMIUM ECONOMY",
    "BUSINESS",
    "FIRST",
}

VALID_TRIP_TYPES = {
    "ONE_WAY",
    "ROUND_TRIP",
    "MULTI_CITY",
}

VALID_SCRAPE_STATUSES = {
    "SUCCESS",
    "PARTIAL",
    "FAILED",
}

VALID_AVAILABILITY_STATUSES = {
    "AVAILABLE",
    "SOLD_OUT",
    "CANCELLED",
    "UNKNOWN",
}


# ---------------------------------------------------------------------------
# Report object
# ---------------------------------------------------------------------------

@dataclass
class CleaningReport:
    """Summary of operations performed during cleaning."""

    input_rows: int = 0
    output_rows: int = 0

    invalid_fares: int = 0
    invalid_currency: int = 0
    invalid_routes: int = 0
    invalid_stops: int = 0

    exact_duplicates_removed: int = 0

    normalized_airlines: int = 0
    normalized_cabins: int = 0
    normalized_trip_types: int = 0
    normalized_fare_families: int = 0
    normalized_flight_numbers: int = 0

    normalized_scrape_statuses: int = 0
    normalized_availability_statuses: int = 0

    scrape_status_counts: dict[str, int] = field(
        default_factory=dict
    )

    availability_status_counts: dict[str, int] = field(
        default_factory=dict
    )

    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """Return the cleaning report as a dictionary."""

        return {
            "input_rows": self.input_rows,
            "output_rows": self.output_rows,
            "invalid_fares": self.invalid_fares,
            "invalid_currency": self.invalid_currency,
            "invalid_routes": self.invalid_routes,
            "invalid_stops": self.invalid_stops,
            "exact_duplicates_removed": self.exact_duplicates_removed,
            "normalized_airlines": self.normalized_airlines,
            "normalized_cabins": self.normalized_cabins,
            "normalized_trip_types": self.normalized_trip_types,
            "normalized_fare_families": self.normalized_fare_families,
            "normalized_flight_numbers": self.normalized_flight_numbers,
            "normalized_scrape_statuses": (
                self.normalized_scrape_statuses
            ),
            "normalized_availability_statuses": (
                self.normalized_availability_statuses
            ),
            "scrape_status_counts": self.scrape_status_counts,
            "availability_status_counts": (
                self.availability_status_counts
            ),
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _normalize_text_column(
    df: pl.DataFrame,
    column: str,
) -> tuple[pl.DataFrame, int]:
    """
    Strip surrounding whitespace and uppercase a text column.

    Missing values remain missing.
    """

    if column not in df.columns:
        return df, 0

    before = df[column].to_list()

    df = df.with_columns(
        pl.col(column)
        .cast(pl.Utf8, strict=False)
        .str.strip_chars()
        .str.to_uppercase()
        .alias(column)
    )

    after = df[column].to_list()

    changed = sum(
        1
        for old, new in zip(before, after)
        if old is not None
        and new is not None
        and old != new
    )

    return df, changed


def _normalize_airline_column(
    df: pl.DataFrame,
    report: CleaningReport,
) -> pl.DataFrame:
    """Normalize airline names conservatively."""

    df, changed = _normalize_text_column(
        df,
        "airline",
    )

    report.normalized_airlines = changed

    return df


def _normalize_cabin_column(
    df: pl.DataFrame,
    report: CleaningReport,
) -> pl.DataFrame:
    """
    Normalize cabin labels.

    Missing cabin values are retained as null because the scraper contract
    allows the field to be present while individual values may be missing.
    """

    df, changed = _normalize_text_column(
        df,
        "cabin_class",
    )

    report.normalized_cabins = changed

    return df


def _normalize_trip_type_column(
    df: pl.DataFrame,
    report: CleaningReport,
) -> pl.DataFrame:
    """Normalize trip type labels."""

    df, changed = _normalize_text_column(
        df,
        "trip_type",
    )

    report.normalized_trip_types = changed

    return df


def _normalize_fare_family_column(
    df: pl.DataFrame,
    report: CleaningReport,
) -> pl.DataFrame:
    """Normalize fare-family labels when supplied."""

    df, changed = _normalize_text_column(
        df,
        "fare_family",
    )

    report.normalized_fare_families = changed

    return df


def _normalize_flight_number_column(
    df: pl.DataFrame,
    report: CleaningReport,
) -> pl.DataFrame:
    """
    Normalize flight numbers conservatively.

    We do not remove or rewrite airline-specific formatting.
    """

    df, changed = _normalize_text_column(
        df,
        "flight_number",
    )

    report.normalized_flight_numbers = changed

    return df


def _normalize_currency(
    df: pl.DataFrame,
) -> pl.DataFrame:
    """Normalize currency codes without performing currency conversion."""

    if "currency" not in df.columns:
        return df

    return df.with_columns(
        pl.col("currency")
        .cast(pl.Utf8, strict=False)
        .str.strip_chars()
        .str.to_uppercase()
        .alias("currency")
    )


def _normalize_status_column(
    df: pl.DataFrame,
    column: str,
) -> tuple[pl.DataFrame, int]:
    """
    Normalize a status column.

    Status values are stripped and uppercased.
    Missing values remain missing.

    No status is converted into another status.
    """

    if column not in df.columns:
        return df, 0

    before = df[column].to_list()

    df = df.with_columns(
        pl.col(column)
        .cast(pl.Utf8, strict=False)
        .str.strip_chars()
        .str.to_uppercase()
        .alias(column)
    )

    after = df[column].to_list()

    changed = sum(
        1
        for old, new in zip(before, after)
        if old is not None
        and new is not None
        and old != new
    )

    return df, changed


def _normalize_scrape_status(
    df: pl.DataFrame,
    report: CleaningReport,
) -> pl.DataFrame:
    """
    Normalize scrape_status.

    Valid values:
        SUCCESS
        PARTIAL
        FAILED

    The cleaning stage does not remove FAILED rows.
    """

    if "scrape_status" not in df.columns:
        report.warnings.append(
            "scrape_status column is missing; status normalization "
            "could not be performed."
        )
        return df

    df, changed = _normalize_status_column(
        df,
        "scrape_status",
    )

    report.normalized_scrape_statuses = changed

    invalid_statuses = (
        df.filter(
            pl.col("scrape_status").is_not_null()
            & ~pl.col("scrape_status").is_in(
                list(VALID_SCRAPE_STATUSES)
            )
        )
        .height
    )

    if invalid_statuses > 0:
        report.warnings.append(
            f"{invalid_statuses} rows contain invalid scrape_status "
            "values. They are retained for validation/error handling."
        )

    missing_statuses = (
        df.filter(
            pl.col("scrape_status").is_null()
            | (pl.col("scrape_status") == "")
        )
        .height
    )

    if missing_statuses > 0:
        report.warnings.append(
            f"{missing_statuses} rows have missing scrape_status."
        )

    report.scrape_status_counts = _status_counts(
        df,
        "scrape_status",
    )

    return df


def _normalize_availability_status(
    df: pl.DataFrame,
    report: CleaningReport,
) -> pl.DataFrame:
    """
    Normalize availability_status.

    Valid values:
        AVAILABLE
        SOLD_OUT
        CANCELLED
        UNKNOWN

    Important:
        UNKNOWN is preserved as UNKNOWN.
        SOLD_OUT and CANCELLED are retained.
    """

    if "availability_status" not in df.columns:
        report.warnings.append(
            "availability_status column is missing; availability "
            "normalization could not be performed."
        )
        return df

    df, changed = _normalize_status_column(
        df,
        "availability_status",
    )

    report.normalized_availability_statuses = changed

    invalid_statuses = (
        df.filter(
            pl.col("availability_status").is_not_null()
            & ~pl.col("availability_status").is_in(
                list(VALID_AVAILABILITY_STATUSES)
            )
        )
        .height
    )

    if invalid_statuses > 0:
        report.warnings.append(
            f"{invalid_statuses} rows contain invalid "
            "availability_status values. They are retained for "
            "validation/error handling."
        )

    missing_statuses = (
        df.filter(
            pl.col("availability_status").is_null()
            | (pl.col("availability_status") == "")
        )
        .height
    )

    if missing_statuses > 0:
        report.warnings.append(
            f"{missing_statuses} rows have missing availability_status."
        )

    report.availability_status_counts = _status_counts(
        df,
        "availability_status",
    )

    return df


def _status_counts(
    df: pl.DataFrame,
    column: str,
) -> dict[str, int]:
    """Return non-null status frequencies."""

    if column not in df.columns:
        return {}

    result = (
        df.group_by(column)
        .len()
        .sort(column)
    )

    counts: dict[str, int] = {}

    for row in result.iter_rows():
        status, count = row

        if status is not None:
            counts[str(status)] = int(count)

    return counts


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_displayed_fare(
    df: pl.DataFrame,
    report: CleaningReport,
) -> pl.DataFrame:
    """
    Validate the observed displayed fare.

    Invalid:
        - missing
        - non-numeric
        - zero
        - negative

    No fare is inferred from another column.
    """

    if "displayed_fare" not in df.columns:
        report.warnings.append(
            "displayed_fare column is missing; fare validation "
            "could not be performed."
        )
        return df

    df = df.with_columns(
        pl.col("displayed_fare")
        .cast(pl.Float64, strict=False)
        .alias("displayed_fare")
    )

    invalid_mask = (
        pl.col("displayed_fare").is_null()
        | (pl.col("displayed_fare") <= 0)
    )

    report.invalid_fares = df.filter(invalid_mask).height

    return df.filter(~invalid_mask)


def _validate_currency(
    df: pl.DataFrame,
    report: CleaningReport,
) -> pl.DataFrame:
    """
    Keep only explicitly INR observations.

    No exchange-rate conversion is performed.
    """

    if "currency" not in df.columns:
        report.warnings.append(
            "currency column is missing; currency filtering "
            "could not be performed."
        )
        return df

    invalid_mask = (
        pl.col("currency").is_null()
        | (~pl.col("currency").is_in(SUPPORTED_CURRENCIES))
    )

    report.invalid_currency = df.filter(invalid_mask).height

    return df.filter(~invalid_mask)


def _validate_routes(
    df: pl.DataFrame,
    report: CleaningReport,
) -> pl.DataFrame:
    """
    Validate origin and destination IATA codes.

    Requirements:
        - exactly three alphabetic characters
        - origin != destination
    """

    required_columns = {
        "origin_iata",
        "destination_iata",
    }

    missing = required_columns - set(df.columns)

    if missing:
        report.warnings.append(
            "Route validation skipped; missing columns: "
            f"{sorted(missing)}"
        )
        return df

    df = df.with_columns(
        [
            pl.col("origin_iata")
            .cast(pl.Utf8, strict=False)
            .str.strip_chars()
            .str.to_uppercase()
            .alias("origin_iata"),

            pl.col("destination_iata")
            .cast(pl.Utf8, strict=False)
            .str.strip_chars()
            .str.to_uppercase()
            .alias("destination_iata"),
        ]
    )

    invalid_mask = (
        pl.col("origin_iata").is_null()
        | pl.col("destination_iata").is_null()
        | (~pl.col("origin_iata").str.contains(r"^[A-Z]{3}$"))
        | (~pl.col("destination_iata").str.contains(r"^[A-Z]{3}$"))
        | (pl.col("origin_iata") == pl.col("destination_iata"))
    )

    report.invalid_routes = df.filter(invalid_mask).height

    return df.filter(~invalid_mask)


def _validate_stops(
    df: pl.DataFrame,
    report: CleaningReport,
) -> pl.DataFrame:
    """
    Validate stops.

    Missing stops remain missing.

    Negative stops are invalid.

    We deliberately do not impose an arbitrary maximum because the
    scraper contract does not specify one.
    """

    if "stops" not in df.columns:
        report.warnings.append(
            "stops column is missing; stop validation "
            "could not be performed."
        )
        return df

    df = df.with_columns(
        pl.col("stops")
        .cast(pl.Int64, strict=False)
        .alias("stops")
    )

    invalid_mask = (
        pl.col("stops").is_not_null()
        & (pl.col("stops") < 0)
    )

    report.invalid_stops = df.filter(invalid_mask).height

    return df.filter(~invalid_mask)


# ---------------------------------------------------------------------------
# Duplicate handling
# ---------------------------------------------------------------------------

def _remove_exact_duplicates(
    df: pl.DataFrame,
    report: CleaningReport,
) -> pl.DataFrame:
    """
    Remove only exact duplicate records.

    An exact duplicate means every source-data value is identical. Internal
    lineage columns (currently ``source_row_id``) do not alter source identity,
    so the first source identifier is retained when they are removed.

    Same fare at different collection timestamps:
        keep both.

    Same complete record at the same collection timestamp:
        remove the duplicate.
    """

    before = df.height

    source_columns = [column for column in df.columns if column != "source_row_id"]
    df = df.unique(subset=source_columns, maintain_order=True)

    report.exact_duplicates_removed = before - df.height

    return df


# ---------------------------------------------------------------------------
# Main cleaning function
# ---------------------------------------------------------------------------

def clean_airfare_quotes(
    df: pl.DataFrame,
) -> tuple[pl.DataFrame, CleaningReport]:
    """
    Clean airfare quote observations.

    Parameters
    ----------
    df:
        Polars DataFrame received from the scraper/API layer.

    Returns
    -------
    cleaned_df, report
    """

    report = CleaningReport(
        input_rows=df.height,
    )

    if df.is_empty():
        report.warnings.append(
            "Input DataFrame is empty."
        )
        return df.clone(), report

    cleaned = df.clone()

    # ---------------------------------------------------------------
    # 1. Normalize text fields
    # ---------------------------------------------------------------

    cleaned = _normalize_airline_column(
        cleaned,
        report,
    )

    cleaned = _normalize_cabin_column(
        cleaned,
        report,
    )

    cleaned = _normalize_trip_type_column(
        cleaned,
        report,
    )

    cleaned = _normalize_fare_family_column(
        cleaned,
        report,
    )

    cleaned = _normalize_flight_number_column(
        cleaned,
        report,
    )

    cleaned = _normalize_currency(cleaned)

    # ---------------------------------------------------------------
    # 2. Normalize scraper and availability status
    #
    # These are retained. They are NOT filtering criteria here.
    # ---------------------------------------------------------------

    cleaned = _normalize_scrape_status(
        cleaned,
        report,
    )

    cleaned = _normalize_availability_status(
        cleaned,
        report,
    )

    # ---------------------------------------------------------------
    # 3. Validate displayed fare
    # ---------------------------------------------------------------

    cleaned = _validate_displayed_fare(
        cleaned,
        report,
    )

    # ---------------------------------------------------------------
    # 4. Keep only INR observations
    # ---------------------------------------------------------------

    cleaned = _validate_currency(
        cleaned,
        report,
    )

    # ---------------------------------------------------------------
    # 5. Validate route
    # ---------------------------------------------------------------

    cleaned = _validate_routes(
        cleaned,
        report,
    )

    # ---------------------------------------------------------------
    # 6. Validate stops
    # ---------------------------------------------------------------

    cleaned = _validate_stops(
        cleaned,
        report,
    )

    # ---------------------------------------------------------------
    # 7. Remove exact duplicate records
    # ---------------------------------------------------------------

    cleaned = _remove_exact_duplicates(
        cleaned,
        report,
    )

    # ---------------------------------------------------------------
    # 8. Preserve missing optional fare components
    # ---------------------------------------------------------------

    optional_fare_components = [
        "airline_surcharge",
        "fuel_surcharge",
        "GST",
        "UDF",
        "PSF",
        "ASF",
        "airport_fee",
        "convenience_fee",
        "other_tax",
        "other_fee",
        "total_tax",
        "total_fees",
        "discount",
    ]

    missing_optional_fields = [
        column
        for column in optional_fare_components
        if column not in cleaned.columns
    ]

    if missing_optional_fields:
        report.warnings.append(
            "Optional fare components not supplied by scraper: "
            f"{missing_optional_fields}"
        )

    # ---------------------------------------------------------------
    # 9. Final report
    # ---------------------------------------------------------------

    report.output_rows = cleaned.height

    return cleaned, report


# ---------------------------------------------------------------------------
# CSV convenience function
# ---------------------------------------------------------------------------

def clean_airfare_csv(
    input_path: str,
    output_path: str | None = None,
) -> tuple[pl.DataFrame, CleaningReport]:
    """
    Read a scraper CSV, clean it, and optionally save the cleaned result.
    """

    df = pl.read_csv(
        input_path,
        try_parse_dates=True,
    )

    cleaned_df, report = clean_airfare_quotes(df)

    if output_path is not None:
        cleaned_df.write_csv(output_path)

    return cleaned_df, report


# ---------------------------------------------------------------------------
# Manual execution
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Airfare cleaning module loaded successfully.")
    print(
        "Use clean_airfare_quotes(df) or "
        "clean_airfare_csv(input_path)."
    )
