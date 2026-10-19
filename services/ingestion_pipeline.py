"""Normalisation des CSV de production uploadés.

Ce module est additif : il ne modifie aucune fonction de ``services.analytics``.
Il réutilise ses constantes et ses helpers pour produire exactement le même
schéma que ``load_production`` :

    asset_code | timestamp | power_mw | data_type
"""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from services.analytics import (
    PRODUCTION_DATE_FORMATS,
    _clean_columns,
    _require_columns,
)

VALID_DATA_TYPES = ("OBSERVED", "ESTIMATED")


def read_production_csv(source: str | Path | bytes) -> pl.DataFrame:
    """Lit un CSV brut sans inférence de type (tout en texte)."""
    return pl.read_csv(source, encoding="utf8", infer_schema=False)


def normalize_production_frame(
    df: pl.DataFrame,
    *,
    data_type: str | None = None,
    year: int | None = None,
    source_timezone: str = "UTC",
    asset_columns: tuple[str, ...] | None = None,
) -> pl.DataFrame:
    """Normalise un CSV large de production vers le schéma long attendu par l'API.

    - deux formats de date : ``MM-DD-YY`` et ``M/D/YY H:MM`` ;
    - les minutes ``:59`` sont ramenées à l'heure suivante ;
    - les colonnes d'actifs deviennent des lignes ;
    - ``-`` devient ``NULL`` ;
    - si ``data_type`` est absent, la dernière année du fichier est ``ESTIMATED``
      et les années précédentes sont ``OBSERVED``.
    """
    if data_type is not None and data_type not in VALID_DATA_TYPES:
        raise ValueError(f"data_type invalide: {data_type!r}")

    try:
        ZoneInfo(source_timezone)
    except Exception as exc:
        raise ValueError(f"timezone invalide: {source_timezone!r}") from exc

    df = _clean_columns(df)
    _require_columns(df, ("Datetime",), "production")

    if asset_columns is None:
        asset_columns = tuple(column for column in df.columns if column != "Datetime")
    if not asset_columns:
        raise ValueError("production: aucune colonne d'actif détectée")
    _require_columns(df, asset_columns, "production")

    raw = pl.col("Datetime").str.strip_chars()
    parsed = pl.coalesce(
        raw.str.strptime(pl.Datetime, PRODUCTION_DATE_FORMATS[0], strict=False),
        raw.str.strptime(pl.Datetime, PRODUCTION_DATE_FORMATS[1], strict=False),
    )

    df = df.with_columns(parsed.alias("_timestamp"))
    invalid_timestamps = df.filter(pl.col("_timestamp").is_null()).height
    if invalid_timestamps:
        raise ValueError(f"production: {invalid_timestamps} timestamp(s) non parsable(s)")

    df = df.with_columns(pl.col("_timestamp").dt.year().alias("_source_year"))
    source_years = sorted(df["_source_year"].unique().to_list())
    latest_year = max(source_years)

    if year is not None:
        df = df.filter(pl.col("_source_year") == year)
        if df.is_empty():
            raise ValueError(
                f"production: aucune ligne pour l'année {year} "
                f"(années disponibles: {source_years})"
            )

    # Calage sur l'heure : 00:59 -> 01:00, 03:59 -> 04:00, etc.
    df = df.with_columns(
        (
            pl.col("_timestamp").dt.truncate("1h")
            + pl.when(pl.col("_timestamp").dt.minute() >= 30)
            .then(pl.duration(hours=1))
            .otherwise(pl.duration(hours=0))
        ).alias("_local_ts")
    )

    if source_timezone == "UTC":
        converted = pl.col("_local_ts").dt.replace_time_zone("UTC")
    else:
        converted = (
            pl.col("_local_ts")
            .dt.replace_time_zone(
                source_timezone,
                ambiguous="earliest",
                non_existent="null",
            )
            .dt.convert_time_zone("UTC")
        )

    df = df.with_columns(converted.alias("timestamp"))
    invalid_local = df.filter(pl.col("timestamp").is_null()).height
    if invalid_local:
        raise ValueError(
            f"production: {invalid_local} timestamp(s) inexistant(s) "
            f"dans le fuseau {source_timezone} (DST)"
        )

    df = df.unpivot(
        index=["timestamp", "_source_year"],
        on=list(asset_columns),
        variable_name="asset_code",
        value_name="power_mw",
    )

    df = df.with_columns(
        pl.col("asset_code").str.replace_all(" ", ""),
        pl.col("power_mw").str.strip_chars().cast(pl.Float64, strict=False),
    )

    if data_type is not None:
        df = df.with_columns(pl.lit(data_type).alias("data_type"))
    else:
        df = df.with_columns(
            pl.when(pl.col("_source_year") == latest_year)
            .then(pl.lit("ESTIMATED"))
            .otherwise(pl.lit("OBSERVED"))
            .alias("data_type")
        )

    df = df.select(
        "asset_code",
        "timestamp",
        "power_mw",
        "data_type",
    )

    # Le fichier source ne contient pas de doublons après calage, mais on
    # protège l'ingestion : on garde la dernière valeur pour une même clé.
    df = df.unique(
        subset=["asset_code", "timestamp", "data_type"],
        keep="last",
        maintain_order=True,
    ).sort(["asset_code", "timestamp"])

    return df
