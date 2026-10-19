from zoneinfo import ZoneInfo

from rest_framework import serializers

from energy.models import ProductionReading


class ProductionUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    data_type = serializers.ChoiceField(
        choices=ProductionReading.DataType.choices,
        required=False,
        allow_null=True,
    )
    year = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1900,
        max_value=2200,
    )
    source_timezone = serializers.CharField(required=False, default="UTC")

    def validate_source_timezone(self, value: str) -> str:
        try:
            ZoneInfo(value)
        except Exception as exc:
            raise serializers.ValidationError(
                "Timezone IANA invalide (ex: UTC, Europe/Paris)."
            ) from exc
        return value


class PerformanceQuerySerializer(serializers.Serializer):
    start_date = serializers.DateField(required=False)
    end_date = serializers.DateField(required=False)
    granularity = serializers.ChoiceField(
        choices=("daily", "monthly"),
        default="monthly",
    )
    data_type = serializers.ChoiceField(
        choices=ProductionReading.DataType.choices,
        default=ProductionReading.DataType.ESTIMATED,
    )
    timezone = serializers.CharField(required=False, default="Europe/Paris")
    price_zone = serializers.CharField(
        required=False,
        default="FR",
        max_length=10,
    )

    def validate_price_zone(self, value: str) -> str:
        zone = value.strip().upper()
        if not zone:
            raise serializers.ValidationError("price_zone ne peut pas être vide.")
        return zone

    def validate_timezone(self, value: str) -> str:
        try:
            ZoneInfo(value)
        except Exception as exc:
            raise serializers.ValidationError(
                "Timezone IANA invalide (ex: UTC, Europe/Paris)."
            ) from exc
        return value

    def validate(self, attrs):
        start = attrs.get("start_date")
        end = attrs.get("end_date")
        if start and end and start > end:
            raise serializers.ValidationError(
                "start_date doit être antérieure ou égale à end_date."
            )
        return attrs
