"""Config and options flow for Network Fault Locator.

Step 1 (network): router, internet targets, DNS test host, notification [, tuning].
Step 2 (devices): pick the local devices to watch from what Home Assistant already
knows about the network, and/or type extra ones as "Name=IP".
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .analysis import parse_devices
from .const import (
    CHECK_MARGIN,
    CONF_DEVICES,
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
    MAX_DEVICES,
    MAX_INTERNET_TARGETS,
    PING_TIMEOUT,
)
from .discovery import async_discover_devices
from .probe import async_detect_privileged, async_detect_router

# One DNS label. Validated label by label: a single regex over the whole name
# with nested quantifiers backtracks exponentially (ReDoS).
_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

TUNING_DEFAULTS: dict[str, Any] = {
    CONF_PING_COUNT: DEFAULT_PING_COUNT,
    CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
    CONF_LOSS_WINDOW: DEFAULT_LOSS_WINDOW,
    CONF_LOSS_THRESHOLD: DEFAULT_LOSS_THRESHOLD,
    CONF_MIN_INCIDENT: DEFAULT_MIN_INCIDENT,
}


def valid_host(host: str) -> bool:
    """A unicast IP address or a syntactically valid hostname."""
    host = host.strip()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if not host or len(host) > 253:
            return False
        labels = host.rstrip(".").split(".")
        # A name made only of digits and dots is a malformed IP, not a hostname
        if all(label.isdigit() for label in labels):
            return False
        return all(_LABEL.match(label) for label in labels)
    # Multicast / unspecified / broadcast "answer" pings without telling anything useful
    return not (
        ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
        or (ip.version == 4 and str(ip) == "255.255.255.255")
    )


def _clean_list(values: list[str] | None) -> list[str]:
    return list(dict.fromkeys(v.strip() for v in values or [] if v and v.strip()))


def _validate_network(user_input: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not valid_host(user_input.get(CONF_ROUTER, "")):
        errors[CONF_ROUTER] = "invalid_host"
    targets = _clean_list(user_input.get(CONF_INTERNET_TARGETS))
    if not targets:
        errors[CONF_INTERNET_TARGETS] = "no_targets"
    elif len(targets) > MAX_INTERNET_TARGETS:
        errors[CONF_INTERNET_TARGETS] = "too_many_targets"
    elif not all(valid_host(t) for t in targets):
        errors[CONF_INTERNET_TARGETS] = "invalid_host"
    if not valid_host(user_input.get(CONF_DNS_HOST, "")):
        errors[CONF_DNS_HOST] = "invalid_host"
    if CONF_PING_COUNT in user_input:
        # A dead host costs ~PING_TIMEOUT per packet; a check must fit in its interval
        worst = int(user_input[CONF_PING_COUNT]) * PING_TIMEOUT + CHECK_MARGIN
        if worst > float(user_input[CONF_SCAN_INTERVAL]):
            errors[CONF_SCAN_INTERVAL] = "check_too_long"
    return errors


def _validate_devices(devices: list[str]) -> dict[str, str]:
    if len(devices) > MAX_DEVICES:
        return {CONF_DEVICES: "too_many_devices"}
    parsed = parse_devices(devices)
    if len(parsed) != len(devices) or not all(valid_host(h) for h in parsed.values()):
        return {CONF_DEVICES: "invalid_device"}
    return {}


def _network_schema(defaults: dict[str, Any], with_name: bool) -> dict:
    schema: dict = {}
    if with_name:
        schema[vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME))] = (
            TextSelector()
        )
    schema.update(
        {
            vol.Required(CONF_ROUTER, default=defaults.get(CONF_ROUTER, "")): TextSelector(),
            vol.Required(
                CONF_INTERNET_TARGETS,
                default=defaults.get(CONF_INTERNET_TARGETS, DEFAULT_INTERNET_TARGETS),
            ): TextSelector(TextSelectorConfig(multiple=True)),
            vol.Required(
                CONF_DNS_HOST, default=defaults.get(CONF_DNS_HOST, DEFAULT_DNS_HOST)
            ): TextSelector(),
            # suggested_value (not default) so the field can be cleared in the UI
            vol.Optional(
                CONF_NOTIFY_ENTITY,
                description={"suggested_value": defaults.get(CONF_NOTIFY_ENTITY)},
            ): EntitySelector(EntitySelectorConfig(domain="notify")),
        }
    )
    return schema


def _tuning_schema(defaults: dict[str, Any]) -> dict:
    def number(key: str, lo: float, hi: float, step: float, unit: str) -> dict:
        return {
            vol.Required(key, default=defaults.get(key, TUNING_DEFAULTS[key])): NumberSelector(
                NumberSelectorConfig(
                    min=lo, max=hi, step=step, unit_of_measurement=unit,
                    mode=NumberSelectorMode.BOX,
                )
            )
        }

    return {
        **number(CONF_PING_COUNT, 3, 50, 1, "pings"),
        **number(CONF_SCAN_INTERVAL, 15, 600, 1, "s"),
        **number(CONF_LOSS_WINDOW, 2, 120, 1, "min"),
        **number(CONF_LOSS_THRESHOLD, 0.5, 50, 0.5, "%"),
        **number(CONF_MIN_INCIDENT, 0, 3600, 1, "s"),
    }


async def _devices_schema(
    hass: HomeAssistant, router: str, selected: list[str]
) -> vol.Schema:
    """Picker of discovered devices; configured ones stay listed, new ones can be typed."""
    selected_hosts = set(parse_devices(selected).values())
    discovered = await async_discover_devices(hass, exclude={router, *selected_hosts})
    options = [
        SelectOptionDict(value=v, label=f"{name} ({host})" if name != host else host)
        for v in selected
        for name, host in parse_devices([v]).items()
    ]
    options += [SelectOptionDict(value=v, label=label) for v, label in discovered.items()]
    return vol.Schema(
        {
            vol.Optional(CONF_DEVICES, default=selected): SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    multiple=True,
                    custom_value=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        }
    )


class NetworkFaultLocatorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up one monitored network."""

    VERSION = 1

    def __init__(self) -> None:
        self._network: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is None:
            if await async_detect_privileged() is None:
                return self.async_abort(reason="no_icmp")
            defaults: dict[str, Any] = {CONF_ROUTER: await async_detect_router() or ""}
        else:
            defaults = user_input
            errors = _validate_network(user_input)
            if not errors:
                self._network = {
                    **user_input,
                    CONF_ROUTER: user_input[CONF_ROUTER].strip(),
                    CONF_DNS_HOST: user_input[CONF_DNS_HOST].strip(),
                    CONF_INTERNET_TARGETS: _clean_list(user_input[CONF_INTERNET_TARGETS]),
                }
                if not self._network.get(CONF_NOTIFY_ENTITY):
                    self._network.pop(CONF_NOTIFY_ENTITY, None)
                await self.async_set_unique_id(self._network[CONF_ROUTER])
                self._abort_if_unique_id_configured()
                return await self.async_step_devices()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(_network_schema(defaults, with_name=True)),
            errors=errors,
        )

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        selected: list[str] = []
        if user_input is not None:
            selected = _clean_list(user_input.get(CONF_DEVICES))
            errors = _validate_devices(selected)
            if not errors:
                options = dict(self._network)
                name = options.pop(CONF_NAME, DEFAULT_NAME) or DEFAULT_NAME
                return self.async_create_entry(
                    title=name,
                    data={CONF_NAME: name},
                    options={**options, CONF_DEVICES: selected, **TUNING_DEFAULTS},
                )
        return self.async_show_form(
            step_id="devices",
            data_schema=await _devices_schema(self.hass, self._network[CONF_ROUTER], selected),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> NetworkFaultLocatorOptionsFlow:
        return NetworkFaultLocatorOptionsFlow()


class NetworkFaultLocatorOptionsFlow(OptionsFlow):
    """Change targets, notification, tuning and the watched devices."""

    def __init__(self) -> None:
        self._network: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current = dict(self.config_entry.options)
        if user_input is not None:
            errors = _validate_network(user_input)
            if not errors:
                self._network = {
                    **user_input,
                    CONF_ROUTER: user_input[CONF_ROUTER].strip(),
                    CONF_DNS_HOST: user_input[CONF_DNS_HOST].strip(),
                    CONF_INTERNET_TARGETS: _clean_list(user_input[CONF_INTERNET_TARGETS]),
                    CONF_PING_COUNT: int(user_input[CONF_PING_COUNT]),
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_MIN_INCIDENT: int(user_input[CONF_MIN_INCIDENT]),
                }
                if not self._network.get(CONF_NOTIFY_ENTITY):
                    self._network.pop(CONF_NOTIFY_ENTITY, None)
                return await self.async_step_devices()
            current = user_input
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {**_network_schema(current, with_name=False), **_tuning_schema(current)}
            ),
            errors=errors,
        )

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        selected: list[str] = list(self.config_entry.options.get(CONF_DEVICES, []))
        if user_input is not None:
            selected = _clean_list(user_input.get(CONF_DEVICES))
            errors = _validate_devices(selected)
            if not errors:
                return self.async_create_entry(data={**self._network, CONF_DEVICES: selected})
        return self.async_show_form(
            step_id="devices",
            data_schema=await _devices_schema(self.hass, self._network[CONF_ROUTER], selected),
            errors=errors,
        )
