from django.db import models

# Create your models here.

class Asset(models.Model):
    class Type(models.TextChoices):
        WIND = "WIND", "Éolien"
        SOLAR = "SOLAR", "Solaire"
        BATTERY = "BATTERY", "Batterie"
        HYDRO = "HYDRO", "Hydraulique"

    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(max_length=10, unique=True)
    type = models.CharField(choices=Type.choices, max_length=10)
    nominal_power_mw = models.DecimalField(max_digits=10, decimal_places=3)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.code})"


class MarketPrice(models.Model):
    bidding_zone = models.CharField(max_length=10)
    timestamp = models.DateTimeField(help_text="Début de livraison, en UTC")
    price_eur_mwh = models.DecimalField(max_digits=10, decimal_places=3)
    settlement_date = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["timestamp"]
        constraints = [
            models.UniqueConstraint(
                fields=["bidding_zone", "timestamp"],
                name="uq_market_price_zone_ts",
            )
        ]
    def __str__(self):
        return f"{self.bidding_zone} {self.timestamp:%Y-%m-%d %H:%M} {self.price_eur_mwh} EUR/MWh"

class ProductionReading(models.Model):
    class DataType(models.TextChoices):
        OBSERVED = "OBSERVED", "Observé"
        ESTIMATED = "ESTIMATED", "Estimé"

    asset = models.ForeignKey(
            Asset,
            on_delete=models.CASCADE,
            related_name="production_readings"
    )

    timestamp = models.DateTimeField(help_text="Début de l'heure, en UTC")
    power_mw = models.DecimalField(
            max_digits=8,
            decimal_places=3,
            null=True,
            blank=True,
            help_text="Puissance moyenne sur l'heure ; NULL si donnée manquante",
    )

    data_type = models.CharField(
            max_length=10,
            choices=DataType.choices,
            default=DataType.OBSERVED,
        )

    class Meta:
        ordering = ["asset", "timestamp"]
        constraints = [
            models.UniqueConstraint(
                fields=["asset", "timestamp", "data_type"],
                name="uq_production_asset_ts_type",
                )
            ]

    def __str__(self):
        return f"{self.asset.code} {self.timestamp:%Y-%m-%d %H:%M} {self.power_mw} MW"


class TimeSeriesIngestionLog(models.Model):
    class Kind(models.TextChoices):
        PRICES = "PRICES", "Prix"
        PRODUCTION = "PRODUCTION", "Production"

    class Status(models.TextChoices):
        RUNNING = "RUNNING", "En cours"
        SUCCESS = "SUCCESS", "Succès"
        PARTIAL = "PARTIAL", "Partiel"
        FAILED = "FAILED", "Échec"

    file_name = models.CharField(max_length=255)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.RUNNING,
    )
    rows_received = models.PositiveIntegerField(default=0)
    rows_ingested = models.PositiveIntegerField(default=0)
    rows_rejected = models.PositiveIntegerField(default=0)
    null_values = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    error_message = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.file_name} [{self.status}]"