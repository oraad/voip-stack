"""Read-only phone projections shared by HA actions and state observers."""

from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace
import logging

from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError

from .dialog_info import snapshot_all
from .endpoint_registry import EndpointRegistryEvent, EndpointRegistryEventType
from .runtime_data import require_runtime_data

_LOGGER = logging.getLogger(__name__)

PHONE_STATE_CHANGED = "voip_stack.phone_state_changed"


def resolve_phone(hass, selector):
    directory = require_runtime_data(hass).endpoints
    endpoint = directory.by_device_id(selector) or directory.resolve(selector)
    if endpoint is None:
        raise ServiceValidationError("Select an existing phone")
    return endpoint


def mailbox_state(hass, endpoint_id, archive=None):
    from .mailbox import mailbox_settings

    enabled = bool(
        mailbox_settings(hass, endpoint_id).get("enabled")
    )
    runtime = require_runtime_data(hass)
    if archive is None and runtime.sip is not None:
        archive = runtime.sip.component("media_archive")
    new, old = (
        archive.counts(endpoint_id) if enabled and archive is not None else (0, 0)
    )
    return {"enabled": enabled, "waiting": bool(new), "new": new, "old": old}


def phone_states(hass, *, dialogs=None):
    """Build one detached projection, without changing call ownership."""
    runtime = require_runtime_data(hass)
    dialogs = snapshot_all(runtime) if dialogs is None else dialogs
    result = {}
    for endpoint in runtime.endpoints.endpoints:
        states = tuple(
            item
            for item in dialogs.get(endpoint.endpoint_id, ())
            if item.state != "terminated"
        )
        ringing = any(
            item.state == "early" and item.direction == "recipient" for item in states
        )
        confirmed = tuple(item for item in states if item.state == "confirmed")
        state = (
            "ringing"
            if ringing
            else "held"
            if confirmed and all(item.held for item in confirmed)
            else "in_call"
            if confirmed
            else "calling"
            if states
            else "idle"
            if endpoint.availability.value == "available"
            else "unavailable"
        )
        calls = []
        for item in states:
            value = asdict(item)
            value["sip_call_id"] = value.pop("call_id")
            value["call_id"] = value.pop("session_id")
            value["pickup_available"] = (
                item.state == "early" and item.direction == "recipient"
            )
            calls.append(value)
        result[endpoint.endpoint_id] = {
            "schema_version": 1,
            "endpoint_id": endpoint.endpoint_id,
            "device_id": endpoint.device_id,
            "name": endpoint.name,
            "extension": endpoint.extension,
            "kind": endpoint.kind.value,
            "availability": endpoint.availability.value,
            "dnd": endpoint.dnd,
            "state": state,
            "calls": calls,
            "mailbox": mailbox_state(hass, endpoint.endpoint_id),
        }
    return result


async def async_get_phone_state(call):
    from .authorization import async_require_service_endpoint_read

    endpoint = resolve_phone(call.hass, call.data["device_id"])
    await async_require_service_endpoint_read(call.hass, call, endpoint)
    return phone_states(call.hass)[endpoint.endpoint_id]


class PhoneStateObserver:
    """One runtime-owned projection feeds HA and SIP without per-card scans."""

    def __init__(self, hass):
        self.hass = hass
        self.listeners = set()
        self.activity_listeners = set()
        self.dialog_listeners = set()
        self.unsubscribers = []
        self.previous = {}
        self.dialogs = {}
        self.activity = {"phones": [], "parked": []}

    def _start(self):
        from .mailbox import EVENT_VOICEMAIL_CHANGED
        from .parking import PARKING_CHANGED
        from .dialog_info import activity_snapshot

        if self.unsubscribers:
            return
        runtime = require_runtime_data(self.hass)
        self.dialogs = snapshot_all(runtime)
        self.previous = phone_states(self.hass, dialogs=self.dialogs)
        self.activity = activity_snapshot(runtime, dialogs=self.dialogs)
        self.unsubscribers = [
            self.hass.bus.async_listen(name, self.refresh)
            for name in (EVENT_VOICEMAIL_CHANGED, PARKING_CHANGED)
        ]
        self.unsubscribers.append(runtime.sip.subscribe_state(self._committed))
        self.unsubscribers.append(runtime.endpoints.subscribe(self._endpoint_changed))

    @callback
    def _committed(self, snapshot):
        runtime = require_runtime_data(self.hass)
        self.refresh(SimpleNamespace(
            event_type="call_commit",
            data=self._call_fields(runtime.sip, snapshot),
            context=runtime.sip.ha_context(snapshot.call_id),
        ))

    def _call_fields(self, registry, snapshot):
        fields = {"call_id": snapshot.call_id, "generation": snapshot.generation,
                  "sequence": snapshot.sequence, "revision": snapshot.revision,
                  "state": snapshot.state, "caller": snapshot.caller,
                  "callee": snapshot.callee, "route_kind": snapshot.route_kind,
                  "event_type": "state_changed", "scope": "call_commit",
                  **registry.event_fields(snapshot.call_id, snapshot.state)}
        from .trunk_events import add_trunk_names

        add_trunk_names(self.hass, fields)
        return fields

    @callback
    def _endpoint_changed(self, event: EndpointRegistryEvent):
        previous = event.previous
        if (
            event.event_type is EndpointRegistryEventType.UPDATED
            and previous is not None
            and replace(event.endpoint, active_call_id=previous.active_call_id)
            == previous
        ):
            # Claims precede the committed call projection and its action context.
            # Call events publish those transitions; registry events cover identity
            # and availability changes independently of a call.
            return
        self.refresh(dialogs=self.dialogs)

    def subscribe(self, listener, *, kind="state"):
        self._start()
        listeners = {
            "state": self.listeners,
            "activity": self.activity_listeners,
            "dialog": self.dialog_listeners,
        }[kind]
        listeners.add(listener)

        def unsubscribe():
            listeners.discard(listener)
            if not (self.listeners or self.activity_listeners or self.dialog_listeners):
                self._stop()

        return unsubscribe

    def _stop(self):
        for unsubscribe in self.unsubscribers:
            unsubscribe()
        self.unsubscribers.clear()
        self.previous = {}
        self.dialogs = {}
        self.activity = {"phones": [], "parked": []}

    @staticmethod
    def _deliver(listeners, *args):
        for listener in tuple(listeners):
            try:
                listener(*args)
            except Exception:
                _LOGGER.exception("Phone state observer failed")

    @callback
    def refresh(self, event=None, *, dialogs=None):
        from .dialog_info import activity_snapshot
        from .websocket_api import CALL_EVENT
        from .parking import PARKING_CHANGED

        runtime = require_runtime_data(self.hass)
        if dialogs is None:
            dialogs = (
                snapshot_all(runtime)
                if event is None or event.event_type in {CALL_EVENT, PARKING_CHANGED, "call_commit"}
                else self.dialogs
            )
        current = phone_states(self.hass, dialogs=dialogs)
        activity = activity_snapshot(runtime, dialogs=dialogs)
        previous, self.previous = self.previous, current
        previous_dialogs, self.dialogs = self.dialogs, dialogs
        previous_activity, self.activity = self.activity, activity
        event_data = getattr(event, "data", {}) or {}
        context = getattr(event, "context", None)
        for identity in previous.keys() | current.keys():
            before, after = previous.get(identity), current.get(identity)
            if before == after:
                continue
            payload = {
                "endpoint_id": identity,
                "previous": before,
                "phone": after,
                "removed": after is None,
            }
            phone_context = context
            if event_data.get("call_id"):
                candidates = (after or {}).get("calls", ()) or (before or {}).get("calls", ())
                selected = next((item for item in candidates
                                 if item["call_id"] == event_data["call_id"]),
                                candidates[0] if candidates else None)
                selected_id = selected["call_id"] if selected else event_data["call_id"]
                session = runtime.sip.get_session(selected_id)
                payload["call"] = (
                    self._call_fields(runtime.sip, session.committed_state)
                    if selected_id != event_data["call_id"] and session is not None
                    and session.committed_state is not None else dict(event_data)
                )
                phone_context = runtime.sip.ha_context(selected_id)
            self.hass.bus.async_fire(
                PHONE_STATE_CHANGED, deepcopy(payload), context=phone_context
            )
            self._deliver(self.listeners, payload, phone_context)
        if previous != current or previous_dialogs != dialogs:
            self._deliver(self.dialog_listeners, dialogs)
        if previous_activity != activity:
            self._deliver(self.activity_listeners, activity)

    async def close(self):
        self._stop()
        self.listeners.clear()
        self.activity_listeners.clear()
        self.dialog_listeners.clear()


def phone_state_observer(hass):
    runtime = require_runtime_data(hass)
    owner = runtime.sip
    if owner is None or not owner.active:
        raise ServiceValidationError("VoIP Stack is not ready")
    observer = owner.component("phone_state")
    if observer is None:
        observer = PhoneStateObserver(hass)
        owner.adopt_component("phone_state", observer, closer=observer.close)
    return observer
