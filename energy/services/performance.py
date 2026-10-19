"""Lecture DB -> Polars -> KPIs valorisés pour l'API performance."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import polars as pl
from django.db.models import Max, Min

from energy.models import Asset, MarketPrice, ProductionReading
from services.analytics import value_production
from services.kpis import aggregate_kpis


def _production_frame(
    asset: Asset,
    rows: list[tuple[datetime, object]],
    data_type: str,
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "asset_code": [asset.code] * len(rows),
            "timestamp": [row[0] for row in rows],
            "power_mw": [
                float(row[1]) if row[1] is not None else None for row in rows
            ],
            "data_type": [data_type] * len(rows),
        },
        schema={
            "asset_code": pl.String,
            "timestamp": pl.Datetime("us", "UTC"),
            "power_mw": pl.Float64,
            "data_type": pl.String,
        },
    )


def _price_frame(rows: list[tuple[str, datetime, object]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "bidding_zone": [row[0] for row in rows],
            "timestamp": [row[1] for row in rows],
            "price_eur_mwh": [float(row[2]) for row in rows],
        },
        schema={
            "bidding_zone": pl.String,
            "timestamp": pl.Datetime("us", "UTC"),
            "price_eur_mwh": pl.Float64,
        },
    )


def _asset_payload(asset: Asset) -> dict:
    return {
        "id": asset.id,
        "code": asset.code,
        "name": asset.name,
        "type": asset.type,
        "nominal_power_mw": float(asset.nominal_power_mw),
    }


def _query_payload(
    start_date: date | None,
    end_date: date | None,
    granularity: str,
    data_type: str,
    timezone: str,
    price_zone: str,
) -> dict:
    return {
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
        "granularity": granularity,
        "data_type": data_type,
        "timezone": timezone,
        "price_zone": price_zone,
    }


def _empty_payload(
    asset: Asset,
    *,
    start_date: date | None,
    end_date: date | None,
    granularity: str,
    data_type: str,
    timezone: str,
    price_zone: str,
    warnings: list[str],
) -> dict:
    return {
        "asset": _asset_payload(asset),
        "query": _query_payload(
            start_date, end_date, granularity, data_type, timezone, price_zone
        ),
        "kpis": [],
        "totals": {
            "production_mwh": 0.0,
            "revenue_eur": 0.0,
            "negative_price_hours": 0,
        },
        "warnings": warnings,
    }


def asset_performance(
    asset: Asset,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    granularity: str = "monthly",
    data_type: str = "ESTIMATED",
    timezone: str = "Europe/Paris",
    price_zone: str = "FR",
) -> dict:
    """Construit la réponse JSON de performance pour un actif."""
    tz = ZoneInfo(timezone)
    readings = ProductionReading.objects.filter(asset=asset, data_type=data_type)

    if start_date is None or end_date is None:
        bounds = readings.aggregate(min_ts=Min("timestamp"), max_ts=Max("timestamp"))
        min_ts = bounds["min_ts"]
        max_ts = bounds["max_ts"]
        if min_ts is None:
            return _empty_payload(
                asset,
                start_date=start_date,
                end_date=end_date,
                granularity=granularity,
                data_type=data_type,
                timezone=timezone,
                price_zone=price_zone,
                warnings=["Aucune donnée de production pour cet actif."],
            )
        if start_date is None:
            start_date = min_ts.astimezone(tz).date()
        if end_date is None:
            end_date = max_ts.astimezone(tz).date()

    # Dates civiles dans le fuseau demandé -> bornes UTC.
    start_utc = datetime.combine(start_date, time.min, tzinfo=tz).astimezone(UTC)
    end_utc = datetime.combine(
        end_date + timedelta(days=1), time.min, tzinfo=tz
    ).astimezone(UTC)

    production_rows = list(
        readings.filter(
            timestamp__gte=start_utc,
            timestamp__lt=end_utc,
        )
        .order_by("timestamp")
        .values_list("timestamp", "power_mw")
    )

    if not production_rows:
        return _empty_payload(
            asset,
            start_date=start_date,
            end_date=end_date,
            granularity=granularity,
            data_type=data_type,
            timezone=timezone,
            price_zone=price_zone,
            warnings=["Aucune donnée de production sur cette période."],
        )

    price_rows = list(
        MarketPrice.objects.filter(
            bidding_zone=price_zone,
            timestamp__gte=start_utc,
            timestamp__lt=end_utc,
        )
        .order_by("timestamp")
        .values_list("bidding_zone", "timestamp", "price_eur_mwh")
    )

    if not price_rows:
        return _empty_payload(
            asset,
            start_date=start_date,
            end_date=end_date,
            granularity=granularity,
            data_type=data_type,
            timezone=timezone,
            price_zone=price_zone,
            warnings=[
                f"Aucun prix de marché pour la zone {price_zone} sur cette période."
            ],
        )

    production = _production_frame(asset, production_rows, data_type)
    prices = _price_frame(price_rows)
    valued = value_production(production, prices, strategy="exact")

    warnings: list[str] = []
    missing = production.height - valued.height
    if missing:
        warnings.append(
            f"{missing} point(s) de production sans prix ont été exclus."
        )

    if valued.is_empty():
        return _empty_payload(
            asset,
            start_date=start_date,
            end_date=end_date,
            granularity=granularity,
            data_type=data_type,
            timezone=timezone,
            price_zone=price_zone,
            warnings=warnings,
        )

    kpis = aggregate_kpis(valued, granularity=granularity, time_zone=timezone)

    return {
        "asset": _asset_payload(asset),
        "query": _query_payload(
            start_date, end_date, granularity, data_type, timezone, price_zone
        ),
        "kpis": kpis.to_dicts(),
        "totals": {
            "production_mwh": round(float(kpis["production_mwh"].sum()), 3),
            "revenue_eur": round(float(kpis["revenue_eur"].sum()), 2),
            "negative_price_hours": int(kpis["negative_price_hours"].sum()),
        },
        "warnings": warnings,
    }
