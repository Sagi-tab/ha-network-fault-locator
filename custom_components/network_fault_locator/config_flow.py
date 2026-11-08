"""Config and options flow for Network Fault Locator."""

from __future__ import annotations

import ipaddress
import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .analysis import parse_access_points
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
)
from .probe import async_detect_privileged, async_detect_router

_HOSTNAME = re.compile(r"^(?=.{1,253}$)([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.?)+$")


def _valid_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return bool(_HOSTNAME.match(host))
    return True


def _validate(user_input: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not _valid_host(user_input.get(CONF_ROUTER, "").strip()):
        errors[CONF_ROUTER] = "invalid_host"
    aps = user_input.get(CONF_ACCESS_POINTS) or []
    if any(not _valid_host(h) for h in parse_access_points(aps).values()):
        errors[CONF_ACCESS_POINTS] = "invalid_access_point"
    targets = [t.strip() for t in user_input.get(CONF_INTERNET_TARGETS) or [] if t.strip()]
    if not targets:
        errors[CONF_INTERNET_TARGETS] = "no_targets"
    elif any(not _valid_host(t) for t in targets):
        errors[CONF_INTERNET_TARGETS] = "invalid_host"
    if not _valid_host(user_input.get(CONF_DNS_HOST, "").strip()):
        errors[CONF_DNS_HOST] = "invalid_host"
    return errors


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    data = dict(user_input)
    data[CONF_ROUTER] = data[CONF_ROUTER].strip()
    data[CONF_DNS_HOST] = data[CONF_DNS_HOST].strip()
    data[CONF_ACCESS_POINTS] = [a.strip() for a in data.get(CONF_ACCESS_POINTS) or [] if a.strip()]
    data[CONF_INTERNET_TARGETS] = [
        t.strip() for t in data.get(CONF_INTERNET_TARGETS) or [] if t.strip()
    ]
    if not data.get(CONF_NOTIFY_ENTITY):
        data.pop(CONF_NOTIFY_ENTITY, None)
    for key in (CONF_PING_COUNT, CONF_SCAN_INTERVAL, CONF_MIN_INCIDENT):
        if key in data:
            data[key] = int(data[key])
    return data


def _network_schema(defaults: dict[str, Any]) -> dict:
    notify_default = defaults.get(CONF_NOTIFY_ENTITY)
    return {
        vol.Required(CONF_ROUTER, default=defaults.get(CONF_ROUTER, "")): TextSelector(),
        vol.Optional(
            CONF_ACCESS_POINTS, default=defaults.get(CONF_ACCESS_POINTS, [])
        ): TextSelector(TextSelectorConfig(multiple=True)),
        vol.Required(
            CONF_INTERNET_TARGETS,
            default=defaults.get(CONF_INTERNET_TARGETS, DEFAULT_INTERNET_TARGETS),
        ): TextSelector(TextSelectorConfig(multiple=True)),
        vol.Required(
            CONF_DNS_HOST, default=defaults.get(CONF_DNS_HOST, DEFAULT_DNS_HOST)
        ): TextSelector(),
        # suggested_value (not default) so the field can be cleared in the UI
        vol.Optional(
            CONF_NOTIFY_ENTITY, description={"suggested_value": notify_default}
        ): EntitySelector(EntitySelectorConfig(domain="notify")),
    }


def _tuning_schema(defaults: dict[str, Any]) -> dict:
    def number(key: str, default: float, lo: float, hi: float, step: float, unit: str):
        return {
            vol.Required(key, default=defaults.get(key, default)): NumberSelector(
                NumberSelectorConfig(
                    min=lo, max=hi, step=step, unit_of_measurement=unit,
                    mode=NumberSelectorMode.BOX,
                )
            )
        }

    return {
        **number(CONF_PING_COUNT, DEFAULT_PING_COUNT, 3, 50, 1, "pings"),
        **number(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, 15, 600, 1, "s"),
        **number(CONF_LOSS_WINDOW, DEFAULT_LOSS_WINDOW, 2, 120, 1, "min"),
        **number(CONF_LOSS_THRESHOLD, DEFAULT_LOSS_THRESHOLD, 0.5, 50, 0.5, "%"),
        **number(CONF_MIN_INCIDENT, DEFAULT_MIN_INCIDENT, 0, 3600, 1, "s"),
    }


class NetworkFaultLocatorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up one monitored network."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is None:
            if await async_detect_privileged() is None:
                return self.async_abort(reason="no_icmp")
            defaults = {CONF_ROUTER: await async_detect_router() or ""}
        else:
            defaults = user_input
            errors = _validate(user_input)
            if not errors:
                data = _clean(user_input)
                await self.async_set_unique_id(data[CONF_ROUTER])
                self._abort_if_unique_id_configured()
                name = data.pop(CONF_NAME, DEFAULT_NAME) or DEFAULT_NAME
                return self.async_create_entry(
                    title=name,
                    data={CONF_NAME: name},
                    options={
                        **data,
                        CONF_PING_COUNT: DEFAULT_PING_COUNT,
                        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                        CONF_LOSS_WINDOW: DEFAULT_LOSS_WINDOW,
                        CONF_LOSS_THRESHOLD: DEFAULT_LOSS_THRESHOLD,
                        CONF_MIN_INCIDENT: DEFAULT_MIN_INCIDENT,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME)): TextSelector(),
                **_network_schema(defaults),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> NetworkFaultLocatorOptionsFlow:
        return NetworkFaultLocatorOptionsFlow()


class NetworkFaultLocatorOptionsFlow(OptionsFlow):
    """Change targets, notification and tuning."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current = dict(self.config_entry.options)
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                return self.async_create_entry(data=_clean(user_input))
            current = user_input
        schema = vol.Schema({**_network_schema(current), **_tuning_schema(current)})
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
