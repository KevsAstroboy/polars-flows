from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from energy.services.ingestion import IngestionError, ingest_prices_file


class Command(BaseCommand):
    help = "Ingère un CSV de prix de marché (colonnes du fichier source)."

    def add_arguments(self, parser):
        parser.add_argument("path", type=Path)
        parser.add_argument("--zone", default="FR")

    def handle(self, *args, **options):
        try:
            result = ingest_prices_file(options["path"], zone=options["zone"])
        except IngestionError as exc:
            raise CommandError(str(exc)) from exc

        log = result.log
        self.stdout.write(
            self.style.SUCCESS(
                f"{log.file_name}: {log.status}, "
                f"{log.rows_ingested} prix ingérés, {log.duration_ms} ms"
            )
        )
