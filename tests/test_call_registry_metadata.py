"""Inbound From carry-through metadata for registry sessions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from .voip_phase1_support import sip
from .support.module_loader import load_voip_stack_module


pytestmark = pytest.mark.unit
call_registry = load_voip_stack_module("call_registry")


def test_trunk_invite_carries_rendered_inbound_from() -> None:
    invite = SimpleNamespace(
        received_via_trunk=True,
        caller_uri=sip.parse_sip_uri("sip:5550100@sip.tenant-a.example.test"),
    )
    assert call_registry.inbound_from_metadata(invite) == {
        "inbound_from_uri": "sip:5550100@sip.tenant-a.example.test"
    }


def test_extension_invite_carries_no_inbound_from() -> None:
    invite = SimpleNamespace(
        received_via_trunk=False,
        caller_uri=sip.parse_sip_uri("sip:5550100@sip.tenant-a.example.test"),
    )
    assert call_registry.inbound_from_metadata(invite) == {}


def test_invite_without_caller_uri_carries_no_inbound_from() -> None:
    invite = SimpleNamespace(received_via_trunk=True, caller_uri=None)
    assert call_registry.inbound_from_metadata(invite) == {}


def test_inbound_from_keeps_percent_encodings_and_parameters() -> None:
    source = "sip:5550100%40backup@sip.tenant-a.example.test;transport=tcp"
    invite = SimpleNamespace(received_via_trunk=True, caller_uri=sip.parse_sip_uri(source))
    assert call_registry.inbound_from_metadata(invite) == {"inbound_from_uri": source}
