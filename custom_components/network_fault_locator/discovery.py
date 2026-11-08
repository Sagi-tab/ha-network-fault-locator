"""Passive discovery of local devices to offer in the config flow.

Nothing is sent on the network: this only reads the host's neighbour (ARP) table
and what Home Assistant already knows (device registry MAC addresses, entity
`ip` / `ip_address` attributes).
"""

from __future__ import annotations

import ipaddress
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

_LOGGER = logging.getLogger(__name__)
ARP_TABLE = "/proc/net/arp"
ATF_COM = 0x2  # ARP entry complete


def _private_ipv4(value: object) -> str | None:
    try:
        ip = ipaddress.IPv4Address(str(value).strip())
    except ValueError:
        return None
    if not ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
        return None
    return str(ip)


def read_arp_table(path: str = ARP_TABLE) -> dict[str, str]:
    """Blocking: {ip: mac} of complete entries in the kernel ARP table."""
    result: dict[str, str] = {}
    try:
        with open(path, encoding="ascii", errors="ignore") as arp:
            next(arp, None)  # header
            for line in arp:
                parts = line.split()
                if len(parts) < 4:
                    continue
                ip, flags, mac = _private_ipv4(parts[0]), parts[2], parts[3].lower()
                try:
                    complete = int(flags, 16) & ATF_COM
                except ValueError:
                    continue
                if ip and complete and mac != "00:00:00:00:00:00":
                    result[ip] = mac
    except OSError as err:
        _LOGGER.debug("Cannot read %s: %s", path, err)
    return result


async def async_discover_devices(hass: HomeAssistant, exclude: set[str]) -> dict[str, str]:
    """Return {value: label} for the device picker, sorted by IP.

    The value is stored as-is: "Name=IP" when a name is known, else just the IP.
    """
    arp = await hass.async_add_executor_job(read_arp_table)

    mac_names: dict[str, str] = {}
    for device in dr.async_get(hass).devices.values():
        name = device.name_by_user or device.name
        if not name:
            continue
        for conn_type, value in device.connections:
            if conn_type == dr.CONNECTION_NETWORK_MAC:
                mac_names[dr.format_mac(value)] = name

    ip_names: dict[str, str] = {}
    for state in hass.states.async_all():
        for attr in ("ip", "ip_address"):
            if ip := _private_ipv4(state.attributes.get(attr, "")):
                ip_names.setdefault(ip, state.name)

    found: dict[str, tuple[str | None, str | None]] = {}
    for ip, mac in arp.items():
        found[ip] = (mac_names.get(dr.format_mac(mac)) or ip_names.get(ip), mac)
    for ip, name in ip_names.items():
        found.setdefault(ip, (name, None))

    options: dict[str, str] = {}
    for ip in sorted(found, key=ipaddress.IPv4Address):
        if ip in exclude:
            continue
        name, mac = found[ip]
        # "=" separates name and host in stored values
        clean = name.replace("=", "-").strip() if name else None
        value = f"{clean}={ip}" if clean else ip
        options[value] = f"{clean} ({ip})" if clean else f"{ip} ({mac})" if mac else ip
    return options
