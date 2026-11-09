"""Fixtures for C-Bus tests."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.fake_pci import FakeNetwork


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request):
    """Enable custom integrations for HA tests only."""
    if "hass" in request.fixturenames:
        request.getfixturevalue("enable_custom_integrations")
    yield


@pytest.fixture
def network():
    """A fake C-Bus network: GA0 off, GA1 on, GA3 dimmed, GA5 off."""
    net = FakeNetwork({0: 0, 1: 255, 3: 128, 5: 0})
    with patch("custom_components.cbus.protocol._SerialPort", net.factory):
        yield net
