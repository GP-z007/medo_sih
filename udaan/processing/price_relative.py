"""
Stage 8B: Price Relative.

Converts daily representative fares into price relatives against
a configurable base period.

Price Relative =
    (Current Daily Representative Fare
     / Base-Period Representative Fare) * 100

The base-period representative fare is the median of all valid
daily representative fares in the selected base period for each
comparable fare stratum.

Comparable fare stratum:
    origin_iata
    destination_iata
    advance_window
    airline
    cabin_class
    stops
    fare_family
"""

from dataclasses import dataclass
from datetime import datetime

import polars as pl


COMPARISON_COLUMNS = [
    "origin_iata",
    "destination_iata",
    "advance_window",
    "airline",
    "cabin_class",
    "stops",
    "fare_family",
]

REQUIRED_COLUMNS = [
    "observation_date",
    *COMPARISON_COLUMNS,
    "daily_representative_fare",
    "daily_representative_observation_count",
    "daily_representative_status",
]

VALID_DAILY_STATUS = "CALCULATED"

STATUS_CALCULATED = "CALCULATED"
STATUS_BASE_MISSING = "BASE_FARE_MISSING"
STATUS_INSUFFICIENT_BASE = "INSUFFICIENT_BASE_COVERAGE"
STATUS_INVALID_CURRENT = "INVALID_CURRENT_FARE"


@dataclass
class PriceRelativeReport:
    """
    Processing summary for Stage 8B.
    """

    input_rows: int
    output_rows: int
    base_period_rows: int
    base_strata: int
    calculated_rows: int
    base_missing_rows: int
    invalid_current_rows: int
    warnings: list[str]


def calculate_price_relative(
    df: pl.DataFrame,
    base_period_start: str,
    base_period_end: str | None = None,
    min_base_observations: int = 1,
) -> tuple[pl.DataFrame, PriceRelativeReport]:
    """
    Calculate price relatives against a selected base period.

    Parameters
    ----------
    df:
        Output from Stage 8A daily_representative.py.

    base_period_start:
        First date of the base period in YYYY-MM-DD format.

    base_period_end:
        Last date of the base period in YYYY-MM-DD format.
        If omitted, only base_period_start is used.

    Returns
    -------
    result:
        Daily price-relative observations.

    report:
        Processing summary.
    """

    if min_base_observations < 1:
        raise ValueError("min_base_observations must be at least 1.")

    # ---------------------------------------------------------
    # 1. Handle empty input
    # ---------------------------------------------------------
    if df.is_empty():
        result = _empty_output()

        report = PriceRelativeReport(
            input_rows=0,
            output_rows=0,
            base_period_rows=0,
            base_strata=0,
            calculated_rows=0,
            base_missing_rows=0,
            invalid_current_rows=0,
            warnings=[],
        )

        return result, report

    # ---------------------------------------------------------
    # 2. Check required columns
    # ---------------------------------------------------------
    missing_columns = [
        column
        for column in REQUIRED_COLUMNS
        if column not in df.columns
    ]

    if missing_columns:
        result = _empty_output()

        report = PriceRelativeReport(
            input_rows=df.height,
            output_rows=0,
            base_period_rows=0,
            base_strata=0,
            calculated_rows=0,
            base_missing_rows=0,
            invalid_current_rows=0,
            warnings=[
                f"Missing required columns: {', '.join(missing_columns)}"
            ],
        )

        return result, report

    # ---------------------------------------------------------
    # 3. Normalize observation_date
    #
    # Accept:
    #   - Polars Date
    #   - datetime values
    #   - YYYY-MM-DD strings
    #   - timestamp strings
    #
    # Taking the first 10 characters gives YYYY-MM-DD.
    # ---------------------------------------------------------
    working = df.with_columns(
        pl.col("observation_date")
        .cast(pl.String)
        .str.slice(0, 10)
        .str.to_date(strict=False)
        .alias("observation_date")
    )

    # ---------------------------------------------------------
    # 4. Parse base-period start
    #
    # Use Python datetime instead of Polars strptime so invalid
    # user-supplied dates are caught cleanly.
    # ---------------------------------------------------------
    try:
        base_start = datetime.strptime(
            base_period_start,
            "%Y-%m-%d",
        ).date()

    except (ValueError, TypeError):
        result = _empty_output()

        report = PriceRelativeReport(
            input_rows=df.height,
            output_rows=0,
            base_period_rows=0,
            base_strata=0,
            calculated_rows=0,
            base_missing_rows=0,
            invalid_current_rows=0,
            warnings=[
                "Invalid base_period_start. Expected YYYY-MM-DD."
            ],
        )

        return result, report

    # ---------------------------------------------------------
    # 5. Parse base-period end
    # ---------------------------------------------------------
    if base_period_end is None:
        base_end = base_start

    else:
        try:
            base_end = datetime.strptime(
                base_period_end,
                "%Y-%m-%d",
            ).date()

        except (ValueError, TypeError):
            result = _empty_output()

            report = PriceRelativeReport(
                input_rows=df.height,
                output_rows=0,
                base_period_rows=0,
                base_strata=0,
                calculated_rows=0,
                base_missing_rows=0,
                invalid_current_rows=0,
                warnings=[
                    "Invalid base_period_end. Expected YYYY-MM-DD."
                ],
            )

            return result, report

    # ---------------------------------------------------------
    # 6. Validate base-period range
    # ---------------------------------------------------------
    if base_end < base_start:
        result = _empty_output()

        report = PriceRelativeReport(
            input_rows=df.height,
            output_rows=0,
            base_period_rows=0,
            base_strata=0,
            calculated_rows=0,
            base_missing_rows=0,
            invalid_current_rows=0,
            warnings=[
                "base_period_end cannot be earlier than base_period_start."
            ],
        )

        return result, report

    # ---------------------------------------------------------
    # 7. Select valid observations for base-period construction
    #
    # Only daily representatives with:
    #   - observation date inside base period
    #   - CALCULATED status
    #   - positive fare
    #
    # are allowed to construct the base fare.
    # ---------------------------------------------------------
    valid_base = working.filter(
        (pl.col("observation_date") >= base_start)
        & (pl.col("observation_date") <= base_end)
        & (
            pl.col("daily_representative_status")
            == VALID_DAILY_STATUS
        )
        & pl.col("daily_representative_fare").is_not_null()
        & (pl.col("daily_representative_fare") > 0)
    )

    # ---------------------------------------------------------
    # 8. Calculate base-period representative fare
    #
    # For every comparison stratum:
    #
    # Base fare = median of valid daily representative fares
    # within the selected base period.
    # ---------------------------------------------------------
    base_fares = (
        valid_base.group_by(
            COMPARISON_COLUMNS,
            maintain_order=True,
        )
        .agg(
            pl.col("daily_representative_fare")
            .median()
            .alias("base_period_representative_fare"),
            pl.len().alias("base_period_observation_count"),
        )
        .with_columns(
            pl.when(pl.col("base_period_observation_count") >= min_base_observations)
            .then(pl.lit("BASE_AVAILABLE"))
            .otherwise(pl.lit(STATUS_INSUFFICIENT_BASE))
            .alias("base_period_status")
        )
    )

    # ---------------------------------------------------------
    # 9. Attach base fare to every daily observation
    # ---------------------------------------------------------
    result = (
        working.join(
            base_fares,
            on=COMPARISON_COLUMNS,
            how="left",
        )
        .with_columns(
            pl.col("base_period_observation_count").fill_null(0).cast(pl.Int64),
            pl.col("base_period_status").fill_null(STATUS_BASE_MISSING),
        )
        # -----------------------------------------------------
        # 10. Calculate price relative
        #
        # Price Relative =
        #     current fare / base fare * 100
        #
        # Rounded to 2 decimal places to avoid floating-point
        # artifacts such as 110.00000000000001.
        # -----------------------------------------------------
        .with_columns(
            pl.when(
                (pl.col("daily_representative_status") == VALID_DAILY_STATUS)
                & pl.col("daily_representative_fare").is_not_null()
                & (pl.col("daily_representative_fare") > 0)
                & pl.col("base_period_representative_fare").is_not_null()
                & (pl.col("base_period_representative_fare") > 0)
                & (pl.col("base_period_status") == "BASE_AVAILABLE")
            )
            .then(
                (
                    pl.col("daily_representative_fare")
                    / pl.col("base_period_representative_fare")
                    * 100
                ).round(2)
            )
            .otherwise(None)
            .alias("price_relative")
        )
        # -----------------------------------------------------
        # 11. Assign price-relative status
        # -----------------------------------------------------
        .with_columns(
            pl.when(pl.col("price_relative").is_not_null())
            .then(pl.lit(STATUS_CALCULATED))
            .when(
                pl.col("daily_representative_status")
                != VALID_DAILY_STATUS
            )
            .then(pl.lit(STATUS_INVALID_CURRENT))
            .when(pl.col("base_period_status") == STATUS_INSUFFICIENT_BASE)
            .then(pl.lit(STATUS_INSUFFICIENT_BASE))
            .otherwise(pl.lit(STATUS_BASE_MISSING))
            .alias("price_relative_status")
        )
        # -----------------------------------------------------
        # 12. Keep exact Stage-8B output columns
        # -----------------------------------------------------
        .select(
            [
                "observation_date",
                *COMPARISON_COLUMNS,
                "daily_representative_fare",
                "base_period_representative_fare",
                "base_period_observation_count",
                "base_period_status",
                "price_relative",
                "price_relative_status",
            ]
        )
        .sort(
            [
                "observation_date",
                "origin_iata",
                "destination_iata",
                "advance_window",
                "airline",
                "cabin_class",
                "stops",
                "fare_family",
            ]
        )
    )

    # ---------------------------------------------------------
    # 13. Calculate report statistics
    # ---------------------------------------------------------
    calculated_rows = result.filter(
        pl.col("price_relative_status") == STATUS_CALCULATED
    ).height

    base_missing_rows = result.filter(
        pl.col("price_relative_status") == STATUS_BASE_MISSING
    ).height

    invalid_current_rows = result.filter(
        pl.col("price_relative_status") == STATUS_INVALID_CURRENT
    ).height

    report = PriceRelativeReport(
        input_rows=df.height,
        output_rows=result.height,
        base_period_rows=valid_base.height,
        base_strata=base_fares.height,
        calculated_rows=calculated_rows,
        base_missing_rows=base_missing_rows,
        invalid_current_rows=invalid_current_rows,
        warnings=[],
    )

    return result, report


def _empty_output() -> pl.DataFrame:
    """
    Return an empty DataFrame with the Stage-8B output schema.
    """

    schema = {
        "observation_date": pl.Date,
        "origin_iata": pl.String,
        "destination_iata": pl.String,
        "advance_window": pl.String,
        "airline": pl.String,
        "cabin_class": pl.String,
        "stops": pl.Int64,
        "fare_family": pl.String,
        "daily_representative_fare": pl.Float64,
        "base_period_representative_fare": pl.Float64,
        "base_period_observation_count": pl.Int64,
        "base_period_status": pl.String,
        "price_relative": pl.Float64,
        "price_relative_status": pl.String,
    }

    return pl.DataFrame(
        {
            column: pl.Series(
                column,
                [],
                dtype=dtype,
            )
            for column, dtype in schema.items()
        }
    )
