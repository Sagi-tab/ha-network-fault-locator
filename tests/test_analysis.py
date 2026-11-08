"""Tests for the pure fault-location logic (no Home Assistant needed)."""

import importlib.util
from pathlib import Path
import sys

_PATH = Path(__file__).parents[1] / "custom_components/network_fault_locator/analysis.py"
_spec = importlib.util.spec_from_file_location("nfl_analysis", _PATH)
a = importlib.util.module_from_spec(_spec)
sys.modules["nfl_analysis"] = a
_spec.loader.exec_module(a)

T = 2.0  # loss threshold, percentage points


def up(host="h", sent=10, received=10, rtt=1.0):
    return a.ProbeResult(host, sent, received, rtt if received else None)


def down(host="h", sent=10):
    return a.ProbeResult(host, sent, 0, None)


def snap(router=None, aps=None, internet=None, dns=True, hop=None, **loss):
    s = a.Snapshot(
        router=router or up("router"),
        access_points=aps if aps is not None else {"ap1": up("ap1"), "ap2": up("ap2")},
        internet=internet if internet is not None else [up("1.1.1.1"), up("8.8.8.8")],
        dns_ok=dns,
        hop=hop if hop is not None else a.HopResult("isp", 5, 5),
    )
    s.router_loss = loss.get("router_loss", 0.0)
    s.access_point_loss = loss.get("ap_loss", {"ap1": 0.0, "ap2": 0.0})
    s.internet_loss = loss.get("internet_loss", 0.0)
    return s


def issues(s):
    return a.evaluate(s, T).issues


def test_all_ok():
    v = a.evaluate(snap(), T)
    assert v.issues == [] and v.status == a.OK


def test_router_down_masks_everything():
    s = snap(router=down("router"), internet=[down(), down()], dns=False)
    assert issues(s) == [a.HOME_NETWORK]


def test_isp_line_vs_upstream_vs_unknown():
    dead = [down(), down()]
    assert issues(snap(internet=dead, hop=a.HopResult(None, 5, 0))) == [a.ISP_LINE]
    assert issues(snap(internet=dead, hop=a.HopResult("isp", 5, 3))) == [a.ISP_UPSTREAM]
    s = snap(internet=dead)
    s.hop = None  # traceroute unavailable
    assert issues(s) == [a.ISP_DOWN]


def test_one_internet_target_down_is_not_an_outage():
    assert issues(snap(internet=[down(), up()])) == []


def test_dns_only_when_internet_works():
    assert issues(snap(dns=False)) == [a.DNS]
    assert issues(snap(dns=False, internet=[down(), down()], hop=a.HopResult(None, 5, 0))) == [a.ISP_LINE]


def test_mesh_down_and_degraded():
    v = a.evaluate(snap(aps={"ap1": down("ap1"), "ap2": up("ap2")}), T)
    assert v.issues == [a.MESH_DOWN] and v.access_points_down == ["ap1"]
    v = a.evaluate(snap(ap_loss={"ap1": 0.0, "ap2": 5.0}), T)
    assert v.issues == [a.MESH_DEGRADED] and v.access_points_degraded == ["ap2"]


def test_loss_is_relative_to_router():
    # Loss near Home Assistant is not blamed on the mesh or the ISP
    s = snap(router_loss=4.0, ap_loss={"ap1": 5.0, "ap2": 4.0}, internet_loss=5.0)
    assert issues(s) == [a.HOME_DEGRADED]
    s = snap(router_loss=4.0, ap_loss={"ap1": 7.0, "ap2": 4.0}, internet_loss=4.0)
    assert issues(s) == [a.HOME_DEGRADED, a.MESH_DEGRADED]


def test_isp_degraded_and_ordering():
    s = snap(internet_loss=3.0, aps={"ap1": down("ap1"), "ap2": up("ap2")}, dns=False)
    assert issues(s) == [a.DNS, a.MESH_DOWN, a.ISP_DEGRADED]


def test_loss_window_pools_and_expires():
    w = a.LossWindow(600)
    w.add(0, 10, 10)
    w.add(30, 10, 5)
    assert w.loss(30) == 25.0
    assert w.loss(700) is None  # both samples expired
    w.add(700, 0, 0)  # empty bursts are ignored
    assert w.loss(700) is None


def test_record_samples_skips_outage_checks():
    rw, iw = a.LossWindow(600), a.LossWindow(600)
    aw = {"ap1": a.LossWindow(600), "ap2": a.LossWindow(600)}
    a.record_samples(snap(), 0, rw, aw, iw)
    # Internet outage: its samples must not pollute the internet loss window
    a.record_samples(snap(internet=[down(), down()]), 30, rw, aw, iw)
    assert iw.loss(30) == 0.0
    # Access point down: same for its window
    s = snap(aps={"ap1": down("ap1"), "ap2": up("ap2")})
    a.record_samples(s, 60, rw, aw, iw)
    assert s.access_point_loss == {"ap1": 0.0, "ap2": 0.0}
    # Partial loss is recorded, using the best internet target
    s = snap(internet=[up(received=6), up(received=8)])
    a.record_samples(s, 90, rw, aw, iw)
    assert s.internet_loss == round(100 * 2 / 30, 2)


def test_incident_tracker_per_issue_times():
    t = a.IncidentTracker()
    assert t.update(0, []) is None
    kind, inc = t.update(100, [a.ISP_LINE])
    assert kind == "started" and inc["issues"][0]["issue"] == a.ISP_LINE
    assert t.update(130, [a.ISP_LINE, a.MESH_DEGRADED]) is None
    assert t.update(340, [a.MESH_DEGRADED]) is None
    kind, inc = t.update(1000, [])
    assert kind == "resolved"
    assert inc["duration"] == 900
    by_issue = {i["issue"]: i for i in inc["issues"]}
    assert (by_issue[a.ISP_LINE]["start"], by_issue[a.ISP_LINE]["end"]) == (100, 340)
    assert (by_issue[a.MESH_DEGRADED]["start"], by_issue[a.MESH_DEGRADED]["end"]) == (130, 1000)
    assert t.since is None and t.update(1030, []) is None


def test_incident_tracker_round_trip():
    t = a.IncidentTracker()
    t.update(10, [a.DNS])
    t2 = a.IncidentTracker.from_dict(t.as_dict())
    kind, inc = t2.update(70, [])
    assert kind == "resolved" and inc["duration"] == 60
    assert a.IncidentTracker.from_dict({"issues": {"bogus": [1, 2]}}).issues == {}


def test_parse_access_points():
    assert a.parse_access_points(["Living room=192.168.1.10", " 192.168.1.11 ", "", "=x"]) == {
        "Living room": "192.168.1.10",
        "192.168.1.11": "192.168.1.11",
        "x": "x",
    }
