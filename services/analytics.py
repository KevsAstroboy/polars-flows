"""Pipelines Polars autonomes de valorisation.

Aucun import Django : ce module lit des CSV (ou reçoit des DataFrames)
et retourne des DataFrames. Il peut être exécuté seul :

    uv run python -m services.analytics
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import polars as pl

PRODUCTION_DATE_FORMATS = ("%m-%d-%y", "%m/%d/%y %H:%M")
ASSET_COLUMNS = ("SPV 1", "SPV 2")
PRICE_COLUMNS = ("bidding_zone", "timestamp", "price_eur_mwh")
PRODUCTION_COLUMNS = ("asset_code", "timestamp", "power_mw", "data_type")

DataType = Literal["OBSERVED", "ESTIMATED"]
JoinStrategy = Literal["exact", "asof"]


def _clean_columns(df: pl.DataFrame) -> pl.DataFrame:
    """Supprime BOM et espaces autour des noms de colonnes."""
    return df.rename({c: c.strip().lstrip("\ufeff").strip() for c in df.columns})


def _require_columns(df: pl.DataFrame, required: tuple[str, ...], label: str) -> None:
    missing = sorted(set(required) - set(df.columns))
    if missing:
        raise ValueError(f"{label}: colonnes manquantes {missing}")


def load_prices(path: str | Path, zone: str = "FR") -> pl.DataFrame:
    """Charge et normalise les prix de marché (UTC, une ligne par heure)."""
    df = _clean_columns(pl.read_csv(path, encoding="utf8", infer_schema=False))
    _require_columns(df, ("bidding_zone", "settlement_price", "delivery_start"), "prix de marché")

    df = df.with_columns(
        pl.col("delivery_start")
        .str.to_datetime("%Y-%m-%d %H:%M:%S%.f %z", time_zone="UTC", strict=False)
        .alias("timestamp"),
        pl.col("settlement_price").cast(pl.Float64, strict=False).alias("price_eur_mwh"),
        pl.col("bidding_zone").str.strip_chars().str.to_uppercase().alias("bidding_zone"),
    ).filter(pl.col("bidding_zone") == zone.upper())

    df = df.select(*PRICE_COLUMNS)

    if df.filter(pl.col("timestamp").is_null() | pl.col("price_eur_mwh").is_null()).height:
        raise ValueError("prix: timestamp ou prix non parsable")

    duplicates = df.group_by(["bidding_zone", "timestamp"]).len().filter(pl.col("len") > 1)
    if duplicates.height:
        raise ValueError(f"prix: {duplicates.height} doublons (zone, timestamp)")

    return df.sort("timestamp")


def load_production(
    path: str | Path,
    *,
    data_type: DataType = "ESTIMATED",
    year: int | None = None,
) -> pl.DataFrame:
    """Charge la production, passe de large à long et cale les timestamps sur l'heure."""
    df = _clean_columns(pl.read_csv(path, encoding="utf8", infer_schema=False))
    _require_columns(df, ("Datetime", *ASSET_COLUMNS), "production")

    raw = pl.col("Datetime").str.strip_chars()
    parsed = pl.coalesce(
        raw.str.strptime(pl.Datetime, PRODUCTION_DATE_FORMATS[0], strict=False),
        raw.str.strptime(pl.Datetime, PRODUCTION_DATE_FORMATS[1], strict=False),
    )

    df = df.with_columns(parsed.alias("_timestamp"))
    df = df.with_columns(
        (
            pl.col("_timestamp").dt.truncate("1h")
            + pl.when(pl.col("_timestamp").dt.minute() >= 30)
            .then(pl.duration(hours=1))
            .otherwise(pl.duration(hours=0))
        )
        .dt.replace_time_zone("UTC")
        .alias("timestamp")
    )

    df = df.unpivot(
        index="timestamp",
        on=list(ASSET_COLUMNS),
        variable_name="asset_code",
        value_name="power_mw",
    )

    df = df.with_columns(
        pl.col("asset_code").str.replace_all(" ", ""),
        pl.col("power_mw").str.strip_chars().cast(pl.Float64, strict=False),
        pl.lit(data_type).alias("data_type"),
    )

    if year is not None:
        df = df.filter(pl.col("timestamp").dt.year() == year)

    df = df.select(*PRODUCTION_COLUMNS)

    if df.filter(pl.col("timestamp").is_null()).height:
        raise ValueError("production: timestamp non parsable")

    duplicates = df.group_by(["asset_code", "timestamp"]).len().filter(pl.col("len") > 1)
    if duplicates.height:
        raise ValueError(f"production: {duplicates.height} doublons (asset, timestamp)")

    return df.sort(["asset_code", "timestamp"])


def align_prices(
    production: pl.DataFrame,
    prices: pl.DataFrame,
    *,
    strategy: JoinStrategy = "exact",
) -> pl.DataFrame:
    """Aligne chaque point de production sur le prix de l'heure.

    exact : inner join sur l'heure (défaut, révèle les prix manquants).
    asof  : dernier prix connu dans les 59 minutes (utile en 15 min).
    """
    prod = production.sort("timestamp")
    pr = prices.sort("timestamp")

    if strategy == "exact":
        return prod.join(pr, on="timestamp", how="inner")

    if strategy == "asof":
        return prod.join_asof(
            pr,
            on="timestamp",
            strategy="backward",
            tolerance="59m",
        )

    raise ValueError("strategy doit être 'exact' ou 'asof'")


def value_production(
    production: pl.DataFrame,
    prices: pl.DataFrame,
    *,
    strategy: JoinStrategy = "exact",
    duration_h: float = 1.0,
) -> pl.DataFrame:
    """Calcule l'énergie et le revenu par heure."""
    aligned = align_prices(production, prices, strategy=strategy)

    return aligned.with_columns(
        (pl.col("power_mw") * duration_h).alias("energy_mwh"),
        (pl.col("power_mw") * duration_h * pl.col("price_eur_mwh")).alias("revenue_eur"),
        (
            (pl.col("price_eur_mwh") < 0)
            & (pl.col("power_mw") > 0).fill_null(False)
        ).alias("negative_price_with_production"),
    )


def monthly_kpis(
    production: pl.DataFrame,
    prices: pl.DataFrame,
    *,
    data_type: DataType | None = "ESTIMATED",
    strategy: JoinStrategy = "exact",
    duration_h: float = 1.0,
) -> pl.DataFrame:
    """Agrège par actif et par mois (UTC)."""
    prod = production
    if data_type is not None:
        prod = prod.filter(pl.col("data_type") == data_type)

    valued = value_production(prod, prices, strategy=strategy, duration_h=duration_h)

    return (
        valued.with_columns(
            pl.col("timestamp").dt.truncate("1mo").cast(pl.Date).alias("month")
        )
        .group_by(["asset_code", "month"])
        .agg(
            pl.col("energy_mwh").sum().round(3).alias("production_mwh"),
            pl.col("revenue_eur").sum().round(2).alias("revenue_eur"),
            pl.col("negative_price_with_production")
            .sum()
            .alias("negative_price_hours"),
        )
        .sort(["asset_code", "month"])
    )


def compute_monthly_kpis(
    production_path: str | Path,
    prices_path: str | Path,
    *,
    zone: str = "FR",
    data_type: DataType = "ESTIMATED",
    year: int | None = 2026,
    strategy: JoinStrategy = "exact",
    duration_h: float = 1.0,
) -> pl.DataFrame:
    """Point d'entrée : CSV -> DataFrame de KPIs."""
    production = load_production(production_path, data_type=data_type, year=year)
    prices = load_prices(prices_path, zone=zone)
    return monthly_kpis(
        production,
        prices,
        data_type=data_type,
        strategy=strategy,
        duration_h=duration_h,
    )


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    production = load_production(root / "source" / "eolien_productions.csv")
    prices = load_prices(root / "source" / "market_price.csv")

    valued = value_production(production, prices)
    kpi = monthly_kpis(production, prices)

    with pl.Config(tbl_rows=50, fmt_str_lengths=30):
        print("Valorisation horaire (12 premières lignes)")
        print(
            valued.select(
                "asset_code",
                "timestamp",
                "power_mw",
                "price_eur_mwh",
                "energy_mwh",
                "revenue_eur",
                "negative_price_with_production",
            ).head(12)
        )
        print(f"\n{valued.height} lignes valorisées")

        print("\nKPIs mensuels")
        print(kpi)

        print("\nTotaux par actif")
        print(
            kpi.group_by("asset_code")
            .agg(
                pl.col("production_mwh").sum().round(3),
                pl.col("revenue_eur").sum().round(2),
                pl.col("negative_price_hours").sum(),
            )
            .sort("asset_code")
        )


if __name__ == "__main__":
    main()
