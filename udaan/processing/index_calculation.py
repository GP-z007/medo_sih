"""Daily weighted arithmetic price-relative airfare index.

The module deliberately reports coverage rather than silently treating absent
route/window observations as if their weights had been reassigned.  An index
is published only when configured weight coverage reaches the explicit
threshold.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl


TARGET_WINDOWS = ("T+1", "T+7", "T+15", "T+30", "T+45")
REQUIRED_COLUMNS = (
    "observation_date", "origin_iata", "destination_iata", "advance_window",
    "price_relative", "price_relative_status",
)


@dataclass(frozen=True)
class IndexWeights:
    """Replaceable, provisional weight configuration.

    These weights are inputs to the calculation; this project makes no claim
    that they are official DGCA/PSD weights.
    """

    route_weights: dict[str, float] | None = None
    advance_window_weights: dict[str, float] | None = None
    label: str = "PROVISIONAL_TEST_WEIGHTS"


@dataclass
class IndexCalculationReport:
    input_rows: int
    output_rows: int
    valid_price_relative_rows: int
    window_observations: int
    route_observations: int
    national_observations: int
    routes_used: int
    windows_used: int
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return self.__dict__.copy()


def _prepared_windows(weights: dict[str, float] | None) -> dict[str, float]:
    if weights is None:
        return {window: 1.0 / len(TARGET_WINDOWS) for window in TARGET_WINDOWS}
    prepared = {window: 0.0 for window in TARGET_WINDOWS}
    for window, value in weights.items():
        if window not in TARGET_WINDOWS or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"Invalid advance-window weight: {window}={value!r}")
        prepared[window] = float(value)
    if sum(prepared.values()) <= 0:
        raise ValueError("Advance-window weights must contain a positive total.")
    return prepared


def _prepared_routes(weights: dict[str, float] | None) -> dict[str, float] | None:
    if weights is None:
        return None
    prepared: dict[str, float] = {}
    for route, value in weights.items():
        if not isinstance(route, str) or "-" not in route or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"Invalid route weight: {route}={value!r}")
        prepared[route] = float(value)
    if sum(prepared.values()) <= 0:
        raise ValueError("Route weights must contain a positive total.")
    return prepared


def _empty_output() -> pl.DataFrame:
    return pl.DataFrame(schema={
        "observation_date": pl.Date,
        "national_airfare_index": pl.Float64,
        "valid_observation_count": pl.Int64,
        "expected_observation_count": pl.Int64,
        "coverage": pl.Float64,
        "weight_coverage": pl.Float64,
        "routes_used": pl.Int64,
        "expected_routes": pl.Int64,
        "missing_routes": pl.String,
        "missing_route_windows": pl.String,
        "index_status": pl.String,
        "weight_set": pl.String,
    })


def calculate_airfare_index(
    df: pl.DataFrame,
    route_weights: dict[str, float] | None = None,
    advance_window_weights: dict[str, float] | None = None,
    min_weight_coverage: float = 0.8,
    weight_set: str = "PROVISIONAL_TEST_WEIGHTS",
) -> tuple[pl.DataFrame, IndexCalculationReport]:
    """Calculate a daily weighted arithmetic price-relative index.

    Missing configured components contribute no numerator and no denominator;
    their absence is surfaced in coverage and prevents publication below
    ``min_weight_coverage``.  It is therefore not a Fisher index and does not
    claim to be one.
    """
    if not 0 < min_weight_coverage <= 1:
        raise ValueError("min_weight_coverage must be in (0, 1].")
    missing = [column for column in REQUIRED_COLUMNS if column not in df.columns]
    if missing:
        return _empty_output(), IndexCalculationReport(df.height, 0, 0, 0, 0, 0, 0, 0, [f"Missing required columns: {', '.join(missing)}"])
    windows = _prepared_windows(advance_window_weights)
    routes = _prepared_routes(route_weights)
    working = df.with_columns(
        pl.col("observation_date").cast(pl.String).str.slice(0, 10).str.to_date(strict=False).alias("observation_date"),
        (pl.col("origin_iata").cast(pl.String) + pl.lit("-") + pl.col("destination_iata").cast(pl.String)).alias("route"),
        pl.col("price_relative").cast(pl.Float64, strict=False).alias("price_relative"),
    )
    dates = working.filter(pl.col("observation_date").is_not_null()).select("observation_date").unique().sort("observation_date")
    if dates.is_empty():
        return _empty_output(), IndexCalculationReport(df.height, 0, 0, 0, 0, 0, 0, 0, ["No valid observation dates available."])
    valid = working.filter(
        (pl.col("price_relative_status") == "CALCULATED")
        & pl.col("price_relative").is_not_null() & (pl.col("price_relative") > 0)
        & pl.col("advance_window").is_in(list(TARGET_WINDOWS))
        & pl.col("observation_date").is_not_null()
    )
    window_indices = valid.group_by(
        "observation_date", "route", "advance_window"
    ).agg(
        pl.col("price_relative").mean().alias("window_index"),
        pl.len().alias("raw_observation_count"),
    )
    window_weight_df = pl.DataFrame({"advance_window": list(windows), "window_weight": list(windows.values())})
    weighted_windows = window_indices.join(window_weight_df, on="advance_window", how="left").filter(pl.col("window_weight") > 0)
    route_indices = weighted_windows.group_by("observation_date", "route").agg(
        (pl.col("window_index") * pl.col("window_weight")).sum().truediv(pl.col("window_weight").sum()).alias("route_index"),
        pl.col("window_weight").sum().alias("route_weight_coverage"),
        pl.col("raw_observation_count").sum().cast(pl.Int64).alias("raw_observation_count"),
        pl.col("advance_window").n_unique().cast(pl.Int64).alias("windows_used"),
    )
    expected_window_weight = sum(value for value in windows.values() if value > 0)
    if routes is None:
        # Default weights are provisional and the expected route universe is
        # inferred once from the supplied input, rather than separately on
        # every date. This lets coverage expose a missing route/day.
        inferred_routes = working.filter(
            pl.col("origin_iata").is_not_null() & pl.col("destination_iata").is_not_null()
        ).select("route").unique().sort("route")
        route_values = inferred_routes.get_column("route").to_list()
        equal_route_weight = 1.0 / len(route_values) if route_values else 0.0
        route_weight_df = pl.DataFrame({
            "route": route_values,
            "route_weight": [equal_route_weight] * len(route_values),
        })
    else:
        route_weight_df = pl.DataFrame({"route": list(routes), "route_weight": list(routes.values())})
    active_route_weights = route_weight_df.filter(pl.col("route_weight") > 0)
    route_indices = route_indices.join(active_route_weights, on="route", how="inner")
    components = weighted_windows.join(active_route_weights, on="route", how="inner")
    expected_routes_by_date = dates.with_columns(
        pl.lit(active_route_weights.height, dtype=pl.Int64).alias("expected_routes")
    )
    expected_total_weight = active_route_weights.get_column("route_weight").sum() * expected_window_weight
    active_windows = window_weight_df.filter(pl.col("window_weight") > 0)
    expected_components = dates.join(
        active_route_weights.select("route"), how="cross"
    ).join(active_windows.select("advance_window"), how="cross")
    missing_component_summary = (
        expected_components.join(
            components.select("observation_date", "route", "advance_window").unique(),
            on=["observation_date", "route", "advance_window"],
            how="anti",
        )
        .group_by("observation_date")
        .agg(
            pl.concat_str(["route", "advance_window"], separator=":")
            .sort()
            .str.join("|")
            .alias("missing_route_windows")
        )
    )
    missing_route_summary = (
        dates.join(active_route_weights.select("route"), how="cross")
        .join(
            components.select("observation_date", "route").unique(),
            on=["observation_date", "route"],
            how="anti",
        )
        .group_by("observation_date")
        .agg(pl.col("route").sort().str.join("|").alias("missing_routes"))
    )
    national = components.group_by("observation_date").agg(
        (pl.col("window_index") * pl.col("window_weight") * pl.col("route_weight")).sum().truediv(
            (pl.col("window_weight") * pl.col("route_weight")).sum()
        ).alias("_index"),
        (pl.col("window_weight") * pl.col("route_weight")).sum().alias("_available_weight"),
        pl.col("raw_observation_count").sum().cast(pl.Int64).alias("valid_observation_count"),
        pl.col("route").n_unique().cast(pl.Int64).alias("routes_used"),
        pl.len().cast(pl.Int64).alias("_available_windows"),
    ).with_columns(pl.lit(expected_total_weight).alias("_expected_weight"))
    expected_windows = sum(1 for value in windows.values() if value > 0)
    result = dates.join(national, on="observation_date", how="left").join(
        expected_routes_by_date, on="observation_date", how="left"
    ).join(
        missing_route_summary, on="observation_date", how="left"
    ).join(
        missing_component_summary, on="observation_date", how="left"
    ).with_columns(
        pl.col("valid_observation_count").fill_null(0).cast(pl.Int64),
        pl.col("routes_used").fill_null(0).cast(pl.Int64),
        pl.col("expected_routes").fill_null(0).cast(pl.Int64),
        pl.when(pl.col("_expected_weight").is_not_null() & (pl.col("_expected_weight") > 0)).then(
            pl.col("_available_weight").fill_null(0) / pl.col("_expected_weight")
        ).otherwise(pl.lit(0.0)).alias("_route_weight_coverage"),
    ).with_columns(
        pl.col("_route_weight_coverage").alias("weight_coverage"),
        (pl.col("expected_routes") * pl.lit(expected_windows)).cast(pl.Int64).alias("expected_observation_count"),
        pl.col("_available_windows").fill_null(0).cast(pl.Int64).alias("_available_expected_windows"),
    ).with_columns(
        pl.when(pl.col("expected_observation_count") > 0).then(
            pl.col("_available_expected_windows").cast(pl.Float64) / pl.col("expected_observation_count")
        ).otherwise(pl.lit(0.0)).alias("coverage"),
        pl.when(pl.col("routes_used") == 0).then(pl.lit("INSUFFICIENT_DATA"))
        .when(pl.col("weight_coverage") < min_weight_coverage).then(pl.lit("LOW_COVERAGE"))
        .otherwise(pl.lit("VALID")).alias("index_status"),
    ).with_columns(
        pl.when(pl.col("index_status") == "VALID").then(pl.col("_index").round(2)).otherwise(pl.lit(None, dtype=pl.Float64)).alias("national_airfare_index"),
        pl.col("missing_routes").fill_null(""),
        pl.col("missing_route_windows").fill_null(""),
        pl.lit(weight_set).alias("weight_set"),
    ).select(_empty_output().columns).sort("observation_date")
    report = IndexCalculationReport(
        input_rows=df.height,
        output_rows=result.height,
        valid_price_relative_rows=valid.height,
        window_observations=window_indices.height,
        route_observations=route_indices.height,
        national_observations=result.filter(pl.col("index_status") == "VALID").height,
        routes_used=route_indices.select("route").n_unique() if not route_indices.is_empty() else 0,
        windows_used=window_indices.select("advance_window").n_unique() if not window_indices.is_empty() else 0,
        warnings=[] if routes is not None else ["Route universe inferred from observed data; replace provisional weights before production."],
    )
    return result, report
