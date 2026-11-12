"""Executable capacity and retransmission guards for inbound routing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.voip_stack import invite_router
from custom_components.voip_stack.core.sdp import RtpPcmFormat
from custom_components.voip_stack.core.sip import parse_sip_uri
from custom_components.voip_stack.router import RouteAction, RouteDecision
from custom_components.voip_stack.sip_listener import SipInvite


pytestmark = pytest.mark.ha


def _invite() -> SipInvite:
    audio = RtpPcmFormat(8, "PCMA", 8000, 1)
    return SipInvite(
        source_host="192.0.2.10",
        source_port=5060,
        request_uri=parse_sip_uri("sip:Casa@192.0.2.1"),
        caller_uri=parse_sip_uri("sip:alice@192.0.2.10"),
        target="Casa",
        caller="Alice",
        caller_route="alice",
        caller_id="alice",
        call_id="call-1",
        cseq="1 INVITE",
        remote_sdp=b"",
        send_format=audio,
        recv_format=audio,
        remote_rtp_host="192.0.2.10",
        remote_rtp_port=40000,
    )


def _runtime() -> invite_router.InviteRuntime:
    return invite_router.InviteRuntime(
        hass=SimpleNamespace(),
        config={},
        local_ip="192.0.2.1",
        registrar=SimpleNamespace(registration_matches_source=Mock(return_value=False)),
        ha_peer_name=Mock(),
        get_trunk_config=Mock(return_value={}),
        trunk_enabled=Mock(return_value=False),
        is_trunk_invite=Mock(return_value=False),
        is_ha_target=Mock(return_value=False),
        ha_router_decision=Mock(),
        inbound_route_decision=Mock(
            return_value=RouteDecision(RouteAction.ANSWER_HA, target="Casa")
        ),
        build_peer_snapshot=AsyncMock(return_value=[]),
        browser_leg_for_member=Mock(),
        defer_invite_to_softphone=Mock(),
        enable_reused_sip_tcp_connection=Mock(),
        on_conference_inbound_timeout=Mock(),
        ring_conference_members=Mock(),
        run_ring_group_call=Mock(),
        run_trunk_inbound_route_guarded=Mock(),
        send_final_response=Mock(),
        sip_uri_transport=Mock(),
        start_local_assist_bridge=Mock(),
        terminate_sip_bridge=Mock(),
    )


def _patch_route_prefix(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pending_routes: dict[str, object] | None = None,
    pending_invites: dict[str, object] | None = None,
) -> None:
    monkeypatch.setattr(invite_router, "_peer_for_target", Mock(return_value=None))
    monkeypatch.setattr(
        invite_router,
        "_registered_roster_entries",
        Mock(return_value=[]),
    )
    monkeypatch.setattr(invite_router, "_roster_from_peers", Mock(return_value=[]))
    monkeypatch.setattr(
        invite_router,
        "_roster_entry_for_target",
        Mock(return_value=None),
    )
    invites = pending_invites or {}
    registry = SimpleNamespace(
        component=lambda _name: None,
        sip_client_for=lambda _call_id: None,
        get_session=lambda _call_id: None,
        artifact_items=lambda name: (
            iter(invites.items()) if name == "pending_invite" else iter(())
        )
    )
    monkeypatch.setattr(invite_router, "_call_registry", lambda _hass: registry)
    monkeypatch.setattr(
        invite_router,
        "endpoint_directory",
        lambda _hass: SimpleNamespace(get=Mock(return_value=None)),
    )
    monkeypatch.setattr(
        invite_router,
        "_pending_routes",
        lambda _hass: pending_routes or {},
    )


def test_trunk_caller_lookup_requires_a_number_and_matches_number_only() -> None:
    from dataclasses import replace

    from custom_components.voip_stack.roster import RosterEntry

    entries = [
        RosterEntry(id="alex", name="Alex Mobile", number="+1 (202) 555-0100"),
        RosterEntry(id="local", name="Local extension", extension="2025550100"),
    ]
    number_invite = replace(
        _invite(), caller="+1 202 555 0100", caller_route="+12025550100"
    )
    assert invite_router._trunk_caller_name(number_invite, entries) == "Alex Mobile"

    empty_display = replace(_invite(), caller="", caller_route="+12025550100")
    assert invite_router._trunk_caller_name(empty_display, entries) == "Alex Mobile"

    named_invite = replace(_invite(), caller="Dio Cane", caller_route="+12025550100")
    assert invite_router._trunk_caller_name(named_invite, entries) == ""

    mismatched_invite = replace(
        _invite(), caller="+12025550101", caller_route="+12025550100"
    )
    assert invite_router._trunk_caller_name(mismatched_invite, entries) == ""

    extension_only = [
        RosterEntry(id="local", name="Local extension", extension="2025550100")
    ]
    assert invite_router._trunk_caller_name(number_invite, extension_only) == ""

    ambiguous = [
        RosterEntry(id="alex", name="Alex Mobile", number="+12025550100"),
        RosterEntry(id="alex-2", name="Alex Office", number="+12025550100"),
    ]
    assert invite_router._trunk_caller_name(number_invite, ambiguous) == ""

    disabled = [
        RosterEntry(id="alex", name="Alex Mobile", number="+12025550100", enabled=False)
    ]
    assert invite_router._trunk_caller_name(number_invite, disabled) == ""

    # A contact whose "name" is the number itself must never trigger a fake
    # replacement; the displayed value would not change.
    number_named = [
        RosterEntry(id="alex", name="+12025550100", number="+12025550100")
    ]
    assert invite_router._trunk_caller_name(number_invite, number_named) == ""

    # Short numeric displays (extensions, 2-digit service codes) are not phone
    # numbers, even when a contact happens to share the digits.
    short_invite = replace(_invite(), caller="22", caller_route="22")
    assert invite_router._trunk_caller_name(short_invite, entries) == ""


@pytest.mark.asyncio
async def test_trunk_route_applies_phonebook_name_and_keeps_raw_caller_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from custom_components.voip_stack.roster import RosterEntry
    from custom_components.voip_stack.sip_listener import SipInviteResult

    _patch_route_prefix(monkeypatch)
    entries = [RosterEntry(id="alex", name="Alex Mobile", number="+12025550100")]
    monkeypatch.setattr(
        invite_router,
        "_roster_from_peers",
        Mock(return_value=entries),
    )
    runtime = _runtime()
    runtime.is_trunk_invite = Mock(return_value=True)
    runtime.is_ha_target = Mock(return_value=False)
    runtime.get_trunk_config = Mock(
        return_value={
            "trunk_inbound_mode": "direct",
            "trunk_inbound_default_target": "Assistant",
            "automation_routing_enabled": True,
        }
    )
    runtime.inbound_route_decision = Mock(
        return_value=RouteDecision(RouteAction.ASSIST, target="Assistant")
    )
    seen = []

    async def capture(invite, **kwargs):
        seen.append(invite)
        return SipInviteResult(200, "OK", to_tag="")

    monkeypatch.setattr(invite_router, "route_local_assist", capture)
    monkeypatch.setattr(
        invite_router,
        "request_route_override",
        AsyncMock(return_value=SimpleNamespace(action="", destination="")),
    )
    monkeypatch.setattr(invite_router, "automation_rejection", lambda **_: None)
    monkeypatch.setattr(
        invite_router,
        "registration_data",
        lambda _: SimpleNamespace(route_trigger_filters={}),
    )

    result = await invite_router.route_invite(
        runtime,
        replace(_invite(), caller="+12025550100", caller_route="+12025550100"),
    )

    assert result.status == 200
    assert seen[0].caller == "Alex Mobile"
    assert seen[0].caller_id == "+12025550100"
    assert seen[0].routing_caller == "+12025550100"


@pytest.mark.asyncio
async def test_trunk_route_keeps_provider_name_and_raw_caller_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from custom_components.voip_stack.roster import RosterEntry
    from custom_components.voip_stack.sip_listener import SipInviteResult

    _patch_route_prefix(monkeypatch)
    entries = [RosterEntry(id="alex", name="Alex Mobile", number="+12025550100")]
    monkeypatch.setattr(
        invite_router,
        "_roster_from_peers",
        Mock(return_value=entries),
    )
    runtime = _runtime()
    runtime.is_trunk_invite = Mock(return_value=True)
    runtime.is_ha_target = Mock(return_value=False)
    runtime.get_trunk_config = Mock(
        return_value={
            "trunk_inbound_mode": "direct",
            "trunk_inbound_default_target": "Assistant",
            "automation_routing_enabled": True,
        }
    )
    runtime.inbound_route_decision = Mock(
        return_value=RouteDecision(RouteAction.ASSIST, target="Assistant")
    )
    seen = []

    async def capture(invite, **kwargs):
        seen.append(invite)
        return SipInviteResult(200, "OK", to_tag="")

    monkeypatch.setattr(invite_router, "route_local_assist", capture)
    monkeypatch.setattr(
        invite_router,
        "request_route_override",
        AsyncMock(return_value=SimpleNamespace(action="", destination="")),
    )
    monkeypatch.setattr(invite_router, "automation_rejection", lambda **_: None)
    monkeypatch.setattr(
        invite_router,
        "registration_data",
        lambda _: SimpleNamespace(route_trigger_filters={}),
    )

    result = await invite_router.route_invite(
        runtime,
        replace(
            _invite(),
            caller="Dio Cane",
            caller_route="+12025550100",
            caller_id="+12025550100",
        ),
    )

    assert result.status == 200
    assert seen[0].caller == "Dio Cane"
    assert seen[0].caller_id == "+12025550100"
    assert seen[0].routing_caller == "+12025550100"


@pytest.mark.asyncio
async def test_route_retransmit_keeps_pending_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_route_prefix(monkeypatch, pending_routes={"call-1": object()})

    result = await invite_router.route_invite(_runtime(), _invite())

    assert result.status == 100
    assert result.reason == "Trying"


@pytest.mark.asyncio
async def test_route_retransmit_keeps_existing_ringing_invite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_route_prefix(monkeypatch, pending_invites={"call-1": object()})

    result = await invite_router.route_invite(_runtime(), _invite())

    assert result.status == 180
    assert result.defer_final is True


@pytest.mark.asyncio
async def test_route_capacity_rejects_before_allocating_more_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = {f"other-{index}": object() for index in range(64)}
    _patch_route_prefix(monkeypatch, pending_invites=pending)

    result = await invite_router.route_invite(_runtime(), _invite())

    assert result.status == 503
    assert result.decline_reason == "capacity_exhausted"
    assert len(pending) == 64


@pytest.mark.parametrize("known", [True, False])
@pytest.mark.asyncio
async def test_direct_trunk_preserves_called_target_and_uses_fallback_only_if_unknown(monkeypatch, known):
    from dataclasses import replace
    from custom_components.voip_stack.inbound_routing.automation import AutomationRoute
    from custom_components.voip_stack.sip_listener import SipInviteResult

    _patch_route_prefix(monkeypatch)
    original = replace(_invite(), target="601", target_route="601")
    destination = SimpleNamespace(extension="601", display_name="Bedroom")
    fallback = SimpleNamespace(extension="777", display_name="Assistant")
    initial = RouteDecision(RouteAction.FORWARD if known else RouteAction.TRUNK,
                            target="Bedroom" if known else "601", entry=destination if known else None)
    selected_fallback = RouteDecision(RouteAction.ASSIST, target="Assistant", entry=fallback)
    runtime = _runtime()
    runtime.is_trunk_invite = Mock(return_value=True)
    runtime.is_ha_target = Mock(return_value=False)
    runtime.get_trunk_config = Mock(return_value={"trunk_inbound_mode": "direct", "trunk_inbound_default_target": "Assistant", "automation_routing_enabled": True})
    runtime.inbound_route_decision = Mock(side_effect=lambda invite, *_: initial if invite.routing_target == "601" else selected_fallback)
    monkeypatch.setattr(invite_router, "registration_data", lambda h: SimpleNamespace(route_trigger_filters={}))
    request = AsyncMock(return_value=AutomationRoute(action="decline"))
    monkeypatch.setattr(invite_router, "request_route_override", request)
    monkeypatch.setattr(invite_router, "automation_rejection", lambda **kw: SipInviteResult(603, "Decline", to_tag=""))
    assist = AsyncMock(return_value=SipInviteResult(488, "Wrong route", to_tag=""))
    monkeypatch.setattr(invite_router, "route_local_assist", assist)

    result = await invite_router.route_invite(runtime, original)

    assert result.status == 603
    request.assert_awaited_once()
    assert request.await_args.kwargs["invite"] is original
    assert request.await_args.kwargs["invite"].routing_target == "601"
    assert request.await_args.kwargs["decision"] == (initial if known else selected_fallback)
    assist.assert_not_awaited()


@pytest.mark.parametrize("override", [False, True])
async def test_restricted_registered_caller_cannot_egress_after_route_override(monkeypatch, override):
    from custom_components.voip_stack.roster import RosterEntry
    _patch_route_prefix(monkeypatch)
    runtime = _runtime()
    runtime.config["external_call_blocked_endpoints"] = ["sip:alice"]
    runtime.registrar.registration_matches_source.return_value = True
    route = RouteDecision(RouteAction.TRUNK, target="441234567890")
    runtime.inbound_route_decision.return_value = (
        RouteDecision(RouteAction.FORWARD, target="Desk", sip_uri="sip:desk@192.0.2.20")
        if override else route
    )
    runtime.ha_router_decision.return_value = route
    runtime.get_trunk_config.return_value = {"trunk_server": "provider.example"}
    runtime.trunk_enabled.return_value = True
    caller = RosterEntry(id="alice", metadata={"endpoint_id": "sip:alice"})
    monkeypatch.setattr(invite_router, "_roster_entry_for_target", lambda *_: caller)
    registry = SimpleNamespace(component=lambda _: None, artifact_items=lambda _: iter(()), get_session=lambda _: None,
                               sip_client_for=lambda _: None)
    monkeypatch.setattr(invite_router, "_call_registry", lambda _: registry)
    monkeypatch.setattr(invite_router, "registration_data", lambda _: SimpleNamespace(route_trigger_filters={}))
    monkeypatch.setattr(invite_router, "request_route_override", AsyncMock(return_value=SimpleNamespace(
        action="forward" if override else "", destination="441234567890" if override else "",
    )))
    monkeypatch.setattr(invite_router, "automation_rejection", lambda **_: None)
    monkeypatch.setattr(invite_router, "resolve_inbound_target", lambda **kwargs: SimpleNamespace(
        failure=None, decision=kwargs["decision"], endpoint=None,
    ))
    monkeypatch.setattr(invite_router, "sip_trunk", lambda _: SimpleNamespace(ready=True))
    bridge = AsyncMock(side_effect=AssertionError("forbidden call must not start bridge"))
    monkeypatch.setattr(invite_router, "route_sip_bridge", bridge)
    result = await invite_router.route_invite(runtime, _invite())
    assert result.status == 403
    assert result.decline_reason == "external_calls_disabled"
    bridge.assert_not_awaited()
