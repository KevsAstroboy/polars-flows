"""Agrégation des KPIs valorisés.

Ce module est additif : il ne modifie pas ``monthly_kpis`` dans
``services.analytics``. Il ajoute le découpage daily/monthly, le fuseau
horaire et le comptage des heures négatives **distinctes**.

Point DST : le bucket (jour/mois) est calculé en heure locale, mais les
heures distinctes sont comptées en UTC. Ainsi le 29 mars 2026 (Paris) vaut
23 heures et le 25 octobre 2026 en vaut 25, sans perdre l'heure répétée.
"""

from __future__ import annotations

from typing import Literal
from zoneinfo import ZoneInfo

import polars as pl

Granularity = Literal["daily", "monthly"]

GRANULARITIES = ("daily", "monthly")

KPI_SCHEMA = {
    "asset_code": pl.String,
    "period": pl.Date,
    "production_mwh": pl.Float64,
    "revenue_eur": pl.Float64,
    "hours": pl.UInt32,
    "negative_price_hours": pl.UInt32,
}


def aggregate_kpis(
    valued: pl.DataFrame,
    *,
    granularity: Granularity = "monthly",
    time_zone: str = "Europe/Paris",
) -> pl.DataFrame:
    """Agrège un DataFrame valorisé par actif et par période.

    ``valued`` est la sortie de ``value_production`` : elle doit contenir
    ``asset_code``, ``timestamp``, ``energy_mwh``, ``revenue_eur`` et
    ``negative_price_with_production``.
    """
    if granularity not in GRANULARITIES:
        raise ValueError(f"granularity invalide: {granularity!r}")

    try:
        ZoneInfo(time_zone)
    except Exception as exc:
        raise ValueError(f"timezone invalide: {time_zone!r}") from exc

    if valued.is_empty():
        return pl.DataFrame(schema=KPI_SCHEMA)

    trunc = "1d" if granularity == "daily" else "1mo"

    local = valued.with_columns(
        pl.col("timestamp").dt.convert_time_zone(time_zone).alias("_local_ts"),
        pl.col("timestamp").dt.truncate("1h").alias("_utc_hour"),
    ).with_columns(
        pl.col("_local_ts").dt.truncate(trunc).dt.date().alias("period"),
    )

    negative_hours = (
        local.filter(pl.col("negative_price_with_production"))
        .group_by(["asset_code", "period"])
        .agg(pl.col("_utc_hour").n_unique().alias("negative_price_hours"))
    )

    return (
        local.group_by(["asset_code", "period"])
        .agg(
            pl.col("energy_mwh").sum().round(3).alias("production_mwh"),
            pl.col("revenue_eur").sum().round(2).alias("revenue_eur"),
            pl.col("_utc_hour").n_unique().alias("hours"),
        )
        .join(negative_hours, on=["asset_code", "period"], how="left")
        .with_columns(
            pl.col("negative_price_hours").fill_null(0).cast(pl.UInt32),
            pl.col("hours").cast(pl.UInt32),
        )
        .select(*KPI_SCHEMA)
        .sort(["asset_code", "period"])
    )
