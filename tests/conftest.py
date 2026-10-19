from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from energy.models import Asset


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def assets(db):
    first = Asset.objects.create(
        code="SPV1",
        name="Polder Eolien 1",
        type=Asset.Type.WIND,
        nominal_power_mw=Decimal("12.000"),
    )
    second = Asset.objects.create(
        code="SPV2",
        name="Polder Eolien 2",
        type=Asset.Type.WIND,
        nominal_power_mw=Decimal("8.000"),
    )
    return first, second
