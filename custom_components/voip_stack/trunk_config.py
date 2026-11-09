"""Native HA records for independently configured SIP trunks."""

from .config import normalized_trunk_config
from .trunk_policy import validate_static_trunk_sources

TRUNK_SUBENTRY_TYPE = "trunk"
CONF_DEFAULT_TRUNK = "default_trunk_id"
TRUNK_DEVICE_MODEL = "SIP trunk"
TRUNK_UPDATE_SIGNAL = "voip_stack_trunk_updated"


def trunk_subentries(entry):
    return [item for item in entry.subentries.values() if item.subentry_type == TRUNK_SUBENTRY_TYPE]


def trunk_identity(record) -> str:
    """Return the stable identity stored on one trunk subentry."""
    return str(record.data.get("trunk_id") or record.subentry_id)


def entry_trunk_configs(entry) -> dict[str, dict]:
    records = trunk_subentries(entry)
    configs = {trunk_identity(item): normalized_trunk_config(item.data) for item in records}
    if len(configs) != len(records):
        raise ValueError("Trunk identities must be unique")
    validate_static_trunk_sources(configs)
    return configs


def entry_default_trunk(entry, configs) -> str:
    selected = str(entry.data.get(CONF_DEFAULT_TRUNK) or "")
    if CONF_DEFAULT_TRUNK in entry.data:
        return selected if selected in configs else ""
    return next(iter(configs)) if len(configs) == 1 else ""


def clear_removed_trunk_default(hass, entry, configs) -> None:
    """Forget a deleted provider without silently selecting a replacement."""
    selected = str(entry.data.get(CONF_DEFAULT_TRUNK) or "")
    if selected and selected not in configs:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DEFAULT_TRUNK: ""}
        )


def migrate_legacy_trunk(hass, entry) -> None:
    """Move the former parent-entry trunk into a stable native record once."""
    from uuid import uuid4
    from homeassistant.config_entries import ConfigSubentry

    if "trunk_enabled" not in entry.data:
        return
    configs = entry_trunk_configs(entry)
    selected = entry_default_trunk(entry, configs)
    current = next((item for item in trunk_subentries(entry)
                    if trunk_identity(item) == selected), None)
    config = normalized_trunk_config(entry.data)
    if config["trunk_enabled"] or current is not None:
        identity = selected if current is not None else uuid4().hex
        title = current.title if current is not None else "SIP trunk"
        data = {**config, "trunk_id": identity, "name": title}
        if current is None:
            hass.config_entries.async_add_subentry(entry, ConfigSubentry(
                subentry_type=TRUNK_SUBENTRY_TYPE, title=title, data=data, unique_id=f"trunk:{identity}",
            ))
        else:
            hass.config_entries.async_update_subentry(entry, current, data=data)
        selected = identity
    parent = {key: value for key, value in entry.data.items() if not key.startswith("trunk_")}
    if selected:
        parent[CONF_DEFAULT_TRUNK] = selected
    hass.config_entries.async_update_entry(entry, data=parent)


def sync_trunk_devices(hass, entry) -> None:
    """Expose stable named line devices for native HA selectors."""
    from homeassistant.helpers import device_registry as dr
    from .const import DOMAIN

    devices = dr.async_get(hass)
    for record in trunk_subentries(entry):
        identity = trunk_identity(record)
        devices.async_get_or_create(
            config_entry_id=entry.entry_id, config_subentry_id=record.subentry_id,
            identifiers={(DOMAIN, f"trunk:{identity}")}, name=record.title,
            manufacturer="VoIP Stack", model=TRUNK_DEVICE_MODEL,
            entry_type=dr.DeviceEntryType.SERVICE,
        )
