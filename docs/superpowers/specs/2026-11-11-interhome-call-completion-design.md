# Inter-home call completion: contact in the 180, verbatim fork identity, edge loop guard

Date: 2026-11-11
Repos: voip-stack (root: `/root/voip-stack`), voip-edge (`/root/voip-edge`)

Privacy note: this doc and all tests use placeholder identities only
(`5550100@sip.tenant-a.example.test`, `10.0.4.10`). No real tenant domains,
numbers, or site addresses appear in code, tests, or specs.

## Why

Inter-home calls die at ringing. The caller has no way to PRACK because the reliable 180
leaves voip-stack without a Contact (RFC 3262 §3 violation): `build_uas_response`
(`core/sip.py:1050`) adds Contact only for 2xx, while `_transmit_reliable_provisional`
(`sip_listener.py:903-915`) sends `Require: 100rel` + `RSeq` through the same builder.
Even when an in-dialog request is sent, the edge relays some of them toward its own
listen address; the copy re-enters, fails the dialog-source check, and is 403'd
(`misc.cfg route[WITHINDLG]`). Separately, a leg forked from an inbound trunk call gets
a rebuilt From (`5550100@10.0.4.10:40150;transport=tcp`, per
`local_softphone_runtime.py:497`), so the callee cannot redial and the missed-call entry
resolves to the wrong tenant.

## Goals

1. Every reliable provisional response to an INVITE carries Contact.
2. A leg forked from an inbound trunk call carries the inbound From URI verbatim.
3. voip-edge never relays an in-dialog request to itself; overlay-bound in-dialog
   relays leave from the socket that can reach them.
4. Prove one inter-home call end to end in Homer and voip-stack logs: 180 with Contact,
   PRACK, 200 PRACK, 200 OK, RTP through rtpengine, state `connected`.

## Non-goals

- Contact on plain 100/180 that do not carry `Require: 100rel` — those keep omitting it.
- The edge From rewrite for trusted home→phone legs (`invite.cfg:20-23`). Left alone;
  revisited only if redial still fails after fix 2.
- Call-ID dialog anchoring or any dialog logic in the edge. The edge change stays a guard.
- Any ordering or feature dependency between voip-stack and voip-edge deploys: the
  voip-stack changes must stand on their own and be agnostic to the edge.

## Anti-goals

- A voip-stack change that only behaves correctly once an edge change ships.
- Pinning TLS or public-IP phone flows to the WG socket by blanket-applying
  `route(TUNNEL_SOCKET)`.
- An edge recreate during a busy window without accepting that regcache (memory-only)
  is lost: phones unreachable until they re-REGISTER, worst case ~55 min.
- Real subscriber identifiers (names, numbers, tenant domains, site IPs) in any
  committed file, test fixture, or log snippet.

## Constraints

- voip-stack: two separate commits. `pytest -q` baseline 786 passed, 1 deselected stays
  green before deploy.
- Test data is fake: placeholder tenants (`sip.tenant-a.example.test`), numbers
  (`5550100`), documentation-range addresses. Existing fixtures follow this already;
  new tests keep it.
- voip-edge: `kamailio/*.cfg` is mounted config (`${EDGE_HOME}` bind mount in
  `compose.yaml`), so deploy is a container recreate, not a table reload. Config must
  pass `kamailio -c -f kamailio.cfg -A 'PUBIP="..."'` validation first.
- The 180 Contact is voip-stack's local socket URI (`_response_contact_uri`,
  `sip_listener.py:516-523`). How that address is reached is the edge's concern, not
  the stack's — that is what keeps fix 1 agnostic.

## Branch placement

The repo has two open feature branches on `origin`:

- `feature/trunk-event-identity` (currently checked out, 3 commits ahead): directional
  trunk identities in HA event metadata. Touches none of the fix sites
  (`core/sip.py`, `local_softphone_runtime.py`, `endpoint_runtime.py` — empty diff).
- `feature/trunk-caller-name-lookup` (1 commit ahead, 58 files): phonebook name lookup
  and `caller_id` propagation into registry sessions/events. Does NOT fix the wire
  From: at its tip `local_softphone_runtime.py:503` still builds
  `SipUri(caller.sip_uri_user, local_ip, sip_port)`. It propagates display identity for
  events; fix 2 fixes the SIP From header — complementary concerns, same file.

Decision: an **independent branch off `main`** carrying both fix commits. The two
feature branches are open review tracks unrelated to these bugs; stacking the
call-killing fixes behind a 58-file feature merge delays them for no gain. Expect one
trivial conflict in `local_softphone_runtime.py` when the From line is touched by both
tracks; whoever merges second rebases.

## Workspace layout (done 2026-11-11)

- Repo contents moved from `/root/voip-stack/voip-stack` up to `/root/voip-stack`;
  `.git` history intact, working tree clean, `.venv` shebang/prefix paths repaired,
  suite verified from the new root (`pytest tests/test_sip_uri.py` green).
- Removed as superseded: `kamailio-edge/` (its Pangolin procedure, native-WireGuard
  convention, and trusted-row management now live in `/root/voip-edge` docs and the
  console Trusted table; `setup-trusted.sh` is replaced by the console), the old
  `repo.tar.gz` source tarball (the real clone replaced it), stale research docs
  (folded into `voip-edge/docs/research-*.md`), and regeneratable caches.
- Sibling projects (`esphome-*`, `ha-voip-lab`, `sip-peer`) and the root `*.log`
  incident captures stay at their absolute paths; they are excluded via
  `.git/info/exclude` (local-only, tracked `.gitignore` untouched).
- Follow-up done during relocation: coordinated-sibling discovery assumed the old
  nested layout in eight places. All now resolve through `tests/support/sibling_repos.py`
  (`sibling_repo()`: env override → inside the workspace → beside it, so CI keeps
  working) or an equivalent in-workspace probe in `scripts/yaml_paths.sh` and
  `scripts/test_suite.sh`. Files touched: `tests/test_package_composition.py`,
  `tests/test_esphome_upstream_alignment.py`, `tests/test_full_processing_restore.py`,
  `tests/test_voip_yaml_media_contract.py`, `tests/test_lvgl_settings_composition.py`,
  `scripts/yaml_paths.sh`, `scripts/test_suite.sh`. These edits ride the fix branch
  as one `chore(workspace)` commit.
- Baseline caveat: ESPHome config tests download wake-word manifests, models, and
  fonts from `github.com` / `raw.githubusercontent.com` / `fonts.googleapis.com`.
  During relocation these downloads hit transient network failures (owner note:
  not firewall-blocked — retry). Full fast-mode run on the new root: 2390 passed,
  171 failed + 8 errored — all in that download-bound subset, plus the one missed
  sibling site (`tests/runtime_observer_fixture.py`) that was fixed, after which
  the errors became passes. The deploy gate is the suite green with working
  network; the protocol/HA-side tests — where fixes 1 and 2 live — are
  network-independent.

## Approaches considered

- Ship stack fix + edge WG-egress in one coupled window so verification passes first try.
  Rejected: couples the repos; the user requires voip-stack to be edge-independent.
- Advertise a publicly reachable Contact in the 180 instead of the local socket.
  Rejected: every response Contact in this deployment is local-socket-shaped; advertising
  the tenant domain violates the literal-IP trunk rule.
- **Chosen:** ship fix 1 and fix 2 in voip-stack independently (one new branch off main,
  two commits), ship fix 3 in voip-edge independently at a quiet time, run the step-4
  verification once both lands exist.

## Design

### Fix 1 (commit A): contact header in the reliable provisional

`core/sip.py`, `build_uas_response`: extend the line-1050 gate so Contact is emitted when
`request.method == "INVITE"` and either 2xx (unchanged) or 101 ≤ status < 200 **and the
response itself carries `Require: 100rel`**. Reliability is detected by scanning the
`extra_headers` argument for a `Require` value containing `100rel`. No new function
parameter, no new headers elsewhere; session-timer headers stay 2xx-only.

Test: new file in `tests/` — an INVITE + `status_code=180` +
`extra_headers=(("Require", "100rel"), ("RSeq", "1"))` yields `Contact: <sip:...>`; the
same 180 and a 100 without `100rel` yield no Contact; existing 2xx behavior unchanged.
All fixtures fake.

### Fix 2 (commit B): inbound caller identity stays verbatim on forked legs

Rule: for a leg forked from an inbound trunk call, the From URI is the inbound From URI
verbatim. Local IP, local port, and the local tenant domain must never enter the From of
a trunk leg.

- Carry: when the inbound trunk INVITE creates the registry session, store the inbound
  From URI text in session metadata (pattern follows existing `call_registry` metadata
  keys). `initial.caller_uri` is already carried at `sip_listener.py:1774` and `:3712`
  and shows the same principle.
- Use: `local_softphone_runtime.py:497` builds the leg's `caller_uri` from that stored
  URI (parsed) when present, instead of `SipUri(caller.sip_uri_user, local_ip, sip_port)`;
  falls back to current behavior for locally originated calls.
- Audit: `endpoint_runtime.py:633` — same rebuild shape; confirm whether a trunk-origin
  call can reach this construction (HA conference/softphone path) and apply the rule
  there only if it can.

Byte-identity note: `parse_sip_uri` preserves parameter order and `SipUri.__str__`
renders params in order, so `str(parse_sip_uri(raw))` round-trips a bare inbound URI
unchanged. The test asserts the forked leg's From equals the inbound
`From: <sip:5550100@sip.tenant-a.example.test>` byte-for-byte. Display-name and tag
handling do not change.

### Fix 3: in-dialog relay guard and overlay egress at the edge

`kamailio/misc.cfg`, `route[WITHINDLG]`, inside the `loose_route()` branch, before
`t_relay()`:

- Loop guard: if the resolved destination (`$du`) is one of our own listen addresses
  (contains PUBIP or WGIP) and the request is not an ACK for a local transaction
  (`is_method("ACK") && t_check_trans()` stays relayable), log at `L_WARN` and answer
  `sl_send_reply("482", "Loop Detected")`, `exit`. Never loop.
- Overlay egress: call `route(TUNNEL_SOCKET)` only when the destination is
  overlay-bound, keyed on the destination host being in the WireGuard peer range
  (`100.64.0.0/10`), extended only if live traffic shows an overlay-bound in-dialog
  relay failing toward another reachable range. A blanket call is not acceptable:
  `TUNNEL_SOCKET` branches only on `;transport=tcp` and otherwise pins the UDP WG
  socket, which would misroute TLS and public-IP phone flows that work today.

The ACK branch after a failed `loose_route()` (misc.cfg:38-41) stays as is.
Validation: `kamailio -c`, then container recreate in a quiet window, with the regcache
loss consequence accepted. Any doc/example added to voip-edge during this work passes
its privacy gate (`tools/check-privacy.sh` + local `private-deny.txt`).

### Verification (after both repos ship their changes)

One inter-home call, checked in Homer by call-id:

1. 180 carries `Contact: <sip:...>`.
2. PRACK reaches the voip-stack host address and `200 PRACK` returns.
3. `200 OK` for the INVITE; RTP flows through rtpengine both directions.
4. voip-stack log reaches state `connected`, never `prack_failed`/`prack_timeout`.
5. The forked leg's From equals the inbound From byte-for-byte.

## Left to the builder

- Metadata key name for the carried inbound From, matching `call_registry` conventions.
- Exact `$du` test style in the cfg (host extraction vs regex), readable in Kamailio 6.1.5.
- Test file placement follows the existing `tests/test_sip_*.py` pattern.
