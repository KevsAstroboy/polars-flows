import io
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from energy.models import MarketPrice, ProductionReading, TimeSeriesIngestionLog
from energy.services.ingestion import (
    IngestionError,
    ingest_prices_file,
    ingest_production_upload,
)


@pytest.mark.django_db
def test_ingest_production_upload(assets):
    csv = (
        b"Datetime, SPV 1 ,SPV 2\n"
        b"01-01-26,1.0,2.0\n"
        b"1/1/26 0:59,3.0,-\n"
    )

    result = ingest_production_upload(
        io.BytesIO(csv),
        data_type="ESTIMATED",
        year=2026,
    )

    assert result.log.status == TimeSeriesIngestionLog.Status.SUCCESS
    assert result.log.rows_received == 2
    assert result.log.rows_ingested == 4
    assert result.log.null_values == 1
    assert result.years == [2026]
    assert result.data_type_by_year == {2026: "ESTIMATED"}
    assert ProductionReading.objects.count() == 4


@pytest.mark.django_db
def test_ingest_production_is_idempotent(assets):
    csv = b"Datetime, SPV 1 ,SPV 2\n01-01-26,1.0,2.0\n"

    first = ingest_production_upload(io.BytesIO(csv), data_type="ESTIMATED", year=2026)
    second = ingest_production_upload(io.BytesIO(csv), data_type="ESTIMATED", year=2026)

    assert first.log.status == TimeSeriesIngestionLog.Status.SUCCESS
    assert second.log.status == TimeSeriesIngestionLog.Status.SUCCESS
    assert ProductionReading.objects.count() == 2
    assert TimeSeriesIngestionLog.objects.count() == 2


@pytest.mark.django_db
def test_ingest_production_infers_data_type_by_year(assets):
    csv = (
        b"Datetime, SPV 1 ,SPV 2\n"
        b"01-01-20,1.0,2.0\n"
        b"01-01-26,3.0,4.0\n"
    )

    result = ingest_production_upload(io.BytesIO(csv))

    assert result.data_type_by_year == {2020: "OBSERVED", 2026: "ESTIMATED"}
    assert set(
        ProductionReading.objects.values_list("data_type", flat=True)
    ) == {"OBSERVED", "ESTIMATED"}


@pytest.mark.django_db
def test_ingest_production_with_source_timezone(assets):
    csv = b"Datetime, SPV 1\n01-01-26,1.0\n"

    result = ingest_production_upload(
        io.BytesIO(csv),
        data_type="ESTIMATED",
        year=2026,
        source_timezone="Europe/Paris",
    )

    assert result.log.status == TimeSeriesIngestionLog.Status.SUCCESS
    reading = ProductionReading.objects.get(asset__code="SPV1")
    assert reading.timestamp == datetime(2025, 12, 31, 23, 0, tzinfo=UTC)


@pytest.mark.django_db
def test_unknown_asset_fails_and_logs(assets):
    csv = b"Datetime, UNKNOWN\n01-01-26,1.0\n"

    with pytest.raises(IngestionError) as exc_info:
        ingest_production_upload(io.BytesIO(csv), data_type="ESTIMATED", year=2026)

    error = exc_info.value
    assert error.status_code == 400
    assert error.log is not None
    assert error.log.status == TimeSeriesIngestionLog.Status.FAILED
    assert ProductionReading.objects.count() == 0
    assert "UNKNOWN" in str(error)


@pytest.mark.django_db
def test_negative_power_is_partially_rejected(assets):
    csv = b"Datetime, SPV 1 ,SPV 2\n01-01-26,-1.0,2.0\n"

    result = ingest_production_upload(
        io.BytesIO(csv),
        data_type="ESTIMATED",
        year=2026,
    )

    assert result.log.status == TimeSeriesIngestionLog.Status.PARTIAL
    assert result.log.rows_rejected == 1
    assert ProductionReading.objects.count() == 1
    assert ProductionReading.objects.get().asset.code == "SPV2"


@pytest.mark.django_db
def test_ingest_prices_keeps_negative_prices(tmp_path):
    csv = (
        "#,bidding_zone,settlement_date,settlement_price,delivery_start\n"
        "1,fr,2025-12-31,-10.5,2026-01-01 00:00:00.000000 +00:00\n"
        "2,fr,2025-12-31,20.0,2026-01-01 01:00:00.000000 +00:00\n"
    )
    path = tmp_path / "prices.csv"
    path.write_text(csv, encoding="utf-8")

    result = ingest_prices_file(path)

    assert result.log.status == TimeSeriesIngestionLog.Status.SUCCESS
    assert result.log.rows_ingested == 2
    assert MarketPrice.objects.count() == 2

    first = MarketPrice.objects.get(
        timestamp=datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    )
    assert first.price_eur_mwh == Decimal("-10.500")
    assert first.settlement_date is None
