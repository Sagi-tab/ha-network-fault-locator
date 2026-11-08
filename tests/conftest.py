"""Shared fixtures. The pure analysis tests don't need Home Assistant."""

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
