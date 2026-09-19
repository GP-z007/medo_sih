"""
Stage 8A: Daily representative fare.

Converts Stage-7 representative fares into one daily observation
per comparable fare stratum.

A comparable fare stratum is identified by:
    origin_iata
    destination_iata
    advance_window
    airline
    cabin_class
    stops
    fare_family

Multiple scraper observations of the same fare group on the same
calendar day contribute only once to the daily representative.
"""

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
    "collection_timestamp",
    "fare_group_id",
    "representative_fare",
    "representative_fare_status",
    *COMPARISON_COLUMNS,
]

VALID_REPRESENTATIVE_STATUSES = {
    "CALCULATED",
    "INSUFFICIENT_GROUP_DATA",
}

STATUS_CALCULATED = "CALCULATED"
STATUS_NO_VALID = "NO_VALID_OBSERVATIONS"


def create_daily_representative(
    df: pl.DataFrame,
) -> tuple[pl.DataFrame, dict]:
    """
    Create one daily representative fare for each comparable
    fare stratum.

    Parameters
    ----------
    df:
        Output from Stage 7 representative_fare.py.

    Returns
    -------
    result:
        Daily representative observations.

    report:
        Processing summary.
    """

    if df.is_empty():
        result = _empty_output()

        report = {
            "input_rows": 0,
            "output_rows": 0,
            "valid_input_rows": 0,
            "daily_observations": 0,
            "calculated_daily_observations": 0,
            "no_valid_daily_observations": 0,
            "unique_fare_groups_used": 0,
            "warnings": [],
        }

        return result, report

    missing_columns = [
        column for column in REQUIRED_COLUMNS
        if column not in df.columns
    ]

    if missing_columns:
        result = _empty_output()

        report = {
            "input_rows": df.height,
            "output_rows": 0,
            "valid_input_rows": 0,
            "daily_observations": 0,
            "calculated_daily_observations": 0,
            "no_valid_daily_observations": 0,
            "unique_fare_groups_used": 0,
            "warnings": [
                f"Missing required columns: {', '.join(missing_columns)}"
            ],
        }

        return result, report

    # collection_timestamp may contain timezone information such as:
    # 2026-09-13T10:00:00+05:30
    #
    # Stage 8A only needs the calendar date, so extract YYYY-MM-DD
    # directly instead of parsing the complete timezone-aware timestamp.
    working = df.with_columns(
        pl.col("collection_timestamp")
        .cast(pl.String)
        .str.slice(0, 10)
        .str.to_date(strict=False)
        .alias("observation_date")
    )

    # Identify every observed comparison stratum/day.
    all_daily_keys = working.select(
        [
            "observation_date",
            *COMPARISON_COLUMNS,
        ]
    ).unique()

    # Only Stage-7 representative fares that are actually usable
    # can contribute to the daily representative.
    valid = working.filter(
        pl.col("representative_fare_status").is_in(
            list(VALID_REPRESENTATIVE_STATUSES)
        )
        & pl.col("representative_fare").is_not_null()
        & (pl.col("representative_fare") > 0)
        & pl.col("fare_group_id").is_not_null()
        & pl.col("observation_date").is_not_null()
    )

    # Collapse repeated observations of the SAME fare group on the
    # SAME day before calculating the daily median.
    #
    # This prevents a scraper collecting one fare group multiple
    # times in a day from giving that fare group excessive influence.
    fare_group_daily = (
        valid.select(
            [
                "observation_date",
                *COMPARISON_COLUMNS,
                "fare_group_id",
                "representative_fare",
            ]
        )
        .unique(
            subset=[
                "observation_date",
                *COMPARISON_COLUMNS,
                "fare_group_id",
            ],
            maintain_order=True,
        )
    )

    # Calculate the daily representative as the median across
    # distinct contributing fare groups.
    calculated = (
        fare_group_daily.group_by(
            [
                "observation_date",
                *COMPARISON_COLUMNS,
            ],
            maintain_order=True,
        )
        .agg(
            pl.col("representative_fare")
            .median()
            .alias("daily_representative_fare"),
            pl.col("fare_group_id")
            .n_unique()
            .alias("daily_representative_observation_count"),
        )
        .with_columns(
            pl.lit(STATUS_CALCULATED).alias(
                "daily_representative_status"
            )
        )
    )

    # Keep observed comparison strata even when no valid Stage-7
    # representative fare exists for that day.
    result = (
        all_daily_keys.join(
            calculated,
            on=["observation_date", *COMPARISON_COLUMNS],
            how="left",
        )
        .with_columns(
            pl.when(
                pl.col("daily_representative_fare").is_not_null()
            )
            .then(pl.col("daily_representative_status"))
            .otherwise(pl.lit(STATUS_NO_VALID))
            .alias("daily_representative_status"),
            pl.col("daily_representative_observation_count")
            .fill_null(0)
            .cast(pl.Int64),
        )
        .select(
            [
                "observation_date",
                *COMPARISON_COLUMNS,
                "daily_representative_fare",
                "daily_representative_observation_count",
                "daily_representative_status",
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

    calculated_rows = result.filter(
        pl.col("daily_representative_status") == STATUS_CALCULATED
    ).height

    no_valid_rows = result.filter(
        pl.col("daily_representative_status") == STATUS_NO_VALID
    ).height

    report = {
        "input_rows": df.height,
        "output_rows": result.height,
        "valid_input_rows": valid.height,
        "daily_observations": result.height,
        "calculated_daily_observations": calculated_rows,
        "no_valid_daily_observations": no_valid_rows,
        "unique_fare_groups_used": (
            fare_group_daily
            .select("fare_group_id")
            .n_unique()
        ),
        "warnings": [],
    }

    return result, report


def _empty_output() -> pl.DataFrame:
    """
    Return an empty DataFrame with the Stage-8A output schema.
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
        "daily_representative_observation_count": pl.Int64,
        "daily_representative_status": pl.String,
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
