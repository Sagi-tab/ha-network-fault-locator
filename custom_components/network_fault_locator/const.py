"""Constants for Network Fault Locator."""

from typing import Final

DOMAIN: Final = "network_fault_locator"

CONF_ROUTER: Final = "router"
CONF_DEVICES: Final = "devices"
CONF_INTERNET_TARGETS: Final = "internet_targets"
CONF_DNS_HOST: Final = "dns_host"
CONF_NOTIFY_ENTITY: Final = "notify_entity"
CONF_PING_COUNT: Final = "ping_count"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_LOSS_WINDOW: Final = "loss_window"
CONF_LOSS_THRESHOLD: Final = "loss_threshold"
CONF_MIN_INCIDENT: Final = "min_incident"

DEFAULT_NAME: Final = "Network Fault Locator"
DEFAULT_INTERNET_TARGETS: Final = ["1.1.1.1", "8.8.8.8"]
DEFAULT_DNS_HOST: Final = "google.com"
DEFAULT_PING_COUNT: Final = 10
DEFAULT_SCAN_INTERVAL: Final = 30  # seconds
DEFAULT_LOSS_WINDOW: Final = 10  # minutes
DEFAULT_LOSS_THRESHOLD: Final = 2.0  # percentage points
DEFAULT_MIN_INCIDENT: Final = 60  # seconds

PING_INTERVAL: Final = 0.2  # seconds between packets of one burst
PING_TIMEOUT: Final = 1.0  # seconds to wait for each reply
TRACE_PROBES: Final = 5
DNS_TIMEOUT: Final = 5.0

# Limits that keep a check bounded and the integration from being a traffic generator
MAX_DEVICES: Final = 30
MAX_INTERNET_TARGETS: Final = 5
CHECK_MARGIN: Final = 3.0  # seconds a check needs on top of the ping bursts

EVENT_INCIDENT_STARTED: Final = f"{DOMAIN}_incident_started"
EVENT_INCIDENT_RESOLVED: Final = f"{DOMAIN}_incident_resolved"
EVENT_ISP_HOP_CHANGED: Final = f"{DOMAIN}_isp_hop_changed"

STORAGE_VERSION: Final = 1
