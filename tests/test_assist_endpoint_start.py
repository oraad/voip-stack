"""Assist endpoint call identity metadata."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.voip_stack import assist_endpoint as module
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime

pytestmark = pytest.mark.ha


def _wire(token: str):
    return SimpleNamespace(
        audio_format=SimpleNamespace(wire_token=lambda: token),
        wire_token=lambda: token,
    )


async def test_trunk_assist_call_records_inbound_trunk_id(monkeypatch):
    registry = SipEndpointRuntime(allow_dark_sessions=True)
    registry.activate()
    media = SimpleNamespace(start=AsyncMock(), on_complete=None)
    monkeypatch.setattr(module, "assist_config", lambda _hass: {"name": "Assist"})
    monkeypatch.setattr(module, "roster_entry_for_target", lambda *_args: None)
    monkeypatch.setattr(module, "build_call_connected_intent", lambda **_kwargs: "prompt")
    monkeypatch.setattr(module, "AssistMediaSession", lambda *_args, **_kwargs: media)
    monkeypatch.setattr(module, "call_registry", lambda _hass: registry)
    monkeypatch.setattr(module, "publish_bridge_projection", MagicMock())
    invite = SimpleNamespace(
        call_id="call-1",
        caller="Alice",
        caller_uri=None,
        routing_caller="Alice",
        source_host="192.0.2.1",
        target="601",
        received_via_trunk=True,
        trunk_id="home",
        send_format=_wire("PCMA/8000"),
        recv_format=_wire("PCMA/8000"),
        local_audio_direction="sendrecv",
        remote_audio_connection_held=False,
    )
    endpoint = module.AssistEndpoint(MagicMock(), AsyncMock())

    await endpoint.start(
        invite,
        reservation=SimpleNamespace(release=MagicMock()),
        local_rtp_port=40000,
        roster_entries=[],
        source="trunk",
        called_extension="601",
    )

    session = registry.get_session("call-1")
    assert session is not None
    assert session.metadata["inbound_trunk_id"] == "home"
