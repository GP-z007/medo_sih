"""
Advance-window assignment for the SIH 2026 airfare index pipeline.

Purpose
-------
Classify airfare observations into the exact advance-booking windows
used by the SIH 2026 methodology:

    T+1
    T+7
    T+15
    T+30
    T+45

The original booking_lead_days value is preserved.

This module does not:
    - recalculate booking_lead_days
    - modify booking_date or departure_date
    - delete non-target observations
    - perform outlier detection
    - calculate representative fares
    - calculate price relatives
    - calculate route or national weights
    - perform API/FastAPI operations
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import polars as pl


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TARGET_ADVANCE_WINDOWS = (1, 7, 15, 30, 45)

_WINDOW_LABELS = {
    1: "T+1",
    7: "T+7",
    15: "T+15",
    30: "T+30",
    45: "T+45",
}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass
class AdvanceWindowReport:
    """Summary statistics for advance-window classification."""

    input_rows: int = 0
    output_rows: int = 0

    matched_t1: int = 0
    matched_t7: int = 0
    matched_t15: int = 0
    matched_t30: int = 0
    matched_t45: int = 0

    outside_target_window: int = 0
    invalid_lead_days: int = 0

    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """Return the report as a dictionary."""

        return {
            "input_rows": self.input_rows,
            "output_rows": self.output_rows,
            "matched_t1": self.matched_t1,
            "matched_t7": self.matched_t7,
            "matched_t15": self.matched_t15,
            "matched_t30": self.matched_t30,
            "matched_t45": self.matched_t45,
            "outside_target_window": self.outside_target_window,
            "invalid_lead_days": self.invalid_lead_days,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# Main processing function
# ---------------------------------------------------------------------------

def assign_advance_windows(
    df: pl.DataFrame,
) -> tuple[pl.DataFrame, AdvanceWindowReport]:
    """
    Assign exact SIH advance-booking windows to airfare observations.

    Parameters
    ----------
    df:
        Polars DataFrame containing booking_lead_days.

    Returns
    -------
    tuple[pl.DataFrame, AdvanceWindowReport]
        The input observations with two additional columns:

        advance_window
            One of T+1, T+7, T+15, T+30, T+45, or null.

        advance_window_status
            MATCHED
            OUTSIDE_TARGET_WINDOW
            INVALID

    Rules
    -----
    Exact matching is used.

    Examples:
        1  -> T+1
        7  -> T+7
        15 -> T+15
        30 -> T+30
        45 -> T+45

        14 -> OUTSIDE_TARGET_WINDOW
        16 -> OUTSIDE_TARGET_WINDOW
        0  -> OUTSIDE_TARGET_WINDOW
        -1 -> INVALID
        null -> INVALID

    The original booking_lead_days column is never overwritten.
    """

    report = AdvanceWindowReport(input_rows=df.height)

    # ------------------------------------------------------------------
    # Empty input
    # ------------------------------------------------------------------

    if df.is_empty():
        processed = df.with_columns(
            pl.Series(
                "advance_window",
                [],
                dtype=pl.String,
            ),
            pl.Series(
                "advance_window_status",
                [],
                dtype=pl.String,
            ),
        )

        report.output_rows = 0
        return processed, report

    # ------------------------------------------------------------------
    # Required column
    # ------------------------------------------------------------------

    if "booking_lead_days" not in df.columns:
        report.warnings.append(
            "booking_lead_days column is missing; "
            "advance-window classification cannot be performed."
        )

        processed = df.with_columns(
            pl.lit(None, dtype=pl.String).alias(
                "advance_window"
            ),
            pl.lit("INVALID", dtype=pl.String).alias(
                "advance_window_status"
            ),
        )

        report.invalid_lead_days = processed.height
        report.output_rows = processed.height

        return processed, report

    # ------------------------------------------------------------------
    # Temporary numeric representation
    #
    # Validation is responsible for checking the actual data contract.
    # Here we only create a temporary integer representation so that
    # exact target matching can be performed.
    # ------------------------------------------------------------------

    processed = df.with_columns(
        pl.col("booking_lead_days")
        .cast(pl.Int64, strict=False)
        .alias("_lead_days_for_window")
    )

    # ------------------------------------------------------------------
    # Exact target-window assignment
    # ------------------------------------------------------------------

    processed = processed.with_columns(
        pl.col("_lead_days_for_window")
        .replace_strict(
            _WINDOW_LABELS,
            return_dtype=pl.String,
            default=None,
        )
        .alias("advance_window")
    )

    # ------------------------------------------------------------------
    # Status assignment
    # ------------------------------------------------------------------

    processed = processed.with_columns(
        pl.when(
            pl.col("_lead_days_for_window").is_null()
            | (pl.col("_lead_days_for_window") < 0)
        )
        .then(pl.lit("INVALID"))
        .when(
            pl.col("_lead_days_for_window").is_in(
                TARGET_ADVANCE_WINDOWS
            )
        )
        .then(pl.lit("MATCHED"))
        .otherwise(
            pl.lit("OUTSIDE_TARGET_WINDOW")
        )
        .alias("advance_window_status")
    )

    # ------------------------------------------------------------------
    # Report counts
    # ------------------------------------------------------------------

    report.matched_t1 = processed.filter(
        pl.col("advance_window") == "T+1"
    ).height

    report.matched_t7 = processed.filter(
        pl.col("advance_window") == "T+7"
    ).height

    report.matched_t15 = processed.filter(
        pl.col("advance_window") == "T+15"
    ).height

    report.matched_t30 = processed.filter(
        pl.col("advance_window") == "T+30"
    ).height

    report.matched_t45 = processed.filter(
        pl.col("advance_window") == "T+45"
    ).height

    report.outside_target_window = processed.filter(
        pl.col("advance_window_status")
        == "OUTSIDE_TARGET_WINDOW"
    ).height

    report.invalid_lead_days = processed.filter(
        pl.col("advance_window_status")
        == "INVALID"
    ).height

    # ------------------------------------------------------------------
    # Remove temporary column
    # ------------------------------------------------------------------

    processed = processed.drop("_lead_days_for_window")

    report.output_rows = processed.height

    return processed, report