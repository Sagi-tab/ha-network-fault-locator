"""Network probes: ICMP bursts, 2-hop traceroute and DNS resolution."""

from __future__ import annotations

import asyncio
import logging
import socket

from icmplib import (
    ICMPLibError,
    NameLookupError,
    SocketPermissionError,
    async_multiping,
    async_ping,
    traceroute,
)

from .analysis import HopResult, ProbeResult
from .const import DNS_TIMEOUT, PING_INTERVAL, PING_TIMEOUT, TRACE_PROBES

_LOGGER = logging.getLogger(__name__)


async def async_detect_privileged() -> bool | None:
    """Return True for raw sockets, False for unprivileged ICMP, None if neither works."""
    for privileged in (True, False):
        try:
            await async_ping("127.0.0.1", count=0, timeout=0, privileged=privileged)
        except SocketPermissionError:
            continue
        return privileged
    return None


async def async_ping_hosts(
    hosts: list[str], count: int, privileged: bool
) -> dict[str, ProbeResult]:
    """Ping all hosts concurrently with a burst of `count` packets each."""
    unique = list(dict.fromkeys(hosts))
    results: dict[str, ProbeResult] = {}
    try:
        replies = await async_multiping(
            unique,
            count=count,
            interval=PING_INTERVAL,
            timeout=PING_TIMEOUT,
            privileged=privileged,
        )
        for host, reply in zip(unique, replies, strict=True):
            results[host] = ProbeResult(
                host=host,
                sent=reply.packets_sent,
                received=reply.packets_received,
                avg_rtt=reply.avg_rtt if reply.is_alive else None,
            )
    except ICMPLibError:
        # One bad target (unresolvable name, refused address, ...) must not take the
        # whole check down: probe one by one so the others still count
        singles = await asyncio.gather(
            *(_async_ping_one(host, count, privileged) for host in unique)
        )
        results = {r.host: r for r in singles}
    return results


async def _async_ping_one(host: str, count: int, privileged: bool) -> ProbeResult:
    try:
        reply = await async_ping(
            host,
            count=count,
            interval=PING_INTERVAL,
            timeout=PING_TIMEOUT,
            privileged=privileged,
        )
    except (NameLookupError, ICMPLibError, OSError):
        return ProbeResult(host=host, sent=count, received=0)
    return ProbeResult(
        host=host,
        sent=reply.packets_sent,
        received=reply.packets_received,
        avg_rtt=reply.avg_rtt if reply.is_alive else None,
    )


def trace_first_hops(target: str) -> tuple[str | None, HopResult]:
    """Blocking: traceroute to `target` limited to 2 hops.

    Returns (hop 1 address, hop 2 result). Hop 1 is normally the router and hop 2
    the ISP's first router, whatever its address is today. Raises
    SocketPermissionError when raw sockets are not available.
    """
    hops = traceroute(
        target,
        count=TRACE_PROBES,
        interval=0.05,
        timeout=PING_TIMEOUT,
        max_hops=2,
        fast=True,
    )
    by_distance = {hop.distance: hop for hop in hops}
    first = by_distance.get(1)
    second = by_distance.get(2)
    if second is None:
        return (first.address if first else None), HopResult(None, TRACE_PROBES, 0)
    return (first.address if first else None), HopResult(
        second.address, second.packets_sent, second.packets_received
    )


async def async_dns_ok(host: str) -> bool:
    """True when `host` resolves through the system resolver."""
    loop = asyncio.get_running_loop()
    try:
        async with asyncio.timeout(DNS_TIMEOUT):
            infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (TimeoutError, OSError):
        return False
    return bool(infos)


async def async_detect_router() -> str | None:
    """Best guess of the router: hop 1 of a traceroute to a public address."""
    loop = asyncio.get_running_loop()
    try:
        first, _ = await loop.run_in_executor(None, trace_first_hops, "1.1.1.1")
    except (ICMPLibError, OSError) as err:
        _LOGGER.debug("Router detection failed: %s", err)
        return None
    return first
