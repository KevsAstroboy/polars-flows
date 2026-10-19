"""Orchestration de l'ingestion : Polars -> Django ORM -> log."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import polars as pl
from django.db import transaction
from django.utils import timezone as django_timezone

from energy.models import (
    Asset,
    MarketPrice,
    ProductionReading,
    TimeSeriesIngestionLog,
)
from services.analytics import load_prices
from services.ingestion_pipeline import (
    normalize_production_frame,
    read_production_csv,
)

logger = logging.getLogger(__name__)


@dataclass
class IngestionResult:
    log: TimeSeriesIngestionLog
    years: list[int] = field(default_factory=list)
    data_type_by_year: dict[int, str] = field(default_factory=dict)
    timestamp_min: datetime | None = None
    timestamp_max: datetime | None = None


class IngestionError(Exception):
    """Erreur d'ingestion destinée à l'API (400) ou interne (500)."""

    def __init__(
        self,
        message: str,
        *,
        log: TimeSeriesIngestionLog | None = None,
        details: dict | None = None,
        status_code: int = 400,
    ) -> None:
        super().__init__(message)
        self.log = log
        self.details = details or {}
        self.status_code = status_code


def _quantize(value: float | None, exponent: str = "0.001") -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal(exponent))


def _finish_log(
    log: TimeSeriesIngestionLog,
    *,
    status: str,
    started: float,
    rows_received: int = 0,
    rows_ingested: int = 0,
    rows_rejected: int = 0,
    null_values: int = 0,
    error_message: str = "",
) -> None:
    log.status = status
    log.rows_received = rows_received
    log.rows_ingested = rows_ingested
    log.rows_rejected = rows_rejected
    log.null_values = null_values
    log.finished_at = django_timezone.now()
    log.duration_ms = int((time.perf_counter() - started) * 1000)
    log.error_message = error_message[:2000]
    log.save(
        update_fields=[
            "status",
            "rows_received",
            "rows_ingested",
            "rows_rejected",
            "null_values",
            "finished_at",
            "duration_ms",
            "error_message",
        ]
    )


def _metadata(frame: pl.DataFrame) -> tuple[list[int], dict[int, str], datetime | None, datetime | None]:
    if frame.is_empty():
        return [], {}, None, None

    years = sorted(frame["timestamp"].dt.year().unique().to_list())
    by_year = (
        frame.with_columns(pl.col("timestamp").dt.year().alias("_year"))
        .group_by(["_year", "data_type"])
        .len()
        .sort(["_year", "data_type"])
    )
    data_type_by_year = {
        int(row["_year"]): row["data_type"] for row in by_year.iter_rows(named=True)
    }
    return years, data_type_by_year, frame["timestamp"].min(), frame["timestamp"].max()


def ingest_production_upload(
    file,
    *,
    data_type: str | None = None,
    year: int | None = None,
    source_timezone: str = "UTC",
) -> IngestionResult:
    """Ingère un CSV de production uploadé (objet fichier ou bytes)."""
    started = time.perf_counter()
    file_name = getattr(file, "name", None) or "upload.csv"

    logger.info(
        "Ingestion production démarrée | file=%s | data_type=%s | year=%s | timezone=%s",
        file_name,
        data_type or "auto",
        year if year is not None else "toutes",
        source_timezone,
    )

    log = TimeSeriesIngestionLog.objects.create(
        file_name=file_name,
        kind=TimeSeriesIngestionLog.Kind.PRODUCTION,
        status=TimeSeriesIngestionLog.Status.RUNNING,
    )
    logger.info("Log d'ingestion créé | log_id=%s", log.id)

    rows_received = 0
    rows_rejected = 0
    null_values = 0

    try:
        content = file.read() if hasattr(file, "read") else file
        raw = read_production_csv(content)
        rows_received = raw.height
        logger.info(
            "CSV lu | rows=%s | colonnes=%s | elapsed_ms=%s",
            raw.height,
            raw.columns,
            int((time.perf_counter() - started) * 1000),
        )

        frame = normalize_production_frame(
            raw,
            data_type=data_type,
            year=year,
            source_timezone=source_timezone,
        )

        years_detected = sorted(frame["timestamp"].dt.year().unique().to_list())
        data_types_detected = sorted(frame["data_type"].unique().to_list())
        logger.info(
            "Normalisation terminée | readings=%s | années=%s | data_types=%s | elapsed_ms=%s",
            frame.height,
            years_detected,
            data_types_detected,
            int((time.perf_counter() - started) * 1000),
        )

        null_values = frame.filter(pl.col("power_mw").is_null()).height
        rows_rejected = frame.filter(pl.col("power_mw") < 0).height
        logger.info(
            "Contrôles qualité | null_values=%s | negative_readings=%s",
            null_values,
            rows_rejected,
        )
        if rows_rejected:
            logger.warning(
                "Puissances négatives rejetées | count=%s", rows_rejected
            )

        frame = frame.filter(
            pl.col("power_mw").is_null() | (pl.col("power_mw") >= 0)
        )

        if frame.is_empty():
            raise IngestionError(
                "Toutes les lignes de production ont été rejetées.",
                details={"rows_rejected": rows_rejected},
            )

        codes = sorted(frame["asset_code"].unique().to_list())
        logger.info("Assets présents dans le CSV | codes=%s", codes)

        assets = {asset.code: asset for asset in Asset.objects.filter(code__in=codes)}
        missing = [code for code in codes if code not in assets]
        known = sorted(Asset.objects.values_list("code", flat=True))
        if missing:
            logger.error(
                "Assets inconnus | missing=%s | known=%s", missing, known
            )
            raise IngestionError(
                "Assets inconnus: " + ", ".join(missing),
                details={
                    "missing_assets": missing,
                    "known_assets": known,
                },
            )
        logger.info("Assets résolus | codes=%s", sorted(assets))

        logger.info("Préparation des readings | count=%s", frame.height)
        readings = [
            ProductionReading(
                asset=assets[row["asset_code"]],
                timestamp=row["timestamp"],
                power_mw=_quantize(row["power_mw"]),
                data_type=row["data_type"],
            )
            for row in frame.iter_rows(named=True)
        ]
        logger.info("Readings préparés | count=%s", len(readings))

        write_started = time.perf_counter()
        with transaction.atomic():
            ProductionReading.objects.bulk_create(
                readings,
                batch_size=500,
                update_conflicts=True,
                unique_fields=["asset", "timestamp", "data_type"],
                update_fields=["power_mw"],
            )
        logger.info(
            "Upsert terminé | readings=%s | elapsed_ms=%s",
            len(readings),
            int((time.perf_counter() - write_started) * 1000),
        )

        years, data_type_by_year, ts_min, ts_max = _metadata(frame)
        status = (
            TimeSeriesIngestionLog.Status.PARTIAL
            if rows_rejected
            else TimeSeriesIngestionLog.Status.SUCCESS
        )
        if rows_rejected:
            logger.warning(
                "Ingestion partielle | rows_rejected=%s | rows_ingested=%s",
                rows_rejected,
                len(readings),
            )

        _finish_log(
            log,
            status=status,
            started=started,
            rows_received=rows_received,
            rows_ingested=len(readings),
            rows_rejected=rows_rejected,
            null_values=null_values,
        )
        logger.info(
            "Ingestion production terminée | log_id=%s | status=%s | "
            "rows_received=%s | rows_ingested=%s | rows_rejected=%s | "
            "null_values=%s | duration_ms=%s | years=%s | data_type_by_year=%s",
            log.id,
            log.status,
            log.rows_received,
            log.rows_ingested,
            log.rows_rejected,
            log.null_values,
            log.duration_ms,
            years,
            data_type_by_year,
        )

        return IngestionResult(
            log=log,
            years=years,
            data_type_by_year=data_type_by_year,
            timestamp_min=ts_min,
            timestamp_max=ts_max,
        )

    except IngestionError as exc:
        _finish_log(
            log,
            status=TimeSeriesIngestionLog.Status.FAILED,
            started=started,
            rows_received=rows_received,
            rows_rejected=rows_rejected,
            null_values=null_values,
            error_message=str(exc),
        )
        logger.error(
            "Ingestion production échouée | log_id=%s | rows_received=%s | "
            "rows_rejected=%s | error=%s",
            log.id,
            rows_received,
            rows_rejected,
            exc,
        )
        exc.log = log
        raise

    except (ValueError, pl.exceptions.PolarsError) as exc:
        _finish_log(
            log,
            status=TimeSeriesIngestionLog.Status.FAILED,
            started=started,
            rows_received=rows_received,
            rows_rejected=rows_rejected,
            null_values=null_values,
            error_message=str(exc),
        )
        logger.warning(
            "CSV production invalide | log_id=%s | rows_received=%s | error=%s",
            log.id,
            rows_received,
            exc,
        )
        raise IngestionError(str(exc), log=log, status_code=400) from exc

    except Exception as exc:
        _finish_log(
            log,
            status=TimeSeriesIngestionLog.Status.FAILED,
            started=started,
            rows_received=rows_received,
            rows_rejected=rows_rejected,
            null_values=null_values,
            error_message=str(exc),
        )
        logger.exception(
            "Erreur interne pendant l'ingestion production | log_id=%s | "
            "rows_received=%s | rows_ingested=0",
            log.id,
            rows_received,
        )
        raise IngestionError(
            f"Erreur interne pendant l'ingestion: {exc}",
            log=log,
            status_code=500,
        ) from exc


def ingest_prices_file(
    path: str | Path,
    *,
    zone: str = "FR",
) -> IngestionResult:
    """Ingère le CSV de prix existant via ``load_prices`` (inchangé)."""
    started = time.perf_counter()
    path = Path(path)
    log = TimeSeriesIngestionLog.objects.create(
        file_name=path.name,
        kind=TimeSeriesIngestionLog.Kind.PRICES,
        status=TimeSeriesIngestionLog.Status.RUNNING,
    )

    rows_received = 0
    try:
        frame = load_prices(path, zone=zone)
        rows_received = frame.height

        prices = [
            MarketPrice(
                bidding_zone=row["bidding_zone"],
                timestamp=row["timestamp"],
                price_eur_mwh=_quantize(row["price_eur_mwh"]),
                settlement_date=None,
            )
            for row in frame.iter_rows(named=True)
        ]

        with transaction.atomic():
            MarketPrice.objects.bulk_create(
                prices,
                batch_size=500,
                update_conflicts=True,
                unique_fields=["bidding_zone", "timestamp"],
                update_fields=["price_eur_mwh"],
            )

        _finish_log(
            log,
            status=TimeSeriesIngestionLog.Status.SUCCESS,
            started=started,
            rows_received=rows_received,
            rows_ingested=len(prices),
        )

        years, _, ts_min, ts_max = _metadata(
            frame.with_columns(pl.lit("OBSERVED").alias("data_type"))
        )
        return IngestionResult(
            log=log,
            years=years,
            timestamp_min=ts_min,
            timestamp_max=ts_max,
        )

    except (OSError, ValueError, pl.exceptions.PolarsError) as exc:
        _finish_log(
            log,
            status=TimeSeriesIngestionLog.Status.FAILED,
            started=started,
            rows_received=rows_received,
            error_message=str(exc),
        )
        raise IngestionError(str(exc), log=log, status_code=400) from exc

    except Exception as exc:
        _finish_log(
            log,
            status=TimeSeriesIngestionLog.Status.FAILED,
            started=started,
            rows_received=rows_received,
            error_message=str(exc),
        )
        raise IngestionError(
            f"Erreur interne pendant l'ingestion des prix: {exc}",
            log=log,
            status_code=500,
        ) from exc
