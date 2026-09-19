from dataclasses import dataclass

import polars as pl


# V1 index uses one-way observations only.
V1_TRIP_TYPE = "ONE_WAY"

# These observations cannot contribute a fare price to the V1 index.
NON_PRICE_AVAILABILITY_STATUSES = {
    "SOLD_OUT",
    "CANCELLED",
}

NON_PRICE_SCRAPE_STATUSES = {
    "FAILED",
}

# Fields that define a comparable fare product.
GROUP_COLUMNS = [
    "origin_iata",
    "destination_iata",
    "departure_date",
    "advance_window",
    "airline",
    "cabin_class",
    "stops",
    "fare_family",
]


@dataclass
class FareGroupReport:
    input_rows: int
    output_rows: int
    eligible_rows: int
    ineligible_rows: int
    groups_created: int
    one_way_rows: int
    non_one_way_rows: int
    sold_out_rows: int
    cancelled_rows: int
    failed_scrape_rows: int
    missing_fare_family_rows: int
    missing_group_field_rows: int
    warnings: list[str]

    def summary(self) -> dict:
        return {
            "input_rows": self.input_rows,
            "output_rows": self.output_rows,
            "eligible_rows": self.eligible_rows,
            "ineligible_rows": self.ineligible_rows,
            "groups_created": self.groups_created,
            "one_way_rows": self.one_way_rows,
            "non_one_way_rows": self.non_one_way_rows,
            "sold_out_rows": self.sold_out_rows,
            "cancelled_rows": self.cancelled_rows,
            "failed_scrape_rows": self.failed_scrape_rows,
            "missing_fare_family_rows": self.missing_fare_family_rows,
            "missing_group_field_rows": self.missing_group_field_rows,
            "warnings": self.warnings,
        }


def _require_columns(df: pl.DataFrame) -> None:
    required = [
        *GROUP_COLUMNS,
        "trip_type",
        "availability_status",
        "scrape_status",
        "comparable_fare",
    ]

    missing = [column for column in required if column not in df.columns]

    if missing:
        raise ValueError(
            f"Missing required columns for fare grouping: {missing}"
        )


def create_fare_group_id(df: pl.DataFrame) -> pl.DataFrame:
    """
    Create a fare-group ID from the frozen grouping dimensions.

    This helper creates IDs for all rows passed to it.
    assign_fare_groups() uses it only for eligible observations.

    Missing grouping values are represented as <NULL> here.
    However, observations with missing grouping information are
    marked ineligible before IDs are assigned in the main function.
    """

    return (
        df.with_columns(
            pl.concat_str(
                [
                    pl.col(column)
                    .cast(pl.String)
                    .fill_null("<NULL>")
                    for column in GROUP_COLUMNS
                ],
                separator="|",
            ).alias("_fare_group_key")
        )
        .with_columns(
            pl.col("_fare_group_key")
            .hash(seed=0)
            .cast(pl.UInt64)
            .cast(pl.String)
            .map_elements(
                lambda value: f"FG_{value}",
                return_dtype=pl.String,
            )
            .alias("fare_group_id")
        )
        .drop("_fare_group_key")
    )


def assign_fare_groups(
    df: pl.DataFrame,
) -> tuple[pl.DataFrame, FareGroupReport]:
    """
    Assign comparable fare groups for the V1 airfare-index pipeline.

    Group identity:
        origin_iata
        destination_iata
        departure_date
        advance_window
        airline
        cabin_class
        stops
        fare_family

    V1 eligibility:
        - trip_type must be ONE_WAY
        - availability_status must not be SOLD_OUT/CANCELLED
        - scrape_status must not be FAILED
        - comparable_fare must be present and positive
        - all grouping dimensions must be present
        - fare_family must be present

    No observations are deleted.

    Eligible observations receive a fare_group_id.
    Ineligible observations are retained with fare_group_id = NULL.
    """

    _require_columns(df)

    input_rows = df.height

    result = df.with_columns(
        [
            (
                pl.col("trip_type")
                .cast(pl.String)
                .str.to_uppercase()
                == V1_TRIP_TYPE
            ).alias("is_v1_trip_type"),

            (
                ~pl.col("availability_status")
                .cast(pl.String)
                .str.to_uppercase()
                .is_in(NON_PRICE_AVAILABILITY_STATUSES)
            ).alias("is_available_for_price"),

            (
                ~pl.col("scrape_status")
                .cast(pl.String)
                .str.to_uppercase()
                .is_in(NON_PRICE_SCRAPE_STATUSES)
            ).alias("is_valid_scrape"),

            (
                pl.col("comparable_fare").is_not_null()
                & (pl.col("comparable_fare") > 0)
            ).alias("has_valid_comparable_fare"),
        ]
    )

    # Any missing grouping field makes the observation unsuitable
    # for comparable fare grouping.
    missing_group_expr = pl.any_horizontal(
        [
            pl.col(column).is_null()
            for column in GROUP_COLUMNS
        ]
    )

    result = result.with_columns(
        [
            missing_group_expr.alias("missing_group_information"),
            pl.col("fare_family")
            .is_null()
            .alias("missing_fare_family"),
        ]
    )

    # An observation must satisfy ALL V1 eligibility conditions.
    result = result.with_columns(
        (
            pl.col("is_v1_trip_type")
            & pl.col("is_available_for_price")
            & pl.col("is_valid_scrape")
            & pl.col("has_valid_comparable_fare")
            & ~pl.col("missing_group_information")
        ).alias("fare_group_eligible")
    )

    # Build the grouping key ONLY for eligible observations.
    result = result.with_columns(
        pl.when(pl.col("fare_group_eligible"))
        .then(
            pl.concat_str(
                [
                    pl.col(column).cast(pl.String)
                    for column in GROUP_COLUMNS
                ],
                separator="|",
            )
        )
        .otherwise(None)
        .alias("_fare_group_key")
    )

    # Create the group ID.
    # Ineligible observations remain NULL.
    result = result.with_columns(
        pl.when(pl.col("fare_group_eligible"))
        .then(
            pl.col("_fare_group_key")
            .hash(seed=0)
            .cast(pl.UInt64)
            .cast(pl.String)
            .map_elements(
                lambda value: f"FG_{value}",
                return_dtype=pl.String,
            )
        )
        .otherwise(None)
        .alias("fare_group_id")
    ).drop("_fare_group_key")

    # -------------------------
    # Report statistics
    # -------------------------

    one_way_rows = int(result["is_v1_trip_type"].sum())
    non_one_way_rows = input_rows - one_way_rows

    sold_out_rows = int(
        result["availability_status"]
        .cast(pl.String)
        .str.to_uppercase()
        .eq("SOLD_OUT")
        .sum()
    )

    cancelled_rows = int(
        result["availability_status"]
        .cast(pl.String)
        .str.to_uppercase()
        .eq("CANCELLED")
        .sum()
    )

    failed_scrape_rows = int(
        result["scrape_status"]
        .cast(pl.String)
        .str.to_uppercase()
        .eq("FAILED")
        .sum()
    )

    missing_fare_family_rows = int(
        result["missing_fare_family"].sum()
    )

    missing_group_field_rows = int(
        result["missing_group_information"].sum()
    )

    eligible_rows = int(
        result["fare_group_eligible"].sum()
    )

    ineligible_rows = input_rows - eligible_rows

    # Count only actual groups.
    # NULL fare_group_id does not represent a group.
    groups_created = int(
        result.filter(
            pl.col("fare_group_id").is_not_null()
        )["fare_group_id"].n_unique()
    )

    report = FareGroupReport(
        input_rows=input_rows,
        output_rows=result.height,
        eligible_rows=eligible_rows,
        ineligible_rows=ineligible_rows,
        groups_created=groups_created,
        one_way_rows=one_way_rows,
        non_one_way_rows=non_one_way_rows,
        sold_out_rows=sold_out_rows,
        cancelled_rows=cancelled_rows,
        failed_scrape_rows=failed_scrape_rows,
        missing_fare_family_rows=missing_fare_family_rows,
        missing_group_field_rows=missing_group_field_rows,
        warnings=[],
    )

    return result, report