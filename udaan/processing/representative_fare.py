from dataclasses import dataclass

import polars as pl


MIN_GROUP_SIZE = 4

STATUS_CALCULATED = "CALCULATED"
STATUS_INSUFFICIENT = "INSUFFICIENT_GROUP_DATA"
STATUS_NO_NORMAL = "NO_NORMAL_OBSERVATIONS"
STATUS_NO_VALID = "NO_VALID_OBSERVATIONS"
STATUS_NOT_ELIGIBLE = "NOT_ELIGIBLE"


@dataclass
class RepresentativeFareReport:
    input_rows: int
    output_rows: int
    eligible_rows: int
    not_eligible_rows: int

    groups_processed: int
    calculated_groups: int
    insufficient_groups: int
    no_normal_groups: int
    no_valid_groups: int

    calculated_rows: int

    warnings: list[str]

    def summary(self) -> dict:
        return self.__dict__.copy()


def _require_columns(df: pl.DataFrame) -> None:
    required = [
        "fare_group_id",
        "fare_group_eligible",
        "comparable_fare",
        "outlier_status",
    ]

    missing = [column for column in required if column not in df.columns]

    if missing:
        raise ValueError(
            f"Missing required columns for representative fare: {missing}"
        )


def calculate_representative_fare(
    df: pl.DataFrame,
) -> tuple[pl.DataFrame, RepresentativeFareReport]:

    _require_columns(df)

    input_rows = df.height

    # ---------------------------------------------------------
    # EMPTY DATAFRAME
    # ---------------------------------------------------------

    if input_rows == 0:

        result = df.with_columns(
            [
                pl.lit(None, dtype=pl.Float64).alias(
                    "representative_fare"
                ),
                pl.lit(
                    STATUS_NOT_ELIGIBLE,
                    dtype=pl.String,
                ).alias(
                    "representative_fare_status"
                ),
                pl.lit(
                    0,
                    dtype=pl.UInt32,
                ).alias(
                    "representative_fare_observation_count"
                ),
            ]
        )

        report = RepresentativeFareReport(
            input_rows=0,
            output_rows=0,
            eligible_rows=0,
            not_eligible_rows=0,
            groups_processed=0,
            calculated_groups=0,
            insufficient_groups=0,
            no_normal_groups=0,
            no_valid_groups=0,
            calculated_rows=0,
            warnings=[],
        )

        return result, report

    # ---------------------------------------------------------
    # ELIGIBLE OBSERVATIONS
    # ---------------------------------------------------------

    eligible = df.filter(
        pl.col("fare_group_eligible")
    )

    valid = eligible.filter(
        pl.col("comparable_fare").is_not_null()
        & (pl.col("comparable_fare") > 0)
    )

    # A statistical outlier is a data-quality flag, not an automatic
    # deletion rule. The representative is therefore the median of every
    # valid fare in the comparable group, including flagged observations.
    group_results = (
        valid
        .group_by("fare_group_id")
        .agg(
            pl.col("comparable_fare").median().alias("_rep"),
            pl.len().alias("_count"),
        )
        .with_columns(pl.lit(STATUS_CALCULATED).alias("_status"))
    )

    # ---------------------------------------------------------
    # ATTACH REPRESENTATIVE FARE TO ORIGINAL ROWS
    # ---------------------------------------------------------

    result = (
        df
        .join(
            group_results,
            on="fare_group_id",
            how="left",
        )
        .with_columns(
            [
                # Ineligible rows receive no representative fare.
                pl.when(
                    ~pl.col("fare_group_eligible")
                )
                .then(None)
                .otherwise(pl.col("_rep"))
                .cast(pl.Float64)
                .alias("representative_fare"),

                # Determine representative-fare status.
                pl.when(
                    ~pl.col("fare_group_eligible")
                )
                .then(
                    pl.lit(STATUS_NOT_ELIGIBLE)
                )
                .when(
                    pl.col("_status").is_null()
                )
                .then(
                    pl.lit(STATUS_NO_VALID)
                )
                .otherwise(
                    pl.col("_status")
                )
                .alias(
                    "representative_fare_status"
                ),

                # Number of observations actually used to
                # calculate the representative fare.
                pl.when(
                    ~pl.col("fare_group_eligible")
                )
                .then(0)
                .otherwise(
                    pl.col("_count").fill_null(0)
                )
                .cast(pl.UInt32)
                .alias(
                    "representative_fare_observation_count"
                ),
            ]
        )
        .drop(
            [
                "_rep",
                "_count",
                "_status",
            ]
        )
    )

    # ---------------------------------------------------------
    # REPORT COUNTS
    # ---------------------------------------------------------

    eligible_rows = int(
        df["fare_group_eligible"].sum()
    )

    not_eligible_rows = (
        input_rows - eligible_rows
    )

    # Distinct groups that contain at least one valid fare.
    groups_processed = valid.select(
        "fare_group_id"
    ).n_unique()

    # Group-level counts.
    calculated_groups = group_results.filter(
        pl.col("_status") == STATUS_CALCULATED
    ).select(
        "fare_group_id"
    ).n_unique()

    insufficient_groups = 0

    no_normal_groups = 0

    # Groups with no valid fare at all are eligible groups
    # whose group_id does not occur in `valid`.
    eligible_group_ids = eligible.select(
        "fare_group_id"
    ).unique()

    valid_group_ids = valid.select(
        "fare_group_id"
    ).unique()

    no_valid_groups = (
        eligible_group_ids
        .join(
            valid_group_ids,
            on="fare_group_id",
            how="anti",
        )
        .height
    )

    # Number of rows whose group received an actual
    # representative fare.
    calculated_rows = result.filter(
        pl.col("representative_fare_status")
        == STATUS_CALCULATED
    ).height

    report = RepresentativeFareReport(
        input_rows=input_rows,
        output_rows=result.height,
        eligible_rows=eligible_rows,
        not_eligible_rows=not_eligible_rows,
        groups_processed=int(groups_processed),
        calculated_groups=int(calculated_groups),
        insufficient_groups=int(insufficient_groups),
        no_normal_groups=int(no_normal_groups),
        no_valid_groups=int(no_valid_groups),
        calculated_rows=int(calculated_rows),
        warnings=[],
    )

    return result, report
