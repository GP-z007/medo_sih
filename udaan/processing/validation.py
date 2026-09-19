from dataclasses import dataclass, field

import polars as pl


# ------------------------------------------------------------------
# Required fields from the finalized scraper contract.
# ------------------------------------------------------------------
REQUIRED_COLUMNS = [
    "origin_iata",
    "destination_iata",
    "departure_date",
    "booking_date",
    "trip_type",
    "booking_lead_days",
    "airline",
    "flight_number",
    "stops",
    "fare_family",
    "displayed_fare",
    "base_fare",
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
    "total_payable",
    "cabin_class",
    "collection_timestamp",
    "currency",
    "scrape_status",
    "availability_status",
]


# ------------------------------------------------------------------
# Valid status values from the scraper.
# ------------------------------------------------------------------
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


@dataclass
class ValidationReport:
    is_valid: bool
    total_rows: int
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


ROW_REQUIRED_COLUMNS = (
    "origin_iata",
    "destination_iata",
    "departure_date",
    "booking_date",
    "booking_lead_days",
    "trip_type",
    "airline",
    "flight_number",
    "stops",
    "fare_family",
    "displayed_fare",
    "collection_timestamp",
    "currency",
    "scrape_status",
    "availability_status",
)


def annotate_validation(df: pl.DataFrame) -> pl.DataFrame:
    """Attach record-level validation status without dropping any source row.

    ``validate_airfare_data`` is retained as the legacy dataset-level report.
    The orchestration path uses this row-level result so an unusable record is
    preserved with a reproducible reason rather than disappearing downstream.
    Missing ``total_payable`` is not a structural validation failure: it is
    explicitly made price-ineligible in fare normalisation.
    """
    result = df
    if "source_row_id" not in result.columns:
        result = result.with_row_index("source_row_id")

    def missing_text(column: str) -> pl.Expr:
        if column not in result.columns:
            return pl.lit(True)
        return pl.col(column).is_null() | (pl.col(column).cast(pl.String).str.strip_chars() == "")

    def invalid_numeric(column: str, allow_missing: bool = False) -> pl.Expr:
        if column not in result.columns:
            return pl.lit(True)
        raw = pl.col(column)
        numeric = raw.cast(pl.Float64, strict=False)
        invalid = raw.is_not_null() & numeric.is_null()
        return invalid if allow_missing else (raw.is_null() | invalid)

    def invalid_date(column: str) -> pl.Expr:
        if column not in result.columns:
            return pl.lit(True)
        raw = pl.col(column).cast(pl.String)
        parsed = raw.str.slice(0, 10).str.to_date(strict=False)
        return raw.is_null() | (raw.str.strip_chars() == "") | parsed.is_null()

    reasons: list[pl.Expr] = []
    for column in ROW_REQUIRED_COLUMNS:
        if column in {"departure_date", "booking_date", "booking_lead_days", "stops", "displayed_fare"}:
            continue
        reasons.append(
            pl.when(missing_text(column)).then(pl.lit(f"MISSING_{column.upper()};")).otherwise(pl.lit(""))
        )

    if "origin_iata" in result.columns:
        origin = pl.col("origin_iata").cast(pl.String).str.strip_chars().str.to_uppercase()
        reasons.append(pl.when(~origin.str.contains(r"^[A-Z]{3}$")).then(pl.lit("INVALID_ORIGIN_IATA;")).otherwise(pl.lit("")))
    if "destination_iata" in result.columns:
        destination = pl.col("destination_iata").cast(pl.String).str.strip_chars().str.to_uppercase()
        reasons.append(pl.when(~destination.str.contains(r"^[A-Z]{3}$")).then(pl.lit("INVALID_DESTINATION_IATA;")).otherwise(pl.lit("")))
    if {"origin_iata", "destination_iata"}.issubset(result.columns):
        reasons.append(pl.when(
            pl.col("origin_iata").cast(pl.String).str.strip_chars().str.to_uppercase()
            == pl.col("destination_iata").cast(pl.String).str.strip_chars().str.to_uppercase()
        ).then(pl.lit("SAME_ORIGIN_DESTINATION;")).otherwise(pl.lit("")))

    for column in ("booking_date", "departure_date"):
        reasons.append(pl.when(invalid_date(column)).then(pl.lit(f"INVALID_{column.upper()};")).otherwise(pl.lit("")))

    lead = pl.col("booking_lead_days").cast(pl.Float64, strict=False) if "booking_lead_days" in result.columns else pl.lit(None, dtype=pl.Float64)
    reasons.append(pl.when(invalid_numeric("booking_lead_days") | (lead < 0)).then(pl.lit("INVALID_BOOKING_LEAD_DAYS;")).otherwise(pl.lit("")))
    if {"booking_date", "departure_date", "booking_lead_days"}.issubset(result.columns):
        booking = pl.col("booking_date").cast(pl.String).str.slice(0, 10).str.to_date(strict=False)
        departure = pl.col("departure_date").cast(pl.String).str.slice(0, 10).str.to_date(strict=False)
        reasons.append(pl.when(
            booking.is_not_null() & departure.is_not_null() & lead.is_not_null()
            & (lead != (departure - booking).dt.total_days())
        ).then(pl.lit("BOOKING_LEAD_MISMATCH;")).otherwise(pl.lit("")))

    displayed = pl.col("displayed_fare").cast(pl.Float64, strict=False) if "displayed_fare" in result.columns else pl.lit(None, dtype=pl.Float64)
    reasons.append(pl.when(invalid_numeric("displayed_fare") | (displayed <= 0)).then(pl.lit("INVALID_DISPLAYED_FARE;")).otherwise(pl.lit("")))
    stops = pl.col("stops").cast(pl.Float64, strict=False) if "stops" in result.columns else pl.lit(None, dtype=pl.Float64)
    reasons.append(pl.when(invalid_numeric("stops") | (stops < 0)).then(pl.lit("INVALID_STOPS;")).otherwise(pl.lit("")))
    if "currency" in result.columns:
        currency = pl.col("currency").cast(pl.String).str.strip_chars().str.to_uppercase()
        reasons.append(pl.when(~currency.is_in(["INR"])).then(pl.lit("UNSUPPORTED_CURRENCY;")).otherwise(pl.lit("")))
    if "scrape_status" in result.columns:
        scrape = pl.col("scrape_status").cast(pl.String).str.strip_chars().str.to_uppercase()
        reasons.append(pl.when(~scrape.is_in(list(VALID_SCRAPE_STATUSES))).then(pl.lit("INVALID_SCRAPE_STATUS;")).otherwise(pl.lit("")))
    if "availability_status" in result.columns:
        availability = pl.col("availability_status").cast(pl.String).str.strip_chars().str.to_uppercase()
        reasons.append(pl.when(~availability.is_in(list(VALID_AVAILABILITY_STATUSES))).then(pl.lit("INVALID_AVAILABILITY_STATUS;")).otherwise(pl.lit("")))
    if "total_payable" in result.columns:
        payable_raw = pl.col("total_payable")
        payable = payable_raw.cast(pl.Float64, strict=False)
        reasons.append(pl.when(
            payable_raw.is_not_null() & (payable.is_null() | (payable <= 0))
        ).then(pl.lit("INVALID_TOTAL_PAYABLE;")).otherwise(pl.lit("")))

    return result.with_columns(
        pl.concat_str(reasons).str.strip_chars(";").alias("validation_reason")
    ).with_columns(
        pl.when(pl.col("validation_reason") == "").then(pl.lit("VALID")).otherwise(pl.lit("INVALID")).alias("validation_status")
    )


def validate_airfare_data(df: pl.DataFrame) -> ValidationReport:
    """
    Validate raw airfare data received from the scraper service.

    Validation checks:
    - Required columns are present.
    - Dates and collection timestamp are parseable.
    - Booking date is not after departure date.
    - Booking lead days matches the booking/departure dates.
    - Booking lead days is non-negative.
    - IATA codes are valid three-letter codes.
    - Origin and destination are different.
    - Airline and trip type are present.
    - Cabin class column exists; missing values generate a warning.
    - Stops are numeric and non-negative.
    - Displayed fare and total payable are numeric and positive.
    - Fare components are numeric when values are provided.
    - Scrape status is present and valid.
    - Availability status is present and valid.

    Important:
    - PARTIAL scrape status is not automatically invalid.
    - FAILED scrape status is not automatically a validation error;
      it is a downstream index-eligibility concern.
    - SOLD_OUT and CANCELLED availability statuses are retained as
      valid observations and handled later for index eligibility.
    - UNKNOWN availability is valid and must not be assumed to mean
      either AVAILABLE or SOLD_OUT.
    - Missing optional fare-component values are allowed.
    """

    errors: list[str] = []
    warnings: list[str] = []

    total_rows = df.height

    # ---------------------------------------------------------
    # V001 - Required columns
    # ---------------------------------------------------------
    missing_columns = [
        column for column in REQUIRED_COLUMNS
        if column not in df.columns
    ]

    if missing_columns:
        errors.append(
            "V001: Missing required columns: "
            + ", ".join(missing_columns)
        )

        return ValidationReport(
            is_valid=False,
            total_rows=total_rows,
            errors=errors,
            warnings=warnings,
        )

    # ---------------------------------------------------------
    # V002 - Booking date must be parseable
    # ---------------------------------------------------------
    booking_date = _parse_date_column(df, "booking_date")

    if booking_date is None:
        errors.append(
            "V002: booking_date contains invalid or unparseable dates."
        )

    # ---------------------------------------------------------
    # V003 - Departure date must be parseable
    # ---------------------------------------------------------
    departure_date = _parse_date_column(df, "departure_date")

    if departure_date is None:
        errors.append(
            "V003: departure_date contains invalid or unparseable dates."
        )

    # ---------------------------------------------------------
    # V004 - Booking date cannot be after departure date
    # ---------------------------------------------------------
    if booking_date is not None and departure_date is not None:
        invalid_date_order = (
            df.with_columns(
                [
                    booking_date.alias("_booking_date"),
                    departure_date.alias("_departure_date"),
                ]
            )
            .filter(
                pl.col("_booking_date") > pl.col("_departure_date")
            )
            .height
        )

        if invalid_date_order > 0:
            errors.append(
                "V004: booking_date cannot be after departure_date."
            )

    # ---------------------------------------------------------
    # V005 - Booking lead days cannot be negative
    # ---------------------------------------------------------
    lead_days = _to_numeric(df, "booking_lead_days")

    if lead_days is None:
        errors.append(
            "V005: booking_lead_days contains non-numeric values."
        )
    else:
        negative_lead_days = (
            df.with_columns(
                lead_days.alias("_lead_days")
            )
            .filter(
                pl.col("_lead_days") < 0
            )
            .height
        )

        if negative_lead_days > 0:
            errors.append(
                "V005: booking_lead_days cannot be negative."
            )

    # ---------------------------------------------------------
    # V006 - Lead days must match booking/departure dates
    # ---------------------------------------------------------
    if (
        booking_date is not None
        and departure_date is not None
        and lead_days is not None
    ):
        lead_day_mismatch = (
            df.with_columns(
                [
                    booking_date.alias("_booking_date"),
                    departure_date.alias("_departure_date"),
                    lead_days.alias("_lead_days"),
                ]
            )
            .with_columns(
                (
                    pl.col("_departure_date")
                    - pl.col("_booking_date")
                )
                .dt.total_days()
                .alias("_calculated_lead_days")
            )
            .filter(
                pl.col("_lead_days")
                != pl.col("_calculated_lead_days")
            )
            .height
        )

        if lead_day_mismatch > 0:
            errors.append(
                "V006: booking_lead_days does not match "
                "departure_date - booking_date."
            )

    # ---------------------------------------------------------
    # V007 - Displayed fare must be numeric
    # ---------------------------------------------------------
    displayed_fare = _to_numeric(df, "displayed_fare")

    if displayed_fare is None:
        errors.append(
            "V007: displayed_fare contains non-numeric values."
        )

    # ---------------------------------------------------------
    # V008 - Displayed fare must be positive
    # ---------------------------------------------------------
    if displayed_fare is not None:
        invalid_displayed_fare = (
            df.with_columns(
                displayed_fare.alias("_displayed_fare")
            )
            .filter(
                pl.col("_displayed_fare") <= 0
            )
            .height
        )

        if invalid_displayed_fare > 0:
            errors.append(
                "V008: displayed_fare must be greater than zero."
            )

    # ---------------------------------------------------------
    # V009 - Currency
    # ---------------------------------------------------------
    if _has_missing_values(df, "currency"):
        errors.append(
            "V009: currency must be present for every record."
        )
    else:
        non_inr = (
            df.filter(
                pl.col("currency")
                .cast(pl.String)
                .str.strip_chars()
                .str.to_uppercase()
                != "INR"
            )
            .height
        )

        if non_inr > 0:
            warnings.append(
                "W001: Non-INR currency detected. "
                "Currency conversion may be required before index calculation."
            )

    # ---------------------------------------------------------
    # V010 - Origin IATA
    # ---------------------------------------------------------
    invalid_origin = _invalid_iata_count(df, "origin_iata")

    if invalid_origin > 0:
        errors.append(
            "V010: origin_iata must contain valid three-letter IATA codes."
        )

    # ---------------------------------------------------------
    # V011 - Destination IATA
    # ---------------------------------------------------------
    invalid_destination = _invalid_iata_count(
        df,
        "destination_iata",
    )

    if invalid_destination > 0:
        errors.append(
            "V011: destination_iata must contain valid three-letter IATA codes."
        )

    # ---------------------------------------------------------
    # V012 - Origin and destination cannot be identical
    # ---------------------------------------------------------
    same_route = (
        df.filter(
            pl.col("origin_iata")
            .cast(pl.String)
            .str.strip_chars()
            .str.to_uppercase()
            ==
            pl.col("destination_iata")
            .cast(pl.String)
            .str.strip_chars()
            .str.to_uppercase()
        )
        .height
    )

    if same_route > 0:
        errors.append(
            "V012: origin_iata and destination_iata cannot be identical."
        )

    # ---------------------------------------------------------
    # V013 - Airline must be present
    # ---------------------------------------------------------
    if _has_missing_values(df, "airline"):
        errors.append(
            "V013: airline must be present for every record."
        )

    # ---------------------------------------------------------
    # V014 - Trip type must be present
    # ---------------------------------------------------------
    if _has_missing_values(df, "trip_type"):
        errors.append(
            "V014: trip_type must be present for every record."
        )

    # ---------------------------------------------------------
    # V015 - Cabin class
    # ---------------------------------------------------------
    # Cabin class column must exist.
    # Missing values are allowed because some scraper sources
    # may not provide cabin information.
    if "cabin_class" not in df.columns:
        errors.append(
            "V015: Required column 'cabin_class' is missing."
        )
    else:
        missing_cabin_count = (
            df.select(
                pl.col("cabin_class")
                .is_null()
                .or_(
                    pl.col("cabin_class")
                    .cast(pl.String)
                    .str.strip_chars()
                    == ""
                )
                .sum()
            )
            .item()
        )

        if missing_cabin_count > 0:
            warnings.append(
                f"W003: {missing_cabin_count} rows have missing cabin_class."
            )

    # ---------------------------------------------------------
    # V016 - Scrape status
    # ---------------------------------------------------------
    scrape_status = (
        df["scrape_status"]
        .cast(pl.String)
        .str.strip_chars()
        .str.to_uppercase()
    )

    missing_scrape_status = (
        scrape_status.is_null()
        | (scrape_status == "")
    ).sum()

    if missing_scrape_status > 0:
        errors.append(
            f"V016: {missing_scrape_status} rows have missing "
            "scrape_status."
        )

    invalid_scrape_status = (
        scrape_status.is_not_null()
        & ~scrape_status.is_in(list(VALID_SCRAPE_STATUSES))
    ).sum()

    if invalid_scrape_status > 0:
        errors.append(
            f"V016: {invalid_scrape_status} rows have invalid "
            "scrape_status."
        )

    # ---------------------------------------------------------
    # V017 - Availability status
    # ---------------------------------------------------------
    availability_status = (
        df["availability_status"]
        .cast(pl.String)
        .str.strip_chars()
        .str.to_uppercase()
    )

    missing_availability_status = (
        availability_status.is_null()
        | (availability_status == "")
    ).sum()

    if missing_availability_status > 0:
        errors.append(
            f"V017: {missing_availability_status} rows have missing "
            "availability_status."
        )

    invalid_availability_status = (
        availability_status.is_not_null()
        & ~availability_status.is_in(
            list(VALID_AVAILABILITY_STATUSES)
        )
    ).sum()

    if invalid_availability_status > 0:
        errors.append(
            f"V017: {invalid_availability_status} rows have invalid "
            "availability_status."
        )

    # ---------------------------------------------------------
    # V018 - Stops must be numeric
    # ---------------------------------------------------------
    stops = _to_numeric(df, "stops")

    if stops is None:
        errors.append(
            "V018: stops contains non-numeric values."
        )
    else:
        negative_stops = (
            df.with_columns(
                stops.alias("_stops")
            )
            .filter(
                pl.col("_stops") < 0
            )
            .height
        )

        if negative_stops > 0:
            errors.append(
                "V018: stops cannot be negative."
            )

    # ---------------------------------------------------------
    # V019 - Collection timestamp
    # ---------------------------------------------------------
    collection_timestamp = _parse_datetime_column(
        df,
        "collection_timestamp",
    )

    if collection_timestamp is None:
        errors.append(
            "V019: collection_timestamp contains invalid "
            "or unparseable timestamps."
        )

    # ---------------------------------------------------------
    # V020 - Flight number
    # ---------------------------------------------------------
    if _has_missing_values(df, "flight_number"):
        warnings.append(
            "W002: flight_number is missing for one or more records."
        )

    # ---------------------------------------------------------
    # V021 - Total payable fare
    # ---------------------------------------------------------
    total_payable = _to_numeric(df, "total_payable")

    if total_payable is None:
        errors.append(
            "V021: total_payable contains non-numeric values."
        )
    else:
        invalid_total_payable = (
            df.with_columns(
                total_payable.alias("_total_payable")
            )
            .filter(
                pl.col("_total_payable").is_not_null()
                & (pl.col("_total_payable") <= 0)
            )
            .height
        )

        if invalid_total_payable > 0:
            errors.append(
                "V021: total_payable must be greater than zero "
                "when provided."
            )

    # ---------------------------------------------------------
    # V022 - Optional fare components must be numeric
    # ---------------------------------------------------------
    fare_components = [
        "base_fare",
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

    for column in fare_components:
        numeric_column = _to_numeric(df, column)

        if numeric_column is None:
            errors.append(
                f"V022: {column} contains non-numeric values."
            )

    return ValidationReport(
        is_valid=len(errors) == 0,
        total_rows=total_rows,
        errors=errors,
        warnings=warnings,
    )


def _parse_date_column(
    df: pl.DataFrame,
    column: str,
) -> pl.Series | None:
    """
    Parse a date column without changing the original DataFrame.
    """
    try:
        parsed = df.select(
            pl.col(column)
            .cast(pl.String)
            .str.strptime(
                pl.Date,
                format=None,
                strict=True,
            )
        ).to_series()

        return parsed

    except (pl.exceptions.PolarsError, ValueError):
        return None


def _parse_datetime_column(
    df: pl.DataFrame,
    column: str,
) -> pl.Series | None:
    """
    Parse a timestamp column without changing the original DataFrame.

    Supports timezone-aware timestamps such as:
    2026-09-13 10:59:46.951109+00:00
    """
    try:
        parsed = df.select(
            pl.col(column)
            .cast(pl.String)
            .str.to_datetime(
                format=None,
                strict=True,
                time_zone="UTC",
            )
        ).to_series()

        return parsed

    except (pl.exceptions.PolarsError, ValueError):
        return None


def _to_numeric(
    df: pl.DataFrame,
    column: str,
) -> pl.Series | None:
    """
    Convert a column to Float64 for validation.

    Returns None if any non-null value cannot be converted.
    Null values are allowed because some fare fields may
    legitimately be unavailable from a scraper.
    """
    try:
        original = df.get_column(column)

        numeric = original.cast(
            pl.Float64,
            strict=False,
        )

        invalid_non_null = (
            original.is_not_null()
            & numeric.is_null()
        ).any()

        if invalid_non_null:
            return None

        return numeric

    except (pl.exceptions.PolarsError, ValueError):
        return None


def _has_missing_values(
    df: pl.DataFrame,
    column: str,
) -> bool:
    """
    Return True when a required textual field contains
    null or blank values.
    """
    return (
        df.select(
            pl.col(column)
            .is_null()
            .or_(
                pl.col(column)
                .cast(pl.String)
                .str.strip_chars()
                == ""
            )
            .any()
        )
        .item()
    )


def _invalid_iata_count(
    df: pl.DataFrame,
    column: str,
) -> int:
    """
    Count invalid IATA codes.

    A valid IATA code is exactly three alphabetic characters.
    """
    return (
        df.filter(
            ~pl.col(column)
            .cast(pl.String)
            .str.strip_chars()
            .str.to_uppercase()
            .str.contains(r"^[A-Z]{3}$")
        )
        .height
    )
