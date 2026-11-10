# Network Fault Locator for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Validate](https://github.com/sagi-tab/ha-network-fault-locator/actions/workflows/validate.yml/badge.svg)](https://github.com/sagi-tab/ha-network-fault-locator/actions/workflows/validate.yml)

**"Is it my ISP or my Wi-Fi?"** This integration answers that question. It continuously probes
every link between Home Assistant and the internet and tells you *where* a failure or packet
loss starts: your home network, a local device you chose to watch (a mesh access point,
extender or switch), the ISP's line, the ISP's backbone, or DNS.

## How it works

Every check (default: every 30 s) Home Assistant probes the whole path in parallel:

```
Home Assistant ──► Router ──┬──► Local devices you pick (access points, switches, ..., optional)
                            └──► ISP first hop ──► Internet targets (1.1.1.1, 8.8.8.8) + DNS
```

| Probe | How |
|---|---|
| Router, local devices, internet targets | A burst of ICMP pings (default 10 per check) |
| ISP first hop | A 2-hop traceroute. Hop 2 is the ISP's first router. It is found dynamically on every check, so it keeps working when your ISP changes it |
| DNS | Resolves a hostname through Home Assistant's resolver |

A problem is blamed on the **first link where it appears**. Packet loss is pooled over a
sliding window (default 10 min) and loss to a local device or the internet only counts
**beyond the loss already seen to the router**, so trouble right next to Home Assistant is
never blamed on them. Checks taken during a hard outage are left out of the
loss windows, so an outage isn't followed by minutes of fake "packet loss".

A packet-loss verdict also needs loss in **at least two checks** within the window: one short
burst (a few packets lost in a single check) isn't reported. The incident report says when the
packets were lost (first to last lossy check), not how long the verdict stayed up.

### Verdicts

`sensor.<name>_status` shows the most severe current issue; the `issues` attribute lists all of them.

| State | Meaning |
|---|---|
| `ok` | All good |
| `home_network` | The router doesn't answer Home Assistant |
| `isp_line` | Router OK, but the ISP's first hop and the internet are unreachable (line / modem / ISP access) |
| `isp_upstream` | The ISP's first hop answers but the internet doesn't (ISP backbone) |
| `isp_down` | Internet unreachable and the first hop is unknown (traceroute unavailable) |
| `dns` | Internet reachable by IP, but name lookups fail |
| `device_down` | A watched local device doesn't answer (see `devices_down`) |
| `home_degraded` | Packet loss between Home Assistant and the router |
| `device_degraded` | A watched local device loses packets beyond the router's loss (see `devices_losing_packets`) |
| `isp_degraded` | The internet loses packets while the home network is clean |

## Installation

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/sagi-tab/ha-network-fault-locator`, category **Integration**.
2. Install **Network Fault Locator** and restart Home Assistant.
3. **Settings → Devices & services → Add integration → Network Fault Locator.**

### Manual

Copy `custom_components/network_fault_locator` into your `config/custom_components/` folder and restart.

## Configuration

**Step 1: network**

| Field | Description |
|---|---|
| Router | IP of your main router. Pre-filled with the first hop Home Assistant sees |
| Internet targets | Up to 5 public IPs that answer pings. Default `1.1.1.1` and `8.8.8.8`. The first one is also used for the traceroute |
| DNS test hostname | A name that should always resolve (default `google.com`) |
| Notify | Optional `notify` entity (Telegram, mobile app, …). Gets a summary when an incident ends |

**Step 2: local devices.** Pick which devices on your network to watch (up to 30, or none).
The list is built from what Home Assistant already knows: the host's neighbour (ARP) table,
with names from your Home Assistant devices where the MAC address matches, plus entities that
report an IP. **Nothing is scanned.** Devices that haven't talked to Home Assistant recently may
be missing; type them as `Name=IP` (e.g. `Living room=192.168.1.10`) or just the IP. Tip: give
watched devices fixed IPs (DHCP reservation).

Under **Configure** you can change all of the above and tune the pings per check, check
interval, loss window, loss threshold (default 2 percentage points) and minimum incident
length (default 60 s). The interval must leave room for the pings: an unreachable host takes
about 1 s per ping.

## Entities

One device per monitored network with:

- **Status** (enum, see above) with attributes `issues`, `devices_down`, `devices_losing_packets`, `isp_first_hop`, `incident_since`
- **Connectivity** binary sensors: Router, Internet, DNS, one per watched device
- **Packet loss** (windowed %): Router, Internet, ISP first hop (per check), one per watched device
- **Latency** (ms): Router, Internet, one per watched device
- **ISP first hop**: current address of the ISP's first router

## Events and notifications

| Event | When | Data |
|---|---|---|
| `network_fault_locator_incident_started` | First issue appears | `issues` |
| `network_fault_locator_incident_resolved` | Everything is OK again (after the minimum incident length) | `start`, `end`, `duration`, `issues` (each with `issue`, `label`, `start`, `end`, `duration`), `summary` |
| `network_fault_locator_isp_hop_changed` | The ISP first hop address changed | `old`, `new` |

All three also appear in the logbook. The notification (and the `summary` field) looks like:

```
• ISP line / access: 4 min from 23:16
• Local device packet loss: 16 min from 23:18
Total: 18 min (23:16–23:34)
```

The notification is sent after the incident, because during an internet outage it couldn't
get out. To react while an incident is ongoing (e.g. turn a light red), trigger on the
status sensor or the `incident_started` event.

## Dashboard card

The integration ships its own card. It's loaded automatically, so there's nothing else
to install. Add it from **Edit dashboard → Add card → Network Fault Locator**, or in YAML:

```yaml
type: custom:network-fault-locator-card
entity: sensor.network_fault_locator_status
hours_to_show: 24            # optional, default 24
show_device_latency: false   # optional, latency lines for your local devices
title: Network               # optional
```

Everything is in **one chart on one time axis**:

- a status strip on top, colored by the verdict;
- incidents shaded behind the lines in the color of the link that failed: home network
  (orange), local device (purple), ISP (red), DNS (blue); packet-loss incidents are lighter;
- **packet loss** of the router, the internet and each watched device as solid lines (left axis, %);
- **latency** as dashed lines in the same color as the matching link (right axis, ms);
- hover (or tap) for every value at that moment; click a legend entry to hide or show a line.

It only needs the Status sensor; the related sensors are found through the device.

## Requirements and limitations

- Home Assistant 2025.3 or newer. The card uses only the built-in frontend (no extra cards or libraries). Home Assistant OS / Container run as root, which the traceroute needs (raw sockets). Without it the integration still works but reports `isp_down` instead of telling `isp_line` and `isp_upstream` apart.
- Run Home Assistant **wired** to the router; otherwise its own Wi-Fi link becomes part of every measurement.
- Pinging a mesh node tests the link to it (wired or wireless backhaul) and the node itself, not the Wi-Fi between the node and its clients.
- The DNS check uses Home Assistant's resolver, which may cache answers for a few minutes.
- Some ISPs rate-limit replies from their first hop; that only matters when the internet is down, which is the only time the first hop is used for the verdict.

## Security & privacy

**What it sends.** ICMP echo requests to the router, your chosen devices and internet targets;
a 2-hop traceroute toward the first internet target; DNS lookups of the test hostname. With the
defaults that's ~10 small packets to each target every 30 s. **Privacy note:** the default
targets are Cloudflare (`1.1.1.1`) and Google (`8.8.8.8` and the `google.com` lookup), so they
see that your IP pings them. Replace them in the options if you prefer other targets.

**What it doesn't do.** It opens no ports, runs no shell commands (all probing goes through the
`icmplib` library that Home Assistant's own Ping integration uses, pinned to the same version),
contacts no cloud service of its own, and doesn't scan your network.

**What it stores.** The open incident and the last ISP first-hop address in
`.storage/network_fault_locator.<entry_id>`. Entity history goes to Home Assistant's recorder
like any sensor.

**What it shares.** Notifications contain only issue names and times, no addresses. The
`isp_hop_changed` event and logbook entry contain the ISP router's address. Diagnostics
downloads redact the ISP first hop and the notify target.

**Hardening in the code.**
- Hostnames are validated label by label (no regex backtracking: a single-regex check
  could otherwise freeze Home Assistant on a crafted ~40-character input).
- Multicast, broadcast, unspecified and reserved addresses are rejected: they would "answer"
  and hide real outages.
- At most 30 devices and 5 internet targets, and a check must fit in its interval, so the
  integration can't be turned into a traffic generator or pile up work.
- One unusable target only marks itself down; it can't break the whole check.
- Corrupt stored state is discarded instead of breaking setup.
- Raw sockets: traceroute needs them (Home Assistant OS/Container run as root). Received ICMP
  is parsed by `icmplib`; someone on your LAN could spoof replies and fake a result, but such an
  attacker could already disrupt your network directly.
- CI uses a read-only token and third-party actions pinned to commit SHAs.
- The dashboard card is a static file with no secrets, served like the rest of the frontend. It reads
  history through the logged-in user's own connection and escapes all names and states it displays.

Found a security problem? Please open a [private security advisory](https://github.com/sagi-tab/ha-network-fault-locator/security/advisories/new) instead of a public issue.

## Development

The fault-location logic lives in `analysis.py` and has no Home Assistant dependencies:

```bash
pip install pytest
pytest tests
```

## License

MIT
