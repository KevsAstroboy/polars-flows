from django.shortcuts import get_object_or_404
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from energy.models import Asset, TimeSeriesIngestionLog
from energy.services.ingestion import IngestionError, ingest_production_upload
from energy.services.performance import asset_performance

from .serializers import PerformanceQuerySerializer, ProductionUploadSerializer


def _log_payload(result) -> dict:
    log = result.log
    return {
        "log_id": log.id,
        "file_name": log.file_name,
        "status": log.status,
        "rows_received": log.rows_received,
        "rows_ingested": log.rows_ingested,
        "rows_rejected": log.rows_rejected,
        "null_values": log.null_values,
        "duration_ms": log.duration_ms,
        "years_ingested": result.years,
        "data_type_by_year": {
            str(year): data_type
            for year, data_type in result.data_type_by_year.items()
        },
        "date_range": {
            "start": (
                result.timestamp_min.isoformat() if result.timestamp_min else None
            ),
            "end": result.timestamp_max.isoformat() if result.timestamp_max else None,
        },
    }


class ProductionIngestionView(APIView):
    parser_classes = (MultiPartParser, FormParser)

    def post(self, request):
        serializer = ProductionUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        payload = serializer.validated_data
        try:
            result = ingest_production_upload(
                payload["file"],
                data_type=payload.get("data_type"),
                year=payload.get("year"),
                source_timezone=payload["source_timezone"],
            )
        except IngestionError as exc:
            body = {
                "error": str(exc),
                "details": exc.details,
            }
            if exc.log is not None:
                body["log_id"] = exc.log.id
                body["status"] = exc.log.status
            return Response(body, status=exc.status_code)

        status_code = (
            201
            if result.log.status == TimeSeriesIngestionLog.Status.SUCCESS
            else 200
        )
        return Response(_log_payload(result), status=status_code)


class AssetPerformanceView(APIView):
    def get(self, request, pk: int):
        asset = get_object_or_404(Asset, pk=pk)

        serializer = PerformanceQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)

        payload = asset_performance(asset, **serializer.validated_data)
        return Response(payload)
