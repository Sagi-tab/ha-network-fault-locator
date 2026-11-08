"""Pure fault-location logic (no Home Assistant imports, unit-testable).

Network path, as seen from Home Assistant:

    HA -> router -> access points (optional, mesh)
                 -> ISP first hop (hop 2, found by traceroute) -> internet targets (+ DNS)

A failure (or loss) is attributed to the first link on that path where it appears.
Loss to an access point or the internet only counts beyond the loss already seen to
the router, so a problem right next to Home Assistant is not blamed on the mesh or ISP.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

# Issue codes, most severe first. The status sensor shows the first active one.
HOME_NETWORK = "home_network"
ISP_LINE = "isp_line"
ISP_UPSTREAM = "isp_upstream"
ISP_DOWN = "isp_down"
DNS = "dns"
MESH_DOWN = "mesh_down"
HOME_DEGRADED = "home_degraded"
MESH_DEGRADED = "mesh_degraded"
ISP_DEGRADED = "isp_degraded"

ISSUES: tuple[str, ...] = (
    HOME_NETWORK,
    ISP_LINE,
    ISP_UPSTREAM,
    ISP_DOWN,
    DNS,
    MESH_DOWN,
    HOME_DEGRADED,
    MESH_DEGRADED,
    ISP_DEGRADED,
)
OK = "ok"

ISSUE_LABELS: dict[str, str] = {
    HOME_NETWORK: "Home network (router unreachable)",
    ISP_LINE: "ISP line / access",
    ISP_UPSTREAM: "ISP backbone",
    ISP_DOWN: "ISP / internet down",
    DNS: "DNS",
    MESH_DOWN: "Mesh - access point unreachable",
    HOME_DEGRADED: "Home network packet loss",
    MESH_DEGRADED: "Mesh - access point packet loss",
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


@dataclass
class Snapshot:
    """Everything one check measured, plus the windowed loss figures."""

    router: ProbeResult
    access_points: dict[str, ProbeResult]  # label -> result
    internet: list[ProbeResult]
    dns_ok: bool
    hop: HopResult | None  # None when traceroute is unavailable
    router_loss: float | None = None
    access_point_loss: dict[str, float | None] = field(default_factory=dict)
    internet_loss: float | None = None

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
    access_points_down: list[str]
    access_points_degraded: list[str]

    @property
    def status(self) -> str:
        return self.issues[0] if self.issues else OK


def record_samples(
    snapshot: Snapshot,
    now: float,
    router_window: LossWindow,
    ap_windows: dict[str, LossWindow],
    internet_window: LossWindow,
) -> None:
    """Add this check to the loss windows and fill in the windowed loss figures.

    Checks taken during a hard outage are left out of the affected window: the
    outage is already reported, and keeping them would show minutes of fake
    "packet loss" after it ends.
    """
    if snapshot.router.alive:
        router_window.add(now, snapshot.router.sent, snapshot.router.received)
        for label, result in snapshot.access_points.items():
            if result.alive and label in ap_windows:
                ap_windows[label].add(now, result.sent, result.received)
        best = snapshot.best_internet
        if best is not None and best.alive:
            internet_window.add(now, best.sent, best.received)

    snapshot.router_loss = router_window.loss(now)
    snapshot.access_point_loss = {
        label: window.loss(now) for label, window in ap_windows.items()
    }
    snapshot.internet_loss = internet_window.loss(now)


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

    down = [label for label, r in snapshot.access_points.items() if not r.alive]
    if down:
        issues.append(MESH_DOWN)

    base = snapshot.router_loss or 0.0
    if base >= threshold:
        issues.append(HOME_DEGRADED)

    degraded = [
        label
        for label, loss in snapshot.access_point_loss.items()
        if loss is not None and label not in down and loss - base >= threshold
    ]
    if degraded:
        issues.append(MESH_DEGRADED)

    if (
        internet
        and snapshot.internet_loss is not None
        and snapshot.internet_loss - base >= threshold
    ):
        issues.append(ISP_DEGRADED)

    issues.sort(key=ISSUES.index)
    return Verdict(issues, down, degraded)


class IncidentTracker:
    """Tracks one incident: from the first issue until everything is OK again.

    Each issue keeps the first and last time it was active, so a report can say
    e.g. "ISP line: 4 min from 23:16; mesh packet loss: 16 min from 23:18".
    """

    def __init__(self) -> None:
        self.since: float | None = None
        self.issues: dict[str, list[float]] = {}  # code -> [first, last]
        self.active: list[str] = []

    def update(self, now: float, issues: list[str]) -> tuple[str, dict] | None:
        """Feed the current issues. Returns ("started" | "resolved", incident) or None."""
        for code in self.active:
            self.issues[code][1] = now
        started = self.since is None and bool(issues)
        if started:
            self.since = now
        for code in issues:
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
        since = self.since if self.since is not None else now
        return {
            "start": since,
            "end": now,
            "duration": round(now - since),
            "issues": [
                {
                    "issue": code,
                    "label": ISSUE_LABELS.get(code, code),
                    "start": first,
                    "end": last,
                    "duration": round(last - first),
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
        if data:
            tracker.since = data.get("since")
            tracker.issues = {
                k: [float(v[0]), float(v[1])]
                for k, v in (data.get("issues") or {}).items()
                if k in ISSUES
            }
            tracker.active = [c for c in data.get("active") or [] if c in tracker.issues]
        return tracker


def parse_access_points(entries: list[str]) -> dict[str, str]:
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
