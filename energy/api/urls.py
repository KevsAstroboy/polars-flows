from django.urls import path

from .views import AssetPerformanceView, ProductionIngestionView

urlpatterns = [
    path(
        "ingest/production/",
        ProductionIngestionView.as_view(),
        name="ingest-production",
    ),
    path(
        "assets/<int:pk>/performance/",
        AssetPerformanceView.as_view(),
        name="asset-performance",
    ),
]
