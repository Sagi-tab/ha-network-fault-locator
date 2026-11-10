"""Pure fault-location logic (no Home Assistant imports, unit-testable).

Network path, as seen from Home Assistant:

    HA -> router -> local devices you choose (access points, switches, ...)
                 -> ISP first hop (hop 2, found by traceroute) -> internet targets (+ DNS)

A failure (or loss) is attributed to the first link on that path where it appears.
Loss to a local device or the internet only counts beyond the loss already seen to
the router, so a problem right next to Home Assistant is not blamed on them.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import NamedTuple

# Issue codes, most severe first. The status sensor shows the first active one.
HOME_NETWORK = "home_network"
ISP_LINE = "isp_line"
ISP_UPSTREAM = "isp_upstream"
ISP_DOWN = "isp_down"
DNS = "dns"
DEVICE_DOWN = "device_down"
HOME_DEGRADED = "home_degraded"
DEVICE_DEGRADED = "device_degraded"
ISP_DEGRADED = "isp_degraded"

ISSUES: tuple[str, ...] = (
    HOME_NETWORK,
    ISP_LINE,
    ISP_UPSTREAM,
    ISP_DOWN,
    DNS,
    DEVICE_DOWN,
    HOME_DEGRADED,
    DEVICE_DEGRADED,
    ISP_DEGRADED,
)
OK = "ok"

# Packet-loss issues whose times come from the lossy checks themselves
DEGRADED_ISSUES = frozenset({HOME_DEGRADED, DEVICE_DEGRADED, ISP_DEGRADED})

# A packet-loss issue needs loss in at least this many checks within the window:
# one short burst (a few lost packets in a single check) is not a problem worth
# reporting, and would otherwise keep the verdict up for the whole window.
MIN_LOSSY_CHECKS = 2

ISSUE_LABELS: dict[str, str] = {
    HOME_NETWORK: "Home network (router unreachable)",
    ISP_LINE: "ISP line / access",
    ISP_UPSTREAM: "ISP backbone",
    ISP_DOWN: "ISP / internet down",
    DNS: "DNS",
    DEVICE_DOWN: "Local device unreachable",
    HOME_DEGRADED: "Home network packet loss",
    DEVICE_DEGRADED: "Local device packet loss",
    ISP_DEGRADED: "ISP packet loss",
}


@dataclass(frozen=True)
class ProbeResult:
    """Result of pinging one host once (a burst of packets)."""

    host: str
    sent: int
    received: int
    avg_rtt: float | None = None

    @property
    def alive(self) -> bool:
        return self.received > 0

    @property
    def loss(self) -> float | None:
        if self.sent <= 0:
            return None
        return 100.0 * (self.sent - self.received) / self.sent


@dataclass(frozen=True)
class HopResult:
    """The ISP first hop (traceroute hop 2) as seen in one check."""

    address: str | None  # None when no probe got a reply
    sent: int
    received: int

    @property
    def alive(self) -> bool:
        return self.received > 0


class LossSpan(NamedTuple):
    """The checks in a window that lost packets."""

    count: int
    first: float
    last: float


class LossWindow:
    """Pooled packet loss over a sliding time window."""

    def __init__(self, window_seconds: float) -> None:
        self._window = window_seconds
        self._samples: deque[tuple[float, int, int]] = deque()

    def add(self, ts: float, sent: int, received: int) -> None:
        if sent > 0:
            self._samples.append((ts, sent, received))

    def _prune(self, now: float) -> None:
        while self._samples and self._samples[0][0] < now - self._window:
            self._samples.popleft()

    def loss(self, now: float) -> float | None:
        """Percentage of packets lost in the window, or None without samples."""
        self._prune(now)
        sent = sum(s for _, s, _ in self._samples)
        if sent == 0:
            return None
        received = sum(r for _, _, r in self._samples)
        return round(100.0 * (sent - received) / sent, 2)

    def lossy(self, now: float) -> LossSpan | None:
        """How many checks in the window lost packets, and when; None if none did."""
        self._prune(now)
        times = [ts for ts, sent, received in self._samples if received < sent]
        return LossSpan(len(times), times[0], times[-1]) if times else None


@dataclass
class Snapshot:
    """Everything one check measured, plus the windowed loss figures."""

    router: ProbeResult
    devices: dict[str, ProbeResult]  # label -> result
    internet: list[ProbeResult]
    dns_ok: bool
    hop: HopResult | None  # None when traceroute is unavailable
    router_loss: float | None = None
    device_loss: dict[str, float | None] = field(default_factory=dict)
    internet_loss: float | None = None
    router_lossy: LossSpan | None = None
    device_lossy: dict[str, LossSpan | None] = field(default_factory=dict)
    internet_lossy: LossSpan | None = None

    @property
    def internet_alive(self) -> bool:
        return any(r.alive for r in self.internet)

    @property
    def best_internet(self) -> ProbeResult | None:
        """Target with the most replies (one target's own hiccup isn't ISP loss)."""
        if not self.internet:
            return None
        return max(self.internet, key=lambda r: (r.received, -(r.avg_rtt or 0)))


@dataclass(frozen=True)
class Verdict:
    issues: list[str]
    devices_down: list[str]
    devices_degraded: list[str]
    # Packet-loss issue -> when its lossy checks were (first, last)
    loss_spans: dict[str, LossSpan] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return self.issues[0] if self.issues else OK


def record_samples(
    snapshot: Snapshot,
    now: float,
    router_window: LossWindow,
    device_windows: dict[str, LossWindow],
    internet_window: LossWindow,
) -> None:
    """Add this check to the loss windows and fill in the windowed loss figures.

    Checks taken during a hard outage are left out of the affected window: the
    outage is already reported, and keeping them would show minutes of fake
    "packet loss" after it ends.
    """
    if snapshot.router.alive:
        router_window.add(now, snapshot.router.sent, snapshot.router.received)
        for label, result in snapshot.devices.items():
            if result.alive and label in device_windows:
                device_windows[label].add(now, result.sent, result.received)
        best = snapshot.best_internet
        if best is not None and best.alive:
            internet_window.add(now, best.sent, best.received)

    snapshot.router_loss = router_window.loss(now)
    snapshot.device_loss = {
        label: window.loss(now) for label, window in device_windows.items()
    }
    snapshot.internet_loss = internet_window.loss(now)
    snapshot.router_lossy = router_window.lossy(now)
    snapshot.device_lossy = {
        label: window.lossy(now) for label, window in device_windows.items()
    }
    snapshot.internet_lossy = internet_window.lossy(now)


def _repeated(span: LossSpan | None) -> bool:
    return span is not None and span.count >= MIN_LOSSY_CHECKS


def evaluate(snapshot: Snapshot, threshold: float) -> Verdict:
    """Locate the fault(s). threshold is in loss percentage points."""
    if not snapshot.router.alive:
        return Verdict([HOME_NETWORK], [], [])

    issues: list[str] = []
    internet = snapshot.internet_alive
    if not internet:
        if snapshot.hop is None:
            issues.append(ISP_DOWN)
        elif snapshot.hop.alive:
            issues.append(ISP_UPSTREAM)
        else:
            issues.append(ISP_LINE)
    elif not snapshot.dns_ok:
        issues.append(DNS)

    down = [label for label, r in snapshot.devices.items() if not r.alive]
    if down:
        issues.append(DEVICE_DOWN)

    spans: dict[str, LossSpan] = {}
    base = snapshot.router_loss or 0.0
    if base >= threshold and _repeated(snapshot.router_lossy):
        issues.append(HOME_DEGRADED)
        spans[HOME_DEGRADED] = snapshot.router_lossy

    degraded = [
        label
        for label, loss in snapshot.device_loss.items()
        if loss is not None
        and label not in down
        and loss - base >= threshold
        and _repeated(snapshot.device_lossy.get(label))
    ]
    if degraded:
        issues.append(DEVICE_DEGRADED)
        lossy = [snapshot.device_lossy[label] for label in degraded]
        spans[DEVICE_DEGRADED] = LossSpan(
            max(s.count for s in lossy), min(s.first for s in lossy), max(s.last for s in lossy)
        )

    if (
        internet
        and snapshot.internet_loss is not None
        and snapshot.internet_loss - base >= threshold
        and _repeated(snapshot.internet_lossy)
    ):
        issues.append(ISP_DEGRADED)
        spans[ISP_DEGRADED] = snapshot.internet_lossy

    issues.sort(key=ISSUES.index)
    return Verdict(issues, down, degraded, spans)


class IncidentTracker:
    """Tracks one incident: from the first issue until everything is OK again.

    Each issue keeps the first and last time it was active, so a report can say
    e.g. "ISP line: 4 min from 23:16". Packet-loss issues keep the times of their
    lossy checks instead: their verdict stays up while the loss is inside the
    window, which says nothing about when packets were actually lost.
    """

    def __init__(self) -> None:
        self.since: float | None = None
        self.issues: dict[str, list[float]] = {}  # code -> [first, last]
        self.active: list[str] = []

    def update(
        self,
        now: float,
        issues: list[str],
        loss_spans: dict[str, LossSpan] | None = None,
    ) -> tuple[str, dict] | None:
        """Feed the current issues. Returns ("started" | "resolved", incident) or None."""
        loss_spans = loss_spans or {}
        for code in self.active:
            if code not in DEGRADED_ISSUES:
                self.issues[code][1] = now
        started = self.since is None and bool(issues)
        if started:
            self.since = now
        for code in issues:
            span = loss_spans.get(code)
            if code in DEGRADED_ISSUES and span is not None:
                entry = self.issues.setdefault(code, [span.first, span.last])
                entry[0] = min(entry[0], span.first)
                entry[1] = max(entry[1], span.last)
            else:
                self.issues.setdefault(code, [now, now])
        self.active = list(issues)

        if started:
            return "started", self.summary(now)
        if not issues and self.since is not None:
            incident = self.summary(now)
            self.reset()
            return "resolved", incident
        return None

    def summary(self, now: float) -> dict:
        """The incident so far.

        ``start``/``end``/``duration`` cover when something was actually wrong
        (for packet loss: its first to last lossy check); ``active_duration`` is
        how long the verdict was up, which is what a minimum length applies to.
        """
        since = self.since if self.since is not None else now
        start = min([since, *(first for first, _ in self.issues.values())])
        end = max([start, *(last for _, last in self.issues.values())])
        return {
            "start": start,
            "end": end,
            "duration": round(end - start),
            "active_duration": round(now - since),
            "issues": [
                {
                    "issue": code,
                    "label": ISSUE_LABELS.get(code, code),
                    "start": first,
                    "end": last,
                    "duration": round(last - first),
                    "packet_loss": code in DEGRADED_ISSUES,
                }
                for code, (first, last) in sorted(
                    self.issues.items(), key=lambda item: item[1][0]
                )
            ],
        }

    def reset(self) -> None:
        self.since = None
        self.issues = {}
        self.active = []

    def as_dict(self) -> dict:
        return {"since": self.since, "issues": self.issues, "active": self.active}

    @classmethod
    def from_dict(cls, data: dict | None) -> IncidentTracker:
        tracker = cls()
        if not isinstance(data, dict):
            return tracker
        try:
            since = data.get("since")
            tracker.since = float(since) if since is not None else None
            tracker.issues = {
                k: [float(v[0]), float(v[1])]
                for k, v in (data.get("issues") or {}).items()
                if k in ISSUES
            }
            tracker.active = [c for c in data.get("active") or [] if c in tracker.issues]
        except (TypeError, ValueError, IndexError, AttributeError):
            return cls()  # corrupt storage: start clean
        return tracker


def parse_devices(entries: list[str]) -> dict[str, str]:
    """Parse "Name=host" or "host" entries into {label: host}."""
    result: dict[str, str] = {}
    for raw in entries:
        entry = raw.strip()
        if not entry:
            continue
        if "=" in entry:
            name, host = (part.strip() for part in entry.split("=", 1))
        else:
            name, host = entry, entry
        if host:
            result[name or host] = host
    return result
