from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from energy.models import MarketPrice, ProductionReading


@pytest.mark.django_db
def test_post_production(api_client, assets):
    csv = b"Datetime, SPV 1 ,SPV 2\n01-01-26,1.0,2.0\n"
    upload = SimpleUploadedFile("production.csv", csv, content_type="text/csv")

    response = api_client.post(
        "/api/ingest/production/",
        {"file": upload, "data_type": "ESTIMATED", "year": 2026},
        format="multipart",
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "SUCCESS"
    assert body["rows_received"] == 1
    assert body["rows_ingested"] == 2
    assert body["data_type_by_year"] == {"2026": "ESTIMATED"}
    assert ProductionReading.objects.count() == 2


@pytest.mark.django_db
def test_post_without_year_infers_data_type(api_client, assets):
    csv = (
        b"Datetime, SPV 1 ,SPV 2\n"
        b"01-01-20,1.0,2.0\n"
        b"01-01-26,3.0,4.0\n"
    )
    upload = SimpleUploadedFile("production.csv", csv, content_type="text/csv")

    response = api_client.post(
        "/api/ingest/production/",
        {"file": upload},
        format="multipart",
    )

    assert response.status_code == 201
    assert response.json()["data_type_by_year"] == {
        "2020": "OBSERVED",
        "2026": "ESTIMATED",
    }
    assert ProductionReading.objects.count() == 4


@pytest.mark.django_db
def test_get_performance_daily(api_client, assets):
    asset = assets[0]
    first_hour = datetime(2026, 1, 1, 0, tzinfo=UTC)
    second_hour = first_hour + timedelta(hours=1)

    MarketPrice.objects.create(
        bidding_zone="FR",
        timestamp=first_hour,
        price_eur_mwh=Decimal("-10.000"),
    )
    MarketPrice.objects.create(
        bidding_zone="FR",
        timestamp=second_hour,
        price_eur_mwh=Decimal("20.000"),
    )
    ProductionReading.objects.create(
        asset=asset,
        timestamp=first_hour,
        power_mw=Decimal("1.000"),
        data_type="ESTIMATED",
    )
    ProductionReading.objects.create(
        asset=asset,
        timestamp=second_hour,
        power_mw=Decimal("2.000"),
        data_type="ESTIMATED",
    )

    response = api_client.get(
        f"/api/assets/{asset.id}/performance/",
        {
            "start_date": "2026-01-01",
            "end_date": "2026-01-01",
            "granularity": "daily",
            "timezone": "UTC",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["asset"]["code"] == "SPV1"
    assert body["totals"] == {
        "production_mwh": 3.0,
        "revenue_eur": 30.0,
        "negative_price_hours": 1,
    }
    assert body["kpis"][0]["period"] == "2026-01-01"
    assert body["kpis"][0]["hours"] == 2
    assert body["kpis"][0]["negative_price_hours"] == 1


@pytest.mark.django_db
def test_price_zone_is_configurable(api_client, assets):
    asset = assets[0]
    timestamp = datetime(2026, 1, 1, 0, tzinfo=UTC)

    ProductionReading.objects.create(
        asset=asset,
        timestamp=timestamp,
        power_mw=Decimal("1.000"),
        data_type="ESTIMATED",
    )
    MarketPrice.objects.create(
        bidding_zone="FR",
        timestamp=timestamp,
        price_eur_mwh=Decimal("10.000"),
    )
    MarketPrice.objects.create(
        bidding_zone="DE",
        timestamp=timestamp,
        price_eur_mwh=Decimal("99.000"),
    )

    response = api_client.get(
        f"/api/assets/{asset.id}/performance/",
        {
            "start_date": "2026-01-01",
            "end_date": "2026-01-01",
            "granularity": "daily",
            "timezone": "UTC",
            "price_zone": "de",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["query"]["price_zone"] == "DE"
    assert body["totals"]["revenue_eur"] == 99.0


@pytest.mark.django_db
def test_get_performance_without_prices_returns_warning(api_client, assets):
    asset = assets[0]
    ProductionReading.objects.create(
        asset=asset,
        timestamp=datetime(2026, 1, 1, 0, tzinfo=UTC),
        power_mw=Decimal("1.000"),
        data_type="ESTIMATED",
    )

    response = api_client.get(
        f"/api/assets/{asset.id}/performance/",
        {"start_date": "2026-01-01", "end_date": "2026-01-01"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["kpis"] == []
    assert body["totals"]["production_mwh"] == 0.0
    assert body["warnings"]


@pytest.mark.django_db
def test_get_performance_dst_day(api_client, assets):
    asset = assets[0]
    start = datetime(2026, 3, 28, 23, tzinfo=UTC)

    ProductionReading.objects.bulk_create(
        [
            ProductionReading(
                asset=asset,
                timestamp=start + timedelta(hours=offset),
                power_mw=Decimal("1.000"),
                data_type="ESTIMATED",
            )
            for offset in range(23)
        ]
    )
    MarketPrice.objects.bulk_create(
        [
            MarketPrice(
                bidding_zone="FR",
                timestamp=start + timedelta(hours=offset),
                price_eur_mwh=Decimal("10.000"),
            )
            for offset in range(23)
        ]
    )

    response = api_client.get(
        f"/api/assets/{asset.id}/performance/",
        {
            "start_date": "2026-03-29",
            "end_date": "2026-03-29",
            "granularity": "daily",
            "timezone": "Europe/Paris",
        },
    )

    assert response.status_code == 200
    kpis = response.json()["kpis"]
    assert len(kpis) == 1
    assert kpis[0]["period"] == "2026-03-29"
    assert kpis[0]["hours"] == 23
    assert kpis[0]["production_mwh"] == 23.0
    assert kpis[0]["revenue_eur"] == 230.0


@pytest.mark.django_db
def test_asset_not_found(api_client):
    response = api_client.get("/api/assets/999/performance/")
    assert response.status_code == 404


@pytest.mark.django_db
def test_invalid_granularity(api_client, assets):
    response = api_client.get(
        f"/api/assets/{assets[0].id}/performance/",
        {"granularity": "hourly"},
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_invalid_timezone(api_client, assets):
    response = api_client.get(
        f"/api/assets/{assets[0].id}/performance/",
        {"timezone": "Mars/Olympus"},
    )
    assert response.status_code == 400
