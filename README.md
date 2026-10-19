# Polars Flows — valorisation d'actifs éoliens

Service REST Django + Polars pour ingérer des séries de production et des prix
de marché, puis exposer des KPIs valorisés par actif et par période.

Le projet répond au test technique : modèle de données, ingestion, pipeline
Polars de valorisation, API DRF et README des choix.

## Sommaire

- [Architecture](#architecture)
- [Stack](#stack)
- [Modèle de données](#modèle-de-données)
- [Installation](#installation)
- [API](#api)
- [Choix techniques](#choix-techniques)
- [Résultats de contrôle](#résultats-de-contrôle)
- [Tests](#tests)
- [Collection Postman](#collection-postman)
- [Limites et extensions](#limites-et-extensions)

## Architecture

```
client / dashboard
       |
       v
+-----------------------------+
| energy/api/                 |  DRF : serializers, views, urls
|  POST /api/ingest/production
|  GET  /api/assets/<id>/performance
+--------------+--------------+
               |
               v
+-----------------------------+
| energy/services/            |  orchestration Django
|  ingestion.py  CSV -> DB    |
|  performance.py DB -> JSON  |
+--------------+--------------+
               |
               v
+-----------------------------+
| services/                   |  Polars pur, zéro import Django
|  analytics.py               |  Step 2 : prix, jointure, valeur, KPIs mensuels UTC
|  ingestion_pipeline.py      |  normalisation des CSV uploadés
|  kpis.py                    |  agrégation daily/monthly + timezone + DST
+--------------+--------------+
               |
               v
+-----------------------------+
| SQLite / PostgreSQL         |
|  Asset, MarketPrice,        |
|  ProductionReading,         |
|  TimeSeriesIngestionLog     |
+-----------------------------+
```

Deux principes :

1. **`services/analytics.py` n'a pas été modifié.** Les fonctions du Step 2
   (`load_prices`, `load_production`, `align_prices`, `value_production`,
   `monthly_kpis`, `compute_monthly_kpis`, `main`) restent la référence.
2. Les ajouts sont **additifs** : `ingestion_pipeline.py` pour les uploads et
   `kpis.py` pour les agrégations daily/monthly avec fuseau horaire.

## Stack

- Python 3.12
- Django 5.2 LTS
- Django REST Framework
- Polars 2.x
- SQLite (développement) ; compatible PostgreSQL
- pytest / pytest-django / ruff

## Modèle de données

| Modèle | Rôle | Clé / contraintes |
|---|---|---|
| `Asset` | actif de production : nom, code, type, puissance nominale | `code` unique, `name` unique |
| `MarketPrice` | prix day-ahead horaire en EUR/MWh | unique `(bidding_zone, timestamp)` |
| `ProductionReading` | puissance moyenne horaire en MW | unique `(asset, timestamp, data_type)` |
| `TimeSeriesIngestionLog` | métadonnées d'un import : fichier, statut, compteurs, durée | — |

Points importants :

- `ProductionReading.data_type` vaut `OBSERVED` ou `ESTIMATED` : une prévision
  et une observation peuvent coexister pour la même heure.
- `power_mw` est nullable : `-` dans le CSV devient `NULL`, jamais `0`.
- les timestamps sont stockés en **UTC**.
- la zone de prix est choisie par le paramètre `price_zone` du GET
  (défaut `FR`) : le fichier fourni ne contient qu'une zone, mais l'API peut
  lire n'importe quelle autre zone présente dans `MarketPrice`.

## Prérequis

- Python 3.12 (uv peut l'installer automatiquement)
- [uv](https://docs.astral.sh/uv/) pour gérer l'environnement et les dépendances
- `curl` et, si vous le souhaitez, Postman pour tester l'API

Installer uv :

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## Installation

1. Récupérer le projet et entrer dans le dossier :

```bash
git clone <url-du-depot> polars-flows
cd polars-flows
```

Si vous avez reçu une archive ZIP, dézippez-la puis placez-vous dans le dossier
`polars-flows`.

2. Vérifier Python et installer les dépendances :

```bash
uv python install 3.12
uv sync
```

`uv sync` lit `pyproject.toml` et `uv.lock`, crée `.venv` et installe Django,
DRF, Polars, pyarrow, pytest et ruff. Il n'y a pas besoin de `pip`.

Si le dossier `.venv` a été livré depuis une autre machine, supprimez-le avant
`uv sync` :

```bash
rm -rf .venv
uv sync
```

3. Créer la base SQLite :

```bash
uv run python manage.py migrate
```

Pour repartir d'une base vierge, supprimez `db.sqlite3` avant `migrate`. La
base livrée peut déjà contenir les données de démonstration ; les commandes
suivantes restent idempotentes.

4. Créer les deux actifs de démonstration :

```bash
uv run python manage.py seed_assets
```

5. Charger les prix de marché :

```bash
uv run python manage.py ingest_prices source/market_price.csv
```

Les deux CSV fournis doivent être présents dans `source/` :

```text
source/market_price.csv
source/eolien_productions.csv
```

6. Lancer le serveur :

```bash
uv run python manage.py runserver
```

Le service écoute sur `http://127.0.0.1:8000`.

7. Dans un second terminal, ingérer la production :

```bash
curl -X POST http://127.0.0.1:8000/api/ingest/production/ \
  -F "file=@source/eolien_productions.csv" \
  -F "data_type=ESTIMATED" \
  -F "year=2026" \
  -F "source_timezone=UTC"
```

Sous Windows, utilisez `curl.exe` (ou Git Bash) pour les commandes `curl`.
Dans PowerShell, placez la commande sur une seule ligne : les `\` de fin de
ligne sont une syntaxe bash/zsh.

Une **collection Postman prête à l'emploi** est également fournie à la racine :
`polars-flows.postman_collection.json`. Elle contient les deux requêtes
(upload de production et lecture des KPIs) ainsi que des tests de statut.
Voir la section [Collection Postman](#collection-postman).

8. Vérifier l'API :

```bash
curl "http://127.0.0.1:8000/api/assets/1/performance/?start_date=2026-01-01&end_date=2026-09-23&granularity=monthly&timezone=UTC&price_zone=FR"
```

9. Vérifier les tests :

```bash
uv run pytest -q
uv run ruff check .
uv run python manage.py check
```

Cette procédure crée les deux actifs (`SPV1` 12 MW, `SPV2` 8 MW), charge les
6 382 prix horaires (zone `FR`, prix négatifs inclus) et les 17 520 readings
de production 2026. Les commandes sont idempotentes : les relancer ne duplique
pas les données.

## API

### POST `/api/ingest/production/`

`multipart/form-data` :

| Champ | Obligatoire | Défaut | Description |
|---|---|---|---|
| `file` | oui | — | CSV de production au format large |
| `data_type` | non | inféré | `OBSERVED` ou `ESTIMATED` |
| `year` | non | toutes | filtre les lignes sur une année |
| `source_timezone` | non | `UTC` | fuseau des timestamps naïfs du CSV |

Format attendu (celui du fichier fourni) :

```
Datetime, SPV 1 ,SPV 2
01-01-26,0.0500,0.0570
1/1/26 0:59,0.1310,0.0050
```

Le pipeline :

1. lit le CSV sans inférence de type ;
2. parse les deux formats de date (`MM-DD-YY` et `M/D/YY H:MM`) ;
3. ramène `:59` à l'heure suivante ;
4. passe de large à long (`SPV 1` → `SPV1`) ;
5. transforme `-` en `NULL` ;
6. infère `OBSERVED` / `ESTIMATED` si `data_type` est absent ;
7. rejette les puissances négatives et les assets inconnus ;
8. fait un upsert idempotent et écrit un `TimeSeriesIngestionLog`.

Règle d'inférence si `data_type` est absent : la **dernière année du fichier**
est `ESTIMATED`, les années précédentes sont `OBSERVED`. Pour le fichier fourni :
2020-2023 → `OBSERVED`, 2026 → `ESTIMATED`.

Exemple :

```bash
curl -X POST http://127.0.0.1:8000/api/ingest/production/ \
  -F "file=@source/eolien_productions.csv" \
  -F "data_type=ESTIMATED" \
  -F "year=2026" \
  -F "source_timezone=UTC"
```

Réponse `201` :

```json
{
  "log_id": 1,
  "file_name": "eolien_productions.csv",
  "status": "SUCCESS",
  "rows_received": 43824,
  "rows_ingested": 17520,
  "rows_rejected": 0,
  "null_values": 714,
  "duration_ms": 423,
  "years_ingested": [2026],
  "data_type_by_year": {"2026": "ESTIMATED"},
  "date_range": {
    "start": "2026-01-01T00:00:00+00:00",
    "end": "2026-12-31T23:00:00+00:00"
  }
}
```

`rows_received` compte les lignes du CSV ; `rows_ingested` compte les readings
en base (une ligne × deux actifs). Si certaines lignes sont rejetées, le statut
est `PARTIAL` et la réponse est `200`. Un asset inconnu renvoie `400` avec le
`log_id` du run en échec.

### GET `/api/assets/<id>/performance/`

| Paramètre | Défaut | Description |
|---|---|---|
| `start_date` | début des données | date civile `YYYY-MM-DD` |
| `end_date` | fin des données | date civile `YYYY-MM-DD` |
| `granularity` | `monthly` | `daily` ou `monthly` |
| `data_type` | `ESTIMATED` | `OBSERVED` ou `ESTIMATED` |
| `timezone` | `Europe/Paris` | fuseau des bornes et des buckets |
| `price_zone` | `FR` | zone de prix utilisée pour la valorisation |

Exemple (UTC, pour reproduire les chiffres du Step 2) :

```bash
curl "http://127.0.0.1:8000/api/assets/1/performance/?\
start_date=2026-01-01&end_date=2026-09-23&\
granularity=monthly&data_type=ESTIMATED&timezone=UTC&price_zone=FR"
```

Réponse :

```json
{
  "asset": {
    "id": 1,
    "code": "SPV1",
    "name": "Polder Eolien 1",
    "type": "WIND",
    "nominal_power_mw": 12.0
  },
  "query": {
    "start_date": "2026-01-01",
    "end_date": "2026-09-23",
    "granularity": "monthly",
    "data_type": "ESTIMATED",
    "timezone": "UTC",
    "price_zone": "FR"
  },
  "kpis": [
    {
      "asset_code": "SPV1",
      "period": "2026-01-01",
      "production_mwh": 1625.122,
      "revenue_eur": 166801.8,
      "hours": 744,
      "negative_price_hours": 0
    }
  ],
  "totals": {
    "production_mwh": 12123.299,
    "revenue_eur": 957288.94,
    "negative_price_hours": 469
  },
  "warnings": [
    "2 point(s) de production sans prix ont été exclus."
  ]
}
```

Les 2 dernières heures du 23 septembre UTC n'ont pas de prix dans le fichier
fourni : la jointure exacte les exclut et le warning le signale. Les totaux
restent ceux du Step 2.

L'endpoint lit la production et les prix en base, construit des DataFrames
Polars, réutilise `value_production` (Step 2) puis agrège avec `aggregate_kpis`.

## Choix techniques

### Structure

`config/` = projet Django, `energy/` = une seule app métier, `services/` =
pipelines Polars purs. Une app suffit pour quatre modèles ; les frontières
internes (`api/`, `services/`) gardent le code lisible.

### UTC, DST et fuseaux

- stockage **UTC** partout (`USE_TZ=True`) ;
- les dates de requête sont des dates civiles dans `timezone` ;
- `Europe/Paris` est le défaut pour les dashboards français ;
- `timezone=UTC` permet de reproduire exactement les KPIs du Step 2 ;
- les buckets daily/monthly sont calculés en heure locale ;
- les heures distinctes sont comptées en UTC.

Conséquence : le 29 mars 2026 en `Europe/Paris` a **23 heures**, le 25 octobre
2026 en a **25**. Le champ `hours` le prouve :

```bash
curl "http://127.0.0.1:8000/api/assets/1/performance/?\
start_date=2026-03-29&end_date=2026-03-29&granularity=daily&timezone=Europe/Paris"
# -> hours = 23

curl "http://127.0.0.1:8000/api/assets/1/performance/?\
start_date=2026-03-29&end_date=2026-03-29&granularity=daily&timezone=UTC"
# -> hours = 24
```

### MW vs MWh

La base stocke une **puissance** (`power_mw`). L'énergie est calculée au moment
de la valorisation :

```
energy_mwh = power_mw × durée_h
revenue_eur = energy_mwh × price_eur_mwh
```

Pour le pas horaire fourni, `durée_h = 1`. Un jour de 23 heures produit donc
23 MWh si la puissance vaut 1 MW, pas 24. Le paramètre `duration_h` de
`value_production` permet de passer à 0,25 pour du 15 min.

### Jointure

La jointure est **exacte** par défaut : une heure de production sans prix est
exclue et signalée dans `warnings`. `join_asof` reste disponible dans
`align_prices` pour un jour où la production serait infra-horaire ; il reporte
le dernier prix connu dans les 59 minutes.

### Données manquantes et prix négatifs

- `-` → `NULL`, exclu des sommes ; zéro n'est pas inventé ;
- les prix négatifs sont conservés (516 lignes dans le fichier, jusqu'à
  -496,86 EUR/MWh) ;
- une heure négative n'est comptée que si l'actif produit strictement plus de
  0 MW ;
- `negative_price_hours` compte des **heures distinctes**, pas des lignes :
  indispensable si on passe un jour au 15 min.

### Idempotence et traçabilité

- upsert sur `(asset, timestamp, data_type)` et `(bidding_zone, timestamp)` ;
- rejouer le même CSV ne duplique rien ;
- chaque exécution crée un `TimeSeriesIngestionLog` avec statut, compteurs,
  durée et message d'erreur ;
- le log est créé en `RUNNING` avant l'écriture et reste présent en cas
  d'échec.

### Logs d'ingestion

Le service `energy.services.ingestion` loggue chaque étape du pipeline avec
des compteurs et des durées. Les logs sont visibles dans la console de
`runserver` (configuration `LOGGING` dans `config/settings.py`) :

```
INFO Ingestion production démarrée | file=... | data_type=ESTIMATED | year=2026
INFO Log d'ingestion créé | log_id=...
INFO CSV lu | rows=43824 | colonnes=[...] | elapsed_ms=...
INFO Normalisation terminée | readings=17520 | années=[2026] | data_types=['ESTIMATED']
INFO Contrôles qualité | null_values=714 | negative_readings=0
INFO Assets présents dans le CSV | codes=['SPV1', 'SPV2']
INFO Assets résolus | codes=['SPV1', 'SPV2']
INFO Préparation des readings | count=17520
INFO Readings préparés | count=17520
INFO Upsert terminé | readings=17520 | elapsed_ms=...
INFO Ingestion production terminée | status=SUCCESS | rows_ingested=17520 | ...
```

Le `TimeSeriesIngestionLog` reste la trace durable en base ; les logs console
servent au diagnostic pendant l'exécution.

### Périmètre

- pas d'authentification (démo) ;
- pas de cache : Polars traite 8 760 lignes par actif en quelques dizaines de
  millisecondes ;
- SQLite convient au test ; le code ORM est compatible PostgreSQL.

## Résultats de contrôle

Avec le fichier fourni, la production 2026 et les prix 2026 (janvier →
23 septembre), la jointure exacte donne :

| Actif | Production | Revenu | Heures prix négatifs |
|---|---:|---:|---:|
| SPV1 | 12 123,299 MWh | 957 288,94 € | 469 |
| SPV2 | 8 705,509 MWh | 682 018,81 € | 469 |
| **Total** | **20 828,808 MWh** | **1 639 307,75 €** | **938** |

Ce sont les mêmes chiffres que le script autonome du Step 2 :

```bash
uv run python -m services.analytics
```

Le script affiche la valorisation horaire, les KPIs mensuels UTC et les totaux.

## Tests

```bash
uv run pytest
uv run ruff check .
uv run python manage.py check
```

Couverture :

- chiffres golden du Step 2 ;
- inférence `OBSERVED` / `ESTIMATED` par année ;
- `-` → `NULL`, prix négatifs, MW → MWh ;
- heures négatives distinctes en 15 min ;
- DST : 23 h le 29 mars, 25 h le 25 octobre ;
- upload, idempotence, asset inconnu, rejet partiel ;
- API : POST, GET daily/monthly, 404, granularité et timezone invalides,
  absence de prix.

Pour tester manuellement les deux services, une collection Postman est
également fournie : voir [Collection Postman](#collection-postman).

## Collection Postman

Une collection de test est fournie à la racine :
`polars-flows.postman_collection.json`. Elle sert à tester les deux services
sans écrire de client HTTP à la main.

### Importer la collection

1. Ouvrir Postman.
2. **Import** → **File** → sélectionner
   `polars-flows.postman_collection.json`.
3. Vérifier les variables de collection :

| Variable | Valeur par défaut | Rôle |
|---|---|---|
| `base_url` | `http://127.0.0.1:8000` | adresse du serveur |
| `asset_id` | `1` | actif à interroger |
| `timezone` | `Europe/Paris` | fuseau des buckets |
| `price_zone` | `FR` | zone de prix utilisée |

### Lancer les tests

1. Démarrer le serveur :

```bash
uv run python manage.py runserver
```

2. Exécuter la requête **1. Ingest production CSV**. Elle envoie
   `source/eolien_productions.csv` avec `data_type=ESTIMATED` et `year=2026`.
3. Exécuter la requête **2. Asset performance**. Elle lit les KPIs mensuels de
   l'actif `1` sur janvier → septembre 2026.
4. Vérifier que les tests Postman passent : statut 200/201, `kpis` non vide,
   `totals.production_mwh` numérique.

Dans l'onglet **Body** de la requête d'upload, vérifier le chemin du fichier :
Postman peut demander de le sélectionner à nouveau après l'import.

La collection n'est pas exécutée par Django : c'est un outil de test manuel,
en complément de `pytest`.

## Limites et extensions

- les prix fournis s'arrêtent au **23 septembre 2026 21:00 UTC** ; les mois
  d'octobre à décembre 2026 n'ont pas de prix et sont exclus de la valorisation ;
- le CSV de production est interprété en **UTC** par défaut (grille 24 h/jour
  du fichier). Un CSV en heure locale peut utiliser `source_timezone=Europe/Paris` ;
- `settlement_date` n'est pas persisté (le modèle l'autorise à `NULL`) ;
- **zone par requête** : `price_zone` est paramétrable (défaut `FR`) mais n'est
  pas validée contre les zones existantes ; si aucun prix ne correspond, l'API
  renvoie `kpis: []` et un warning. Un portefeuille multi-zones demanderait une
  règle de mapping actif → zone ;
- le format long (`asset_code`, `timestamp`, `power_mw`) n'est pas encore
  accepté par le POST ; le format large du test l'est ;
- extensions naturelles : connecteur ENTSO-E, MTU 15 min, cache Django,
  authentification, PostgreSQL, calcul de capture price et de revenu
  portefeuille.
