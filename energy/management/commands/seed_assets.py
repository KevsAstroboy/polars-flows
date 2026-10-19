from decimal import Decimal

from django.core.management.base import BaseCommand

from energy.models import Asset

ASSETS = (
    {
        "code": "SPV1",
        "name": "Polder Eolien 1",
        "type": Asset.Type.WIND,
        "nominal_power_mw": Decimal("12.000"),
    },
    {
        "code": "SPV2",
        "name": "Polder Eolien 2",
        "type": Asset.Type.WIND,
        "nominal_power_mw": Decimal("8.000"),
    },
)


class Command(BaseCommand):
    help = "Crée ou met à jour les deux actifs éoliens de démonstration."

    def handle(self, *args, **options):
        for payload in ASSETS:
            asset, created = Asset.objects.update_or_create(
                code=payload["code"],
                defaults=payload,
            )
            action = "créé" if created else "mis à jour"
            self.stdout.write(
                self.style.SUCCESS(
                    f"{asset.code} {action} ({asset.nominal_power_mw} MW)"
                )
            )
