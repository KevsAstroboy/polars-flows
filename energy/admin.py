from django.contrib import admin

from .models import Asset, MarketPrice, ProductionReading, TimeSeriesIngestionLog


# Register your models here.
@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "type", "nominal_power_mw")
    list_filter = ("type",)
    search_fields = ("name", "code")


@admin.register(MarketPrice)
class MarketPriceAdmin(admin.ModelAdmin):
    list_display = ("bidding_zone", "timestamp", "price_eur_mwh", "settlement_date")
    list_filter = ("bidding_zone",)
    date_hierarchy = "timestamp"


@admin.register(ProductionReading)
class ProductionReadingAdmin(admin.ModelAdmin):
    list_display = ("asset", "timestamp", "power_mw", "data_type")
    list_filter = ("data_type", "asset")
    date_hierarchy = "timestamp"
    list_select_related = ("asset",)


@admin.register(TimeSeriesIngestionLog)
class TimeSeriesIngestionLogAdmin(admin.ModelAdmin):
    list_display = (
        "file_name",
        "kind",
        "status",
        "rows_received",
        "rows_ingested",
        "rows_rejected",
        "duration_ms",
        "started_at",
    )
    list_filter = ("kind", "status")