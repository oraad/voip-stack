"""Correlate one native ESPHome action with its actual incoming SIP dialog."""

import asyncio
from dataclasses import dataclass, field
import logging
import secrets

_LOGGER = logging.getLogger(__name__)
ACTION_TIMEOUT = 8.0
CALL_ID_PREFIX = "ha-esp-"
_ROUTE_SOURCE = "esphome_action"


class CorrelationError(ValueError):
    def __init__(self, status, reason):
        self.status, self.reason = status, reason
        super().__init__(reason)


@dataclass(slots=True)
class EspCallRequest:
    session: object
    endpoint_id: str
    destination: str
    trunk_id: str
    accepted: bool = False
    ready: asyncio.Event = field(default_factory=asyncio.Event)

    def close(self, _reason):
        self.ready.set()


def apply_correlated_invite(registry, invite, identified_source):
    """Accept only the requested device and destination for this Call-ID."""
    if not invite.call_id.startswith(CALL_ID_PREFIX):
        return ""
    request = registry.resource_for(invite.call_id, "esphome_request")
    if request is None:
        if registry.is_terminated(invite.call_id):
            context = registry.event_context(invite.call_id)
            if context is not None and any(item.get("source") == _ROUTE_SOURCE for item in context.route_history):
                raise CorrelationError(481, "expired_esphome_action")
        return ""
    if not request.session.live:
        raise CorrelationError(481, "expired_esphome_action")
    if identified_source != request.endpoint_id or invite.routing_target != request.destination:
        raise CorrelationError(403, "esphome_action_mismatch")
    request.accepted = True
    request.ready.set()
    return request.trunk_id


async def originate_with_trunk(call, phone, destination):
    from homeassistant.exceptions import ServiceValidationError
    from .esphome_actions import async_call_action, has_action
    from .endpoint_session import CleanupStage, TerminationIntent, SipTerminationDisposition
    from .runtime_data import require_runtime_data
    from .trunk_selection import select_call_trunk

    hass = call.hass
    device = phone.transport_data
    if not all(has_action(hass, device, name) for name in ("start_call_via_ha", "hangup_call_if_id")):
        raise ServiceValidationError("Update this ESPHome phone's firmware to select a trunk for an individual call")
    destination = str(destination).strip()
    if not destination or len(destination.encode()) > 64 or any(char in destination for char in "|,;\r\n"):
        raise ServiceValidationError("Choose a phonebook name or dial-plan number of at most 64 UTF-8 bytes")
    selection = select_call_trunk(hass, requested=str(call.data["trunk_id"]), endpoint_id=phone.endpoint_id)
    if not selection.available:
        raise selection.unavailable_error()
    runtime = require_runtime_data(hass)
    registry = runtime.sip
    if registry is None or not registry.active:
        raise ServiceValidationError("The Home Assistant SIP endpoint is unavailable")
    endpoint = runtime.endpoints.get(phone.endpoint_id)
    if endpoint is None:
        raise ServiceValidationError("The selected ESPHome phone is unavailable")
    if endpoint.active_call_id:
        raise ServiceValidationError("The selected ESPHome phone is busy")
    call_id = CALL_ID_PREFIX + secrets.token_hex(16) + "@ha"
    session = registry.upsert(call_id, owner="router", state="connecting", caller=phone.name,
        caller_id=str(getattr(endpoint, "sip_uri_user", "") or phone.name), callee=destination,
        source_endpoint_id=phone.endpoint_id, endpoint_id=phone.endpoint_id,
        outbound_trunk_id=selection.identity)
    request = EspCallRequest(session, phone.endpoint_id, destination, selection.identity)
    sent = False
    try:
        registry.claim_endpoint(call_id, phone.endpoint_id, role="source")
        registry.bind_controller(call_id, context=call.context, endpoint_id=phone.endpoint_id)
        registry.record_route(call_id, action="originate", destination=destination, source=_ROUTE_SOURCE)
        registry.own_resource(call_id, f"esphome_request:{call_id}", request, request.close,
            stage=CleanupStage.OBSERVER, generation=session.generation)
        async with asyncio.timeout(ACTION_TIMEOUT):
            sent = True
            await async_call_action(hass, device, "start_call_via_ha", {"dest": destination, "call_id": call_id}, context=call.context)
            await request.ready.wait()
        if not request.accepted:
            raise ServiceValidationError("The ESPHome call ended before it could be routed")
        return call_id
    except BaseException as error:
        from .session_cleanup import async_wait_for_cleanup
        async def cleanup():
            if sent:
                try:
                    async with asyncio.timeout(2):
                        await async_call_action(hass, device, "hangup_call_if_id", {"call_id": call_id}, context=call.context)
                except Exception:
                    _LOGGER.debug("Could not cancel ESPHome action call_id=%s", call_id, exc_info=True)
            if registry.get_session(call_id) is session:
                await registry.terminate_call_wait(call_id, intent=TerminationIntent("cancelled",
                    sip_disposition=SipTerminationDisposition.AUTO if request.accepted else SipTerminationDisposition.NONE))
        await async_wait_for_cleanup(asyncio.create_task(cleanup(), name="voip-esphome-action-cleanup"))
        if isinstance(error, TimeoutError):
            raise ServiceValidationError("The ESPHome phone did not start its requested SIP call") from error
        raise
