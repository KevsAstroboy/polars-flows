from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from services.analytics import compute_monthly_kpis, value_production
from services.ingestion_pipeline import normalize_production_frame
from services.kpis import aggregate_kpis

UTC_DATETIME = pl.Datetime("us", "UTC")


def _valued_frame(start: datetime, hours: int) -> pl.DataFrame:
    rows = [
        {
            "asset_code": "SPV1",
            "timestamp": start + timedelta(hours=offset),
            "power_mw": 1.0,
            "price_eur_mwh": 10.0,
            "energy_mwh": 1.0,
            "revenue_eur": 10.0,
            "negative_price_with_production": False,
        }
        for offset in range(hours)
    ]
    return pl.DataFrame(
        rows,
        schema={
            "asset_code": pl.String,
            "timestamp": UTC_DATETIME,
            "power_mw": pl.Float64,
            "price_eur_mwh": pl.Float64,
            "energy_mwh": pl.Float64,
            "revenue_eur": pl.Float64,
            "negative_price_with_production": pl.Boolean,
        },
    )


def test_monthly_kpis_golden():
    root = Path(__file__).resolve().parents[1]
    kpi = compute_monthly_kpis(
        root / "source" / "eolien_productions.csv",
        root / "source" / "market_price.csv",
    )

    assert kpi.shape == (18, 5)
    assert round(float(kpi["production_mwh"].sum()), 3) == 20828.808
    assert round(float(kpi["revenue_eur"].sum()), 2) == 1639307.75
    assert int(kpi["negative_price_hours"].sum()) == 938


def test_normalize_infers_data_type_by_year():
    raw = pl.DataFrame(
        {
            "Datetime": ["01-01-20", "1/1/20 0:59", "01-01-26", "1/1/26 0:59"],
            "SPV 1": ["1.0", "2.0", "3.0", "4.0"],
            "SPV 2": ["0.5", "0.5", "0.5", "0.5"],
        }
    )

    frame = normalize_production_frame(raw)

    assert frame.height == 8
    pairs = set(
        zip(
            frame["timestamp"].dt.year().to_list(),
            frame["data_type"].to_list(),
        )
    )
    assert pairs == {(2020, "OBSERVED"), (2026, "ESTIMATED")}


def test_normalize_year_filter_keeps_source_year_semantics():
    raw = pl.DataFrame(
        {
            "Datetime": ["01-01-20", "01-01-26"],
            "SPV 1": ["1.0", "2.0"],
        }
    )

    frame = normalize_production_frame(raw, year=2020)

    assert frame["timestamp"].dt.year().unique().to_list() == [2020]
    assert frame["data_type"].unique().to_list() == ["OBSERVED"]


def test_value_production_converts_mw_to_mwh():
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    production = pl.DataFrame(
        {
            "asset_code": ["SPV1"],
            "timestamp": [timestamp],
            "power_mw": [2.0],
            "data_type": ["OBSERVED"],
        },
        schema={
            "asset_code": pl.String,
            "timestamp": UTC_DATETIME,
            "power_mw": pl.Float64,
            "data_type": pl.String,
        },
    )
    prices = pl.DataFrame(
        {
            "bidding_zone": ["FR"],
            "timestamp": [timestamp],
            "price_eur_mwh": [50.0],
        },
        schema={
            "bidding_zone": pl.String,
            "timestamp": UTC_DATETIME,
            "price_eur_mwh": pl.Float64,
        },
    )

    valued = value_production(production, prices)

    assert float(valued["energy_mwh"][0]) == 2.0
    assert float(valued["revenue_eur"][0]) == 100.0


def test_null_power_is_not_zero():
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    production = pl.DataFrame(
        {
            "asset_code": ["SPV1"],
            "timestamp": [timestamp],
            "power_mw": [None],
            "data_type": ["OBSERVED"],
        },
        schema={
            "asset_code": pl.String,
            "timestamp": UTC_DATETIME,
            "power_mw": pl.Float64,
            "data_type": pl.String,
        },
    )
    prices = pl.DataFrame(
        {
            "bidding_zone": ["FR"],
            "timestamp": [timestamp],
            "price_eur_mwh": [10.0],
        },
        schema={
            "bidding_zone": pl.String,
            "timestamp": UTC_DATETIME,
            "price_eur_mwh": pl.Float64,
        },
    )

    valued = value_production(production, prices)

    assert valued["energy_mwh"][0] is None
    assert valued["revenue_eur"][0] is None
    assert valued["negative_price_with_production"][0] is False


def test_negative_price_hours_are_distinct_per_hour():
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    rows = [
        {
            "asset_code": "SPV1",
            "timestamp": start + timedelta(minutes=15 * index),
            "power_mw": 1.0,
            "price_eur_mwh": -10.0,
            "energy_mwh": 0.25,
            "revenue_eur": -2.5,
            "negative_price_with_production": True,
        }
        for index in range(4)
    ]
    valued = pl.DataFrame(
        rows,
        schema={
            "asset_code": pl.String,
            "timestamp": UTC_DATETIME,
            "power_mw": pl.Float64,
            "price_eur_mwh": pl.Float64,
            "energy_mwh": pl.Float64,
            "revenue_eur": pl.Float64,
            "negative_price_with_production": pl.Boolean,
        },
    )

    kpi = aggregate_kpis(valued, granularity="daily", time_zone="UTC")

    assert kpi.height == 1
    assert int(kpi["hours"][0]) == 1
    assert int(kpi["negative_price_hours"][0]) == 1
    assert round(float(kpi["production_mwh"][0]), 3) == 1.0


def test_dst_daily_buckets_have_23_and_25_hours():
    # 29 mars 2026 : la journée civile Paris couvre 23 heures UTC.
    spring_start = datetime(2026, 3, 28, 23, tzinfo=UTC)
    spring = aggregate_kpis(
        _valued_frame(spring_start, 23),
        granularity="daily",
        time_zone="Europe/Paris",
    )
    assert str(spring["period"][0]) == "2026-03-29"
    assert int(spring["hours"][0]) == 23

    # 25 octobre 2026 : la journée civile Paris couvre 25 heures UTC.
    autumn_start = datetime(2026, 10, 24, 22, tzinfo=UTC)
    autumn = aggregate_kpis(
        _valued_frame(autumn_start, 25),
        granularity="daily",
        time_zone="Europe/Paris",
    )
    assert str(autumn["period"][0]) == "2026-10-25"
    assert int(autumn["hours"][0]) == 25
