"""Logbook entries for Network Fault Locator events."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.logbook import LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import Event, HomeAssistant, callback

from .analysis import ISSUE_LABELS
from .const import DOMAIN, EVENT_INCIDENT_RESOLVED, EVENT_INCIDENT_STARTED, EVENT_ISP_HOP_CHANGED


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    """Describe logbook events."""

    @callback
    def describe_started(event: Event) -> dict[str, str]:
        labels = ", ".join(ISSUE_LABELS.get(i, i) for i in event.data.get("issues", []))
        return {
            LOGBOOK_ENTRY_NAME: event.data.get("name", "Network Fault Locator"),
            LOGBOOK_ENTRY_MESSAGE: f"network incident started: {labels}",
        }

    @callback
    def describe_resolved(event: Event) -> dict[str, str]:
        summary = event.data.get("summary", "").replace("\n", "; ")
        return {
            LOGBOOK_ENTRY_NAME: event.data.get("name", "Network Fault Locator"),
            LOGBOOK_ENTRY_MESSAGE: f"network incident resolved: {summary}",
        }

    @callback
    def describe_hop(event: Event) -> dict[str, str]:
        return {
            LOGBOOK_ENTRY_NAME: event.data.get("name", "Network Fault Locator"),
            LOGBOOK_ENTRY_MESSAGE: (
                f"ISP first hop changed: {event.data.get('old')} → {event.data.get('new')}"
            ),
        }

    async_describe_event(DOMAIN, EVENT_INCIDENT_STARTED, describe_started)
    async_describe_event(DOMAIN, EVENT_INCIDENT_RESOLVED, describe_resolved)
    async_describe_event(DOMAIN, EVENT_ISP_HOP_CHANGED, describe_hop)
