"""Shared fixtures. The pure analysis tests don't need Home Assistant."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    import pytest_homeassistant_custom_component  # noqa: F401
except ImportError:  # running only tests/test_analysis.py without HA installed
    pytest_plugins: list[str] = []
else:

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(enable_custom_integrations):
        """Load integrations from custom_components/."""
        return

    @pytest.fixture(autouse=True)
    def frontend(hass):
        """Stand in for the frontend/http dependencies (their packages aren't installed)."""
        hass.config.components.update({"frontend", "http"})
        hass.http = MagicMock(async_register_static_paths=AsyncMock())
        with patch("custom_components.network_fault_locator.add_extra_js_url") as extra_js:
            yield {"http": hass.http, "extra_js": extra_js}
