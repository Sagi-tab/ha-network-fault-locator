"""Coordinator: runs one check per interval and keeps loss windows and incidents."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from icmplib import ICMPLibError, SocketPermissionError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .analysis import (
    HopResult,
    IncidentTracker,
    LossWindow,
    Snapshot,
    Verdict,
    evaluate,
    parse_access_points,
    record_samples,
)
from .const import (
    CONF_ACCESS_POINTS,
    CONF_DNS_HOST,
    CONF_INTERNET_TARGETS,
    CONF_LOSS_THRESHOLD,
    CONF_LOSS_WINDOW,
    CONF_MIN_INCIDENT,
    CONF_NOTIFY_ENTITY,
    CONF_PING_COUNT,
    CONF_ROUTER,
    CONF_SCAN_INTERVAL,
    DEFAULT_DNS_HOST,
    DEFAULT_INTERNET_TARGETS,
    DEFAULT_LOSS_THRESHOLD,
    DEFAULT_LOSS_WINDOW,
    DEFAULT_MIN_INCIDENT,
    DEFAULT_NAME,
    DEFAULT_PING_COUNT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_INCIDENT_RESOLVED,
    EVENT_INCIDENT_STARTED,
    EVENT_ISP_HOP_CHANGED,
    STORAGE_VERSION,
)
from .probe import async_dns_ok, async_ping_hosts, trace_first_hops

_LOGGER = logging.getLogger(__name__)

type NetworkFaultLocatorConfigEntry = ConfigEntry[NetworkFaultLocatorCoordinator]


@dataclass
class NetworkState:
    """What the entities read after each check."""

    snapshot: Snapshot
    verdict: Verdict
    isp_hop_address: str | None  # last known ISP first hop (kept while it's silent)
    incident_since: datetime | None


def _local_hm(ts: float) -> str:
    return dt_util.as_local(datetime.fromtimestamp(ts, tz=UTC)).strftime("%H:%M")


def format_incident(incident: dict[str, Any]) -> str:
    """Human-readable incident summary, one line per issue plus a total."""
    lines = [
        f"• {item['label']}: {max(round(item['duration'] / 60), 1)} min from "
        f"{_local_hm(item['start'])}"
        for item in incident["issues"]
    ]
    lines.append(
        f"Total: {round(incident['duration'] / 60)} min "
        f"({_local_hm(incident['start'])}–{_local_hm(incident['end'])})"
    )
    return "\n".join(lines)


class NetworkFaultLocatorCoordinator(DataUpdateCoordinator[NetworkState]):
    """Probes the network path and locates faults."""

    config_entry: NetworkFaultLocatorConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: NetworkFaultLocatorConfigEntry,
        privileged: bool,
    ) -> None:
        opts = {**entry.data, **entry.options}
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=opts.get(CONF_NAME, DEFAULT_NAME),
            update_interval=timedelta(
                seconds=opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.privileged = privileged
        self.router: str = opts[CONF_ROUTER]
        self.access_points: dict[str, str] = parse_access_points(
            opts.get(CONF_ACCESS_POINTS, [])
        )
        self.internet_targets: list[str] = list(
            opts.get(CONF_INTERNET_TARGETS) or DEFAULT_INTERNET_TARGETS
        )
        self.dns_host: str = opts.get(CONF_DNS_HOST) or DEFAULT_DNS_HOST
        self.notify_entity: str | None = opts.get(CONF_NOTIFY_ENTITY) or None
        self.ping_count = int(opts.get(CONF_PING_COUNT, DEFAULT_PING_COUNT))
        self.threshold = float(opts.get(CONF_LOSS_THRESHOLD, DEFAULT_LOSS_THRESHOLD))
        self.min_incident = int(opts.get(CONF_MIN_INCIDENT, DEFAULT_MIN_INCIDENT))

        window = 60 * float(opts.get(CONF_LOSS_WINDOW, DEFAULT_LOSS_WINDOW))
        self._router_window = LossWindow(window)
        self._ap_windows = {label: LossWindow(window) for label in self.access_points}
        self._internet_window = LossWindow(window)

        # Traceroute needs raw sockets; without them the ISP first hop is unknown
        self.trace_available = privileged
        self._isp_hop_address: str | None = None
        self._tracker = IncidentTracker()
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )

    async def async_load_state(self) -> None:
        """Restore an open incident and the last ISP hop after a restart."""
        data = await self._store.async_load() or {}
        self._tracker = IncidentTracker.from_dict(data.get("incident"))
        self._isp_hop_address = data.get("isp_hop_address")

    def _save_state(self) -> None:
        self._store.async_delay_save(
            lambda: {
                "incident": self._tracker.as_dict(),
                "isp_hop_address": self._isp_hop_address,
            },
            5,
        )

    async def _async_trace(self) -> HopResult | None:
        if not self.trace_available:
            return None
        try:
            _, hop = await self.hass.async_add_executor_job(
                trace_first_hops, self.internet_targets[0]
            )
        except SocketPermissionError:
            _LOGGER.warning(
                "Traceroute needs raw socket access; ISP first-hop detection is disabled"
            )
            self.trace_available = False
            return None
        except (ICMPLibError, OSError) as err:
            _LOGGER.debug("Traceroute failed: %s", err)
            return HopResult(None, 0, 0)
        return hop

    async def _async_update_data(self) -> NetworkState:
        hosts = [self.router, *self.access_points.values(), *self.internet_targets]
        try:
            pings, dns_ok, hop = await asyncio.gather(
                async_ping_hosts(hosts, self.ping_count, self.privileged),
                async_dns_ok(self.dns_host),
                self._async_trace(),
            )
        except (ICMPLibError, OSError) as err:
            raise UpdateFailed(f"Network probe failed: {err}") from err

        now = dt_util.utcnow().timestamp()
        snapshot = Snapshot(
            router=pings[self.router],
            access_points={
                label: pings[host] for label, host in self.access_points.items()
            },
            internet=[pings[host] for host in self.internet_targets],
            dns_ok=dns_ok,
            hop=hop,
        )
        record_samples(
            snapshot, now, self._router_window, self._ap_windows, self._internet_window
        )
        verdict = evaluate(snapshot, self.threshold)

        self._handle_hop(hop)
        event = self._tracker.update(now, verdict.issues)
        if event is not None:
            self._handle_incident_event(*event)
        self._save_state()

        since = self._tracker.since
        return NetworkState(
            snapshot=snapshot,
            verdict=verdict,
            isp_hop_address=self._isp_hop_address,
            incident_since=(
                datetime.fromtimestamp(since, tz=UTC) if since is not None else None
            ),
        )

    def _handle_hop(self, hop: HopResult | None) -> None:
        if hop is None or hop.address is None:
            return
        previous = self._isp_hop_address
        self._isp_hop_address = hop.address
        if previous and previous != hop.address:
            self.hass.bus.async_fire(
                EVENT_ISP_HOP_CHANGED,
                {
                    "entry_id": self.config_entry.entry_id,
                    "name": self.name,
                    "old": previous,
                    "new": hop.address,
                },
            )

    def _handle_incident_event(self, kind: str, incident: dict[str, Any]) -> None:
        base = {"entry_id": self.config_entry.entry_id, "name": self.name}
        if kind == "started":
            self.hass.bus.async_fire(
                EVENT_INCIDENT_STARTED,
                {**base, "issues": [i["issue"] for i in incident["issues"]]},
            )
            return
        if incident["duration"] < self.min_incident:
            _LOGGER.debug("Ignoring short incident: %s", incident)
            return
        summary = format_incident(incident)
        self.hass.bus.async_fire(
            EVENT_INCIDENT_RESOLVED, {**base, **incident, "summary": summary}
        )
        if self.notify_entity:
            self.hass.async_create_task(self._async_notify(summary))

    async def _async_notify(self, summary: str) -> None:
        try:
            await self.hass.services.async_call(
                "notify",
                "send_message",
                {
                    "entity_id": self.notify_entity,
                    "title": "🌐 Network issue resolved",
                    "message": summary,
                },
                blocking=True,
            )
        except Exception:  # noqa: BLE001 - a failed notification must not break probing
            _LOGGER.exception("Could not send the incident notification")
