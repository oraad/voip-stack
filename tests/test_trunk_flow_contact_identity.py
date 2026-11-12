"""Trunk-flow reuse must not erase a leg's stable Contact identity."""

from .voip_phase1_support import types, unittest, _load_intercom_module


def _client(contact):
    return types.SimpleNamespace(
        local_contact_uri=contact,
        local_ip="192.0.2.10",
        media_ip="",
        set_codec_policy=lambda policy: None,
    )


class TrunkFlowContactTest(unittest.TestCase):
    def test_static_trunk_flow_keeps_server_contact(self):
        trunk_signaling = _load_intercom_module("trunk_signaling")
        client = _client("sip:HA@192.0.2.10:5060;transport=tcp")
        trunk = types.SimpleNamespace(config=types.SimpleNamespace(
            audio_codecs=None, video_codecs=None, outbound_media_address="",
        ))
        self.assertFalse(trunk_signaling.reuse_registered_trunk_flow(trunk, client))
        self.assertEqual(client.local_contact_uri, "sip:HA@192.0.2.10:5060;transport=tcp")

    def test_registered_trunk_public_gruu_still_wins(self):
        trunk_signaling = _load_intercom_module("trunk_signaling")
        client = _client("sip:HA@192.0.2.10:5060;transport=tcp")
        trunk = types.SimpleNamespace(
            registered=True, public_gruu="sip:edge-assigned-gruu@198.51.100.9",
            config=types.SimpleNamespace(
                audio_codecs=None, video_codecs=None, outbound_media_address="",
            ),
        )
        trunk_signaling.reuse_registered_trunk_flow(trunk, client)
        self.assertEqual(client.local_contact_uri, "sip:edge-assigned-gruu@198.51.100.9")


if __name__ == "__main__":
    unittest.main()
