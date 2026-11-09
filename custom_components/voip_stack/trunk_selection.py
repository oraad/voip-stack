"""Resolve HA trunk selectors and call defaults without changing registration."""

from dataclasses import dataclass, field

from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr

from .config import normalized_trunk_config, trunk_config, trunk_enabled
from .const import DOMAIN
from .runtime_data import require_runtime_data, sip_trunk
from .store import config_entry
from .trunk_config import trunk_identity, trunk_subentries
from .trunk_policy import trunk_available


@dataclass(frozen=True, slots=True)
class TrunkSelection:
    identity: str
    config: dict = field(repr=False)
    client: object = field(repr=False)

    @property
    def available(self) -> bool:
        return trunk_enabled(self.config) and trunk_available(self.client)

    def unavailable_error(self):
        """Explain a failed external route without blocking ordinary local calls."""
        from .service_errors import service_error

        if not self.identity and not self.config.get("trunk_server"):
            return service_error(
                "No outgoing SIP trunk selected. Choose a line in the phone options or set a default trunk.",
                "outgoing_trunk_not_selected",
            )
        return service_error("The selected SIP trunk is unavailable", "outgoing_trunk_unavailable")


def resolve_trunk_selector(hass, value: str) -> str:
    """Accept a native Device selector, stable identity or unique configured name."""
    runtime = require_runtime_data(hass)
    value = str(value or "").strip()
    if not value:
        return ""
    if value in runtime.trunk_configs:
        return value
    device = dr.async_get(hass).async_get(value)
    if device is not None:
        for domain, identifier in device.identifiers:
            if domain == DOMAIN and identifier.startswith("trunk:") and identifier[6:] in runtime.trunk_configs:
                return identifier[6:]
    entry = config_entry(hass)
    matches = [trunk_identity(item)
               for item in trunk_subentries(entry) if item.title.casefold() == value.casefold()] if entry else []
    if len(matches) == 1:
        return matches[0]
    raise ServiceValidationError("Select an existing SIP trunk")


def trunk_display_name(hass, identity: str) -> str:
    """Return the current subentry title for one stable trunk identity."""
    entry = config_entry(hass)
    if entry is None or not identity:
        return ""
    for record in trunk_subentries(entry):
        if trunk_identity(record) == identity:
            return str(record.title or "")
    return ""


def add_trunk_names(hass, payload: dict) -> None:
    """Attach current display names without changing stable trunk identities."""
    for direction in ("inbound", "outbound"):
        identity = str(payload.get(f"{direction}_trunk_id") or "").strip()
        if not identity or payload.get(f"{direction}_trunk_name"):
            continue
        name = trunk_display_name(hass, identity)
        if name:
            payload[f"{direction}_trunk_name"] = name


def select_call_trunk(hass, *, requested: str = "", endpoint_id: str = "", inherited: str = "") -> TrunkSelection:
    runtime = require_runtime_data(hass)
    endpoint = runtime.endpoints.get(endpoint_id) if endpoint_id else None
    preference = endpoint.policy.outbound_trunk_id if endpoint is not None else ""
    identity = resolve_trunk_selector(hass, requested) if requested else inherited or preference or runtime.default_trunk_id
    if runtime.trunk_configs:
        config = runtime.trunk_configs.get(identity, normalized_trunk_config({}))
        return TrunkSelection(identity, config, sip_trunk(hass, identity))
    if identity:
        return TrunkSelection(identity, normalized_trunk_config({}), None)
    return TrunkSelection("", trunk_config(hass), sip_trunk(hass))
