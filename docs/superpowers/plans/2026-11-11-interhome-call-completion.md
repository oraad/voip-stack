# Inter-home call completion implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make inter-home calls complete — reliable 180 carries a Contact so PRACK works, forked trunk legs keep the inbound From verbatim, and voip-edge never relays an in-dialog request to itself.

**Architecture:** Two small, independent voip-stack commits on one fresh branch off `main` (response builder gate; inbound-From carried through registry metadata to the local bridge leg) plus one voip-edge `misc.cfg` guard with its static regression test. voip-stack behavior is agnostic to the edge; the edge change is a guard, not dialog logic.

**Tech Stack:** Python 3.14 + pytest (Home Assistant custom component), Kamailio 6.1.5 config, Homer (HEP captures) for verification.

**Spec:** `docs/superpowers/specs/2026-11-11-interhome-call-completion-design.md`

## Global constraints

- All new code, tests, docs, and log examples use fake identities only: `5550100@sip.tenant-a.example.test`, `10.0.4.10`, documentation/CGNAT-style addresses. Never real tenant domains, numbers, or site IPs.
- `build_uas_response` keeps its exact signature; no new parameters, no new headers outside the Contact gate.
- Session-timer headers stay 2xx-only; plain 100/180 without `Require: 100rel` keep omitting Contact.
- The edge From rewrite (`invite.cfg` `uac_replace_from`) is NOT touched.
- The edge change is config-only: guard + conditional socket pinning; no dialog modules, no Call-ID anchoring.
- voip-stack deploy gate: `bash scripts/test_suite.sh fast` green with working network (the ~179 ESPHome config tests download wake-word models/fonts from github.com and fonts.googleapis.com; transient failures there are retried, not waived).
- Commits: Conventional Commits, scoped (repo style: `fix(sip): …`, `chore(workspace): …`), message bodies written in normal prose.
- Edge container recreate is operator-scheduled only (regcache is memory-only; phones unreachable until re-REGISTER, worst ~55 min). Do not recreate or restart in this plan.

## Review focus

Each line is an input class the spec implies that no task's main test names; the named task pins it with an extra assertion in its own test.

1. Reliable provisional with empty `contact_uri` must not emit `Contact: <>` — Task 1.
2. Inbound From carrying URI parameters (e.g. `;transport=tcp` on a non-standard inbound) must keep parameters in order through store→parse→render — Task 2.
3. A session created without the new metadata key (locally originated call, or pre-fix session) must fall back to the exact current leg behavior — Task 2.
4. In-dialog relay whose `$du` is the edge's own address with no user part (`sip:203.0.113.10`) must hit the 482 guard, not the regcache 403 path — Task 3.
5. Overlay-bound UDP flows (`100.64/10`, non-tcp) pin via `WGSOCK` the same as TCP pins `tcp:WGIP:5060` — Task 3.

---

### Task 0: relocation chore onto the fix branch

Carries the already-written workspace-relocation fixes (design doc "Workspace layout" list) onto a fresh branch off `main` as one chore commit, together with the spec and this plan. Current branch `feature/trunk-event-identity` must stay untouched.

**Files:**
- Create branch `feature/inter-pbx-call-completion` from `main` (repo root `/root/voip-stack`).
- Carried modified files (identical between `main` and current HEAD, so they travel across checkout): `scripts/yaml_paths.sh`, `tests/support/sibling_repos.py` (new), `tests/runtime_observer_fixture.py`, `tests/test_package_composition.py`, `tests/test_esphome_upstream_alignment.py`, `tests/test_full_processing_restore.py`, `tests/test_voip_yaml_media_contract.py`, `tests/test_lvgl_settings_composition.py`, `docs/superpowers/`.
- Re-applied after checkout: `scripts/test_suite.sh` (differs between `main` and current HEAD; carry would smuggle a foreign line).

**Interfaces:**
- Consumes: nothing.
- Produces: branch `feature/inter-pbx-call-completion`; helper `tests.support.sibling_repos.sibling_repo(variable: str, name: str) -> pathlib.Path` used by later test steps.

- [ ] **Step 1: Re-apply the test_suite.sh probe on main's version instead of carrying it.**

```bash
git diff -- scripts/test_suite.sh > /tmp/relocation-test-suite.patch
```

Read the patch; it must contain only the `resolve_ha_python` change (probe `$ROOT/ha-voip-lab/.venv/bin/python` first, fall back to `$ROOT/../ha-voip-lab/.venv/bin/python`). If it contains anything else, stop and report.

- [ ] **Step 2: Protect it and switch branches.**

```bash
git stash push -m relocation-test-suite -- scripts/test_suite.sh
git checkout -b fix/interhome-call-completion main
git stash pop
```

`git stash pop` applies cleanly because the `event-identity` delta on this file is a different hunk (`ha_tests` list). If it ever conflicts, edit `resolve_ha_python` on main's version by hand to match Step 1 and `git stash drop`.

- [ ] **Step 3: Verify the tree.**

Run: `git status --short && bash -n scripts/test_suite.sh && .venv/bin/python -m ruff check tests scripts`
Expected: only the carried files + `docs/superpowers/` + `tests/support/sibling_repos.py` as changes; `bash -n` silent; ruff "All checks passed!".

Run: `.venv/bin/python -m pytest tests/test_documentation_contract.py -q -p no:cacheprovider`
Expected: pass (spec headings comply with the markdown contract).

- [ ] **Step 4: Commit.**

```bash
git add -A scripts tests/support/sibling_repos.py docs/superpowers
git commit -m "chore(workspace): relocate checkout to workspace root

Siblings are resolved through tests/support/sibling_repos.py, yaml_paths and
test_suite probe the new layout with the old one kept as fallback. Adds the
inter-home call completion spec and plan."
```

---

### Task 1: contact header in the reliable provisional (voip-stack commit A)

**Files:**
- Modify: `custom_components/voip_stack/core/sip.py` (`build_uas_response`, gate at line 1050; `extra_headers` consumed at line 1061)
- Test: `tests/test_sip_uri.py` (class `SipUriTest`; follow the request+`build_uas_response`+`parse_message` pattern at lines 18-35)

**Interfaces:**
- Consumes: nothing new.
- Produces: for an INVITE, `build_uas_response` emits `Contact: <{contact_uri}>` when `101 <= status < 200` **and** the materialized `extra_headers` contain `("Require", <value containing "100rel">)`. Behavior for 2xx and all other methods is unchanged. `_transmit_reliable_provisional` (`sip_listener.py:903-915`) already passes `contact_uri` via `_send_response` (`sip_listener.py:2023`) — no listener change.

- [ ] **Step 1: Write the failing tests in `SipUriTest`.**

Test 1 `test_reliable_provisional_invite_response_carries_contact`: build a request for INVITE exactly as the class's existing pattern (From `<sip:5550100@sip.tenant-a.example.test>;tag=f1`, To `<sip:5550200@sip.tenant-b.example.test>`, R-URI `sip:5550200@sip.tenant-b.example.test`). Call `sip.build_uas_response(request, 180, "Ringing", ... headers..., contact_uri="sip:5550100@10.0.4.10:5060;transport=tcp", to_tag="t1", extra_headers=(("Require", "100rel"), ("RSeq", "1")))`, parse the result with `sip.parse_message`, assert `parsed.header("Contact") == "<sip:5550100@10.0.4.10:5060;transport=tcp>"`.

Test 2 `test_plain_provisional_responses_stay_contact_free`: same builder, two subTests — 100 `Trying` and 180 `Ringing` with `extra_headers=()` — assert `parsed.header("Contact")` is falsy.

Test 3 (Review Focus 1) `test_reliable_provisional_without_contact_uri_stays_contact_free`: 180 + `Require: 100rel` + `contact_uri=""` — assert no Contact header (never `Contact: <>`).

- [ ] **Step 2: Run to verify they fail.**

Run: `.venv/bin/python -m pytest tests/test_sip_uri.py -k "reliable_provisional or plain_provisional" -q`
Expected: tests 1 fails (no Contact); 2 and 3 pass already.

- [ ] **Step 3: Implement in `build_uas_response` (`core/sip.py`).**

Materialize `extra_headers = tuple(extra_headers)` once at the top of the function (it is extended into `headers` later; scanning an arbitrary iterable first would consume a generator). Derive `require_100rel = any(name == "Require" and "100rel" in value for name, value in extra_headers)`. After the existing 2xx Contact block (line 1050-1054), add the new gate:

```python
if (contact_uri and request.method == "INVITE"
        and 101 <= int(status_code) < 200 and require_100rel):
    headers.append(("Contact", f"<{contact_uri}>"))
```

Do not move or re-condition the existing 2xx block; `Supported` (line 1055) already covers 1xx.

- [ ] **Step 4: Run to verify all three pass.**

Run: `.venv/bin/python -m pytest tests/test_sip_uri.py -q`
Expected: all pass.

Run: `bash scripts/test_suite.sh fast`
Expected: green (retry any download-bound test once; transient network, not policy).

- [ ] **Step 5: Commit.**

```bash
git add custom_components/voip_stack/core/sip.py tests/test_sip_uri.py
git commit -m "fix(sip): carry contact header on reliable provisional responses

RFC 3262 requires a Contact in every reliable provisional response so the
caller has a remote target for PRACK. The builder emits it only when the
response itself carries Require: 100rel, keeping plain 100/180 unchanged."
```

---

### Task 2: verbatim inbound caller address on forked legs (voip-stack commit B)

**Files:**
- Modify: `custom_components/voip_stack/call_registry.py` (add `inbound_from_metadata`)
- Modify: `custom_components/voip_stack/endpoint_runtime.py:382-394` and `:769-777` (the two inbound `registry.upsert(...)` calls)
- Modify: `custom_components/voip_stack/local_softphone_runtime.py:494-500` (`PendingLocalSource` leg construction)
- Test: `tests/test_local_source_runtime.py` (HA-venv file, owns `PendingLocalSource`); helper unit test in the fast-venv registry test file that already loads `call_registry` via `tests/support/module_loader` (grep `call_registry` in `tests/` to pick it; create `tests/test_call_registry_metadata.py` only if none fits)

**Interfaces:**
- Consumes: `SipInvite.received_via_trunk: bool` and `SipInvite.caller_uri: SipUri | None` (both already on the inbound invite objects).
- Produces: `inbound_from_metadata(invite) -> dict[str, str]` in `call_registry.py` — returns `{}` unless `received_via_trunk` and `caller_uri is not None`; otherwise `{"inbound_from_uri": str(invite.caller_uri)}`. Registry sessions carry metadata key `inbound_from_uri` = rendered inbound From URI. `PendingLocalSource` uses it when present.

- [ ] **Step 1: Write the failing helper test.**

For a fake invite built through the repo's module-loader pattern (`load_voip_stack_module`), assert:
trunk invite (`received_via_trunk=True`, `caller_uri` parsed from `sip:5550100@sip.tenant-a.example.test`) → `{"inbound_from_uri": "sip:5550100@sip.tenant-a.example.test"}`;
extension invite (`received_via_trunk=False`) → `{}`;
invite with `caller_uri=None` → `{}`.
Review Focus 2 case: `caller_uri` parsed from `sip:5550100%40backup@sip.tenant-a.example.test;transport=tcp` → stored text keeps the percent-encoded user and the parameter (assert exact `str()` equality with the rendered source).

- [ ] **Step 2: Run to verify it fails, then implement `inbound_from_metadata` in `call_registry.py`** per the Interfaces block (guard: `not getattr(invite, "received_via_trunk", False) or invite.caller_uri is None → {}`). Re-run: pass.

- [ ] **Step 3: Write the failing leg test in `tests/test_local_source_runtime.py`.**

Follow the file's existing `PendingLocalSource` construction pattern. Two cases:
(a) session whose metadata contains `inbound_from_uri: "sip:5550100@sip.tenant-a.example.test"` → constructed `PendingLocalSource.invite.caller_uri` renders byte-identical to that string;
(b) Review Focus 3: same construction with no metadata key → `caller_uri` equals the current `SipUri(caller.sip_uri_user, local_ip, sip_port)` result (regression guard).

- [ ] **Step 4: Run to verify (a) fails and (b) passes.**

Run: `/root/voip-stack/ha-voip-lab/.venv/bin/python -m pytest tests/test_local_source_runtime.py -k inbound_from -q`
Expected: (a) FAIL, (b) PASS.

- [ ] **Step 5: Implement the carry and the use.**

`endpoint_runtime.py`: add `**inbound_from_metadata(invite)` to the upsert call at 382 and `**inbound_from_metadata(initial)` at 769 (import from `.call_registry` alongside existing imports).
`local_softphone_runtime.py` leg construction: before `SipInvite(...)`, compute

```python
inbound_from = str(session.metadata.get("inbound_from_uri") or "")
caller_uri = (
    sip.parse_sip_uri(inbound_from) if inbound_from
    else sip.SipUri(caller.sip_uri_user, local_ip, sip_port)
)
```

and pass `caller_uri=caller_uri`. Leave `request_uri`, display name, tag handling, and everything else untouched. Audit `endpoint_runtime.py:633` (`caller_uri=parse_sip_uri(f"sip:{local_name...}@{local_ip}...")`): it is the HA-conference local-origin leg; keep it and record the audit result in the commit message body.

- [ ] **Step 6: Run tests.**

`.venv/bin/python -m pytest tests/test_call_registry*.py -q` (helper) and the HA-venv file from Step 4 — all pass.
`bash scripts/test_suite.sh fast` — green (retry transient downloads).
`bash scripts/test_suite.sh software-full` — green; this exercises the HA-loaded paths `endpoint_runtime`/`local_softphone_runtime` changed here.

- [ ] **Step 7: Commit.**

```bash
git add custom_components/voip_stack/call_registry.py custom_components/voip_stack/endpoint_runtime.py custom_components/voip_stack/local_softphone_runtime.py tests/
git commit -m "fix(fork): keep the inbound trunk caller identity verbatim on legs

A leg forked from an inbound trunk call now renders From from the stored
inbound From URI instead of the caller user plus the local socket, so the
callee can redial and missed-call entries resolve the right tenant.
Locally originated legs keep their current From."
```

---

### Task 3: edge in-dialog loop guard and overlay egress (voip-edge)

Repo `/root/voip-edge` (branch `main`). Config-only change in `route[WITHINDLG]` plus its static guard test. No container recreate here (operator gate, Global Constraints).

**Files:**
- Modify: `kamailio/misc.cfg` (`route[WITHINDLG]`, lines 3-44)
- Test: `tests/unit/test_config_guards.py` (extend in the file's `_read text + assert` style)

**Interfaces:**
- Consumes: existing `route[TUNNEL_SOCKET]` (misc.cfg:47-53, pins `tcp:WGIP:5060` or `WGSOCK` by `$du` transport), macros `PUBIP`, `WGIP`, `sl_send_reply`, `t_check_trans`.
- Produces: `WITHINDLG` answers `482 Loop Detected` + `L_WARN` xlog for in-dialog requests whose resolved `$du` is one of our own listen addresses (except an ACK for a local transaction); overlay-bound relays (`$du` host in `100.64.0.0/10`) leave from the WG socket via `route(TUNNEL_SOCKET)`.

- [ ] **Step 1: Write the failing static guards in `tests/unit/test_config_guards.py`.**

New test `test_indialog_relays_guard_loops_and_pin_the_overlay`:

```python
misc = (ROOT / "kamailio" / "misc.cfg").read_text()
assert '"482", "Loop Detected"' in misc
assert "Loop Detected" in misc.split("t_relay")[0]  # guard precedes relay
assert 'xlog("L_WARN"' in misc
assert 'route(TUNNEL_SOCKET)' in misc  # inside WITHINDLG, conditional
assert '100\\.64\\.' in misc            # overlay range, not blanket pinning
assert '!is_method("ACK") || !t_check_trans()' in misc  # ACK exemption
```

Also assert WITHINDLG still contains `"Unknown Dialog Source"`.

- [ ] **Step 2: Run to verify failure.**

Run: `cd /root/voip-edge && .venv/bin/python -m pytest tests/unit/test_config_guards.py -q`
Expected: FAIL on the new test only.

- [ ] **Step 3: Edit `route[WITHINDLG]` in `kamailio/misc.cfg`.**

Inside `if (loose_route()) {`, immediately at the top of the block (before the BYE/rtpengine handling), add:

```
	# Never relay a request back to ourselves. An ACK for a local
	# transaction is TM's to forward; everything else self-bound is a loop.
	if (!is_method("ACK") || !t_check_trans()) {
		$var(loopre) = "[@:]" + PUBIP + "|[@:]" + WGIP;
		if ($du != $null && $du =~ $var(loopre)) {
			xlog("L_WARN", "in-dialog loop: $rm $ci resolved to self ($du)\n");
			sl_send_reply("482", "Loop Detected");
			exit;
		}
	}
	# Overlay-bound flows must leave through the WireGuard socket.
	if ($du =~ "[@:]100\.64\.") {
		route(TUNNEL_SOCKET);
	}
```

Review Focus 4 is covered by the `[@:]` character class: it matches `sip:PUBIP` (no user part) as well as `sip:user@PUBIP`. Review Focus 5: pinning keys on the destination range only; `route(TUNNEL_SOCKET)` itself chooses TCP socket vs `WGSOCK` by `$du` transport, matching how `invite.cfg` delivers flows. Watch the known quirks: no trailing `#` comments on `#!define` lines; macros don't expand inside quoted route strings — the regex fragments above are built at runtime by string concatenation or are literals; if `kamailio -c` rejects `=~ $var(loopre)` or the `+` concatenation, fall back to two inline literal comparisons against the deployment values and note it in the commit body. Keep edits minimal; do not restructure the rest of the route.

- [ ] **Step 4: Verify config parses.**

Run a throwaway container of the same official image with the edited tree mounted (no live container touched):

```bash
docker run --rm -v /root/voip-edge/kamailio:/etc/kamailio/voip-edge:ro \
  <kamailio image tag from compose.yaml> \
  kamailio -c -C 'include_file="/etc/kamailio/voip-edge/kamailio.cfg"' -L /usr/lib/kamailio/modules || \
docker run --rm -v /root/voip-edge/kamailio:/etc/kamailio/voip-edge:ro \
  <kamailio image tag> kamailio -c -f /etc/kamailio/voip-edge/kamailio.cfg
```

Use the exact invocation the container's entrypoint (`kamailio/entrypoint.sh`) performs, minus startup; pass `-A` defines for `PUBIP`/`WGIP` if the entrypoint supplies runtime values.
Expected: "cfg file parsed, scripts defined" / exit 0.

- [ ] **Step 5: Run edge unit tests.**

Run: `cd /root/voip-edge && .venv/bin/python -m pytest tests/unit -q`
Expected: all pass, including the new guard test. The privacy pre-commit hook must stay green (no new sensitive strings; regex uses placeholders only).

- [ ] **Step 6: Commit.**

```bash
git add kamailio/misc.cfg tests/unit/test_config_guards.py
git commit -m "fix(routing): refuse in-dialog self-relay and pin overlay egress

Relaying a loose-routed request back to a public or WireGuard listen
address re-entered this proxy and died on the regcache source check; it
now gets 482 Loop Detected with a warning log. In-dialog relays toward
the overlay range leave from the WG socket so reliable provisional
targets reachable only over the tunnel actually arrive."
```

---

### Task 4: live verification (operator-gated)

**Files:** none (Homer + logs).

**Interfaces:**
- Consumes: voip-stack fix branch deployed to the HA test environment; voip-edge `main` with Task 3 merged and recreated in a quiet window (regcache loss accepted; mobile1 re-registers).
- Produces: evidence the spec's Verification section demands.

- [ ] **Step 1: Operator places one inter-home call** (caller at tenant A dials a member of tenant B whose mobile1 rings via the local bridge). Record the SIP Call-ID.

- [ ] **Step 2: Check the call in Homer** (HOMER MCP tools; search by Call-ID within the last hour): the 180 carries `Contact:`; a PRACK follows targeting that contact; `200` answers the PRACK; `200 OK` completes the INVITE; BYE ends the dialog with no `482`/`403 Unknown Dialog Source` on any in-dialog request.

- [ ] **Step 3: Check voip-stack logs** (`ha.log` no longer exists; use the live HA container's log): call state reaches `connected`; no `prack_failed` / `prack_timeout`.

- [ ] **Step 4: Check the fork leg From** in the captured INVITE toward mobile1: From URI byte-identical to the inbound From the edge saw. Redial from mobile1 and confirm it resolves the correct tenant (this is the deferred check that decides whether the edge From rewrite needs its own fix later).

- [ ] **Step 5: RTP:** confirm rtpengine media for both legs (Homer or `rtpengine` console), audio both directions.

- [ ] **Step 6: Report pass/fail per item to the user; on full pass, offer branch push/PR** (GitHub connectivity is intermittent — retry pushes).
