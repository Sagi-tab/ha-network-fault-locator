# Network Fault Locator for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Validate](https://github.com/sagi-tab/ha-network-fault-locator/actions/workflows/validate.yml/badge.svg)](https://github.com/sagi-tab/ha-network-fault-locator/actions/workflows/validate.yml)

**"Is it my ISP or my Wi-Fi?"** This integration answers that question. It continuously probes
every link between Home Assistant and the internet and tells you *where* a failure or packet
loss starts: your home network, a mesh access point, the ISP's line, the ISP's backbone,
or DNS.

## How it works

Every check (default: every 30 s) Home Assistant probes the whole path in parallel:

```
Home Assistant ──► Router ──┬──► Access point(s) (mesh nodes, optional)
                            └──► ISP first hop ──► Internet targets (1.1.1.1, 8.8.8.8) + DNS
```

| Probe | How |
|---|---|
| Router, access points, internet targets | A burst of ICMP pings (default 10 per check) |
| ISP first hop | A 2-hop traceroute. Hop 2 is the ISP's first router. It is found dynamically on every check, so it keeps working when your ISP changes it |
| DNS | Resolves a hostname through Home Assistant's resolver |

A problem is blamed on the **first link where it appears**. Packet loss is pooled over a
sliding window (default 10 min) and loss to an access point or the internet only counts
**beyond the loss already seen to the router**, so trouble right next to Home Assistant is
never blamed on the mesh or the ISP. Checks taken during a hard outage are left out of the
loss windows, so an outage isn't followed by minutes of fake "packet loss".

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
| `mesh_down` | An access point doesn't answer (see `access_points_down`) |
| `home_degraded` | Packet loss between Home Assistant and the router |
| `mesh_degraded` | An access point loses packets beyond the router's loss (see `access_points_losing_packets`) |
| `isp_degraded` | The internet loses packets while the home network is clean |

## Installation

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/sagi-tab/ha-network-fault-locator`, category **Integration**.
2. Install **Network Fault Locator** and restart Home Assistant.
3. **Settings → Devices & services → Add integration → Network Fault Locator.**

### Manual

Copy `custom_components/network_fault_locator` into your `config/custom_components/` folder and restart.

## Configuration

| Field | Description |
|---|---|
| Router | IP of your main router. Pre-filled with the first hop Home Assistant sees |
| Access points | Optional. One per line, as `Name=IP` (e.g. `Living room=192.168.1.10`) or just the IP. Tip: give them fixed IPs (DHCP reservation) |
| Internet targets | Public IPs that answer pings. The first one is also used for the traceroute |
| DNS test hostname | A name that should always resolve (default `google.com`) |
| Notify | Optional `notify` entity (Telegram, mobile app, …). Gets a summary when an incident ends |

Under **Configure** you can also tune the pings per check, check interval, loss window,
loss threshold (default 2 percentage points) and minimum incident length (default 60 s).

## Entities

One device per monitored network with:

- **Status** (enum, see above) with attributes `issues`, `access_points_down`, `access_points_losing_packets`, `isp_first_hop`, `incident_since`
- **Connectivity** binary sensors: Router, Internet, DNS, one per access point
- **Packet loss** (windowed %): Router, Internet, ISP first hop (per check), one per access point
- **Latency** (ms): Router, Internet, one per access point
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
• Mesh - access point packet loss: 16 min from 23:18
Total: 18 min (23:16–23:34)
```

The notification is sent after the incident, because during an internet outage it couldn't
get out. To react while an incident is ongoing (e.g. turn a light red), trigger on the
status sensor or the `incident_started` event.

## Dashboard example

```yaml
type: history-graph
title: Network
hours_to_show: 24
entities:
  - entity: sensor.network_fault_locator_status
  - entity: sensor.network_fault_locator_router_packet_loss
  - entity: sensor.network_fault_locator_internet_packet_loss
  - entity: sensor.network_fault_locator_router_latency
  - entity: sensor.network_fault_locator_internet_latency
```

## Requirements and limitations

- Home Assistant 2025.3 or newer. Home Assistant OS / Container run as root, which the traceroute needs (raw sockets). Without it the integration still works but reports `isp_down` instead of telling `isp_line` and `isp_upstream` apart.
- Run Home Assistant **wired** to the router; otherwise its own Wi-Fi link becomes part of every measurement.
- Pinging an access point tests the link to it (wired or wireless backhaul) and the node itself, not the Wi-Fi between the node and its clients.
- The DNS check uses Home Assistant's resolver, which may cache answers for a few minutes.
- Some ISPs rate-limit replies from their first hop; that only matters when the internet is down, which is the only time the first hop is used for the verdict.

## Development

The fault-location logic lives in `analysis.py` and has no Home Assistant dependencies:

```bash
pip install pytest
pytest tests
```

## License

MIT
