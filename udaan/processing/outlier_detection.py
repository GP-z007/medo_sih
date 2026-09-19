from dataclasses import dataclass

import polars as pl


# Minimum number of observations required for IQR-based detection.
MIN_GROUP_SIZE = 4

# Standard Tukey IQR multiplier.
IQR_MULTIPLIER = 1.5

OUTLIER_STATUS_NORMAL = "NORMAL"
OUTLIER_STATUS_STATISTICAL = "STATISTICAL_OUTLIER"
OUTLIER_STATUS_INSUFFICIENT = "INSUFFICIENT_GROUP_DATA"
OUTLIER_STATUS_NOT_ELIGIBLE = "NOT_ELIGIBLE"


@dataclass
class OutlierDetectionReport:
    input_rows: int
    output_rows: int
    eligible_rows: int
    not_eligible_rows: int
    groups_analyzed: int
    insufficient_groups: int
    normal_rows: int
    outlier_rows: int
    insufficient_rows: int
    warnings: list[str]

    def summary(self) -> dict:
        return {
            "input_rows": self.input_rows,
            "output_rows": self.output_rows,
            "eligible_rows": self.eligible_rows,
            "not_eligible_rows": self.not_eligible_rows,
            "groups_analyzed": self.groups_analyzed,
            "insufficient_groups": self.insufficient_groups,
            "normal_rows": self.normal_rows,
            "outlier_rows": self.outlier_rows,
            "insufficient_rows": self.insufficient_rows,
            "warnings": self.warnings,
        }


def _require_columns(df: pl.DataFrame) -> None:
    required = [
        "fare_group_id",
        "fare_group_eligible",
        "comparable_fare",
    ]

    missing = [column for column in required if column not in df.columns]

    if missing:
        raise ValueError(
            f"Missing required columns for outlier detection: {missing}"
        )


def detect_outliers(
    df: pl.DataFrame,
) -> tuple[pl.DataFrame, OutlierDetectionReport]:
    """
    Detect statistical airfare outliers within comparable fare groups.

    Method:
        - Only fare_group_eligible rows are analyzed.
        - Groups with fewer than MIN_GROUP_SIZE observations are not
          statistically evaluated.
        - For eligible groups, use Tukey's IQR rule:
              IQR = Q3 - Q1
              lower = Q1 - 1.5 * IQR
              upper = Q3 + 1.5 * IQR
        - A fare below lower or above upper is a statistical outlier.
        - No rows are deleted.
        - Ineligible rows are retained and marked NOT_ELIGIBLE.

    Output columns added:
        outlier_group_size
        outlier_q1
        outlier_q3
        outlier_iqr
        outlier_lower_bound
        outlier_upper_bound
        outlier_status
    """

    _require_columns(df)

    input_rows = df.height

    if input_rows == 0:
        result = df.with_columns(
            [
                pl.lit(None, dtype=pl.UInt32).alias("outlier_group_size"),
                pl.lit(None, dtype=pl.Float64).alias("outlier_q1"),
                pl.lit(None, dtype=pl.Float64).alias("outlier_q3"),
                pl.lit(None, dtype=pl.Float64).alias("outlier_iqr"),
                pl.lit(None, dtype=pl.Float64).alias("outlier_lower_bound"),
                pl.lit(None, dtype=pl.Float64).alias("outlier_upper_bound"),
                pl.lit(OUTLIER_STATUS_NOT_ELIGIBLE, dtype=pl.String).alias(
                    "outlier_status"
                ),
            ]
        )

        report = OutlierDetectionReport(
            input_rows=0,
            output_rows=0,
            eligible_rows=0,
            not_eligible_rows=0,
            groups_analyzed=0,
            insufficient_groups=0,
            normal_rows=0,
            outlier_rows=0,
            insufficient_rows=0,
            warnings=[],
        )
        return result, report

    # Only eligible observations participate in statistical detection.
    eligible = df.filter(pl.col("fare_group_eligible"))

    group_stats = (
        eligible.group_by("fare_group_id")
        .agg(
            [
                pl.len().alias("outlier_group_size"),
                pl.col("comparable_fare")
                .quantile(0.25, interpolation="linear")
                .alias("outlier_q1"),
                pl.col("comparable_fare")
                .quantile(0.75, interpolation="linear")
                .alias("outlier_q3"),
            ]
        )
        .with_columns(
            [
                (
                    pl.col("outlier_q3") - pl.col("outlier_q1")
                ).alias("outlier_iqr"),
            ]
        )
        .with_columns(
            [
                (
                    pl.col("outlier_q1")
                    - IQR_MULTIPLIER * pl.col("outlier_iqr")
                ).alias("outlier_lower_bound"),
                (
                    pl.col("outlier_q3")
                    + IQR_MULTIPLIER * pl.col("outlier_iqr")
                ).alias("outlier_upper_bound"),
            ]
        )
    )

    result = df.join(group_stats, on="fare_group_id", how="left")

    result = result.with_columns(
        pl.when(~pl.col("fare_group_eligible"))
        .then(pl.lit(OUTLIER_STATUS_NOT_ELIGIBLE))
        .when(pl.col("outlier_group_size") < MIN_GROUP_SIZE)
        .then(pl.lit(OUTLIER_STATUS_INSUFFICIENT))
        .when(
            (pl.col("comparable_fare") < pl.col("outlier_lower_bound"))
            | (pl.col("comparable_fare") > pl.col("outlier_upper_bound"))
        )
        .then(pl.lit(OUTLIER_STATUS_STATISTICAL))
        .otherwise(pl.lit(OUTLIER_STATUS_NORMAL))
        .alias("outlier_status")
    )

    eligible_rows = int(result["fare_group_eligible"].sum())
    not_eligible_rows = input_rows - eligible_rows

    status_counts = result["outlier_status"].value_counts()

    def count_status(status: str) -> int:
        matches = status_counts.filter(
            pl.col("outlier_status") == status
        )["count"]
        return int(matches[0]) if len(matches) else 0

    groups_analyzed = int(
        group_stats.filter(pl.col("outlier_group_size") >= MIN_GROUP_SIZE)
        .height
    )
    insufficient_groups = int(
        group_stats.filter(pl.col("outlier_group_size") < MIN_GROUP_SIZE)
        .height
    )

    report = OutlierDetectionReport(
        input_rows=input_rows,
        output_rows=result.height,
        eligible_rows=eligible_rows,
        not_eligible_rows=not_eligible_rows,
        groups_analyzed=groups_analyzed,
        insufficient_groups=insufficient_groups,
        normal_rows=count_status(OUTLIER_STATUS_NORMAL),
        outlier_rows=count_status(OUTLIER_STATUS_STATISTICAL),
        insufficient_rows=count_status(OUTLIER_STATUS_INSUFFICIENT),
        warnings=[],
    )

    return result, report
