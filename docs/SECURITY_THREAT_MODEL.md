# REACH — Security Threat Model

Every external page is treated as hostile input. The defences below are
implemented in code and covered by the 40 tests in
`tests/test_security.py`.

## Trust boundaries

```
 trusted                             │ untrusted
─────────────────────────────────────┼───────────────────────────────────────
 REACH's own recording catalog       │ every fetched webpage
 user input in the campaign wizard   │ every search result
 REACH's own database                │ every provider API response
 environment configuration           │ every inbound webhook
```

Nothing crosses left across that line without passing a named guard.

## T1 — Prompt injection / agent manipulation

**Threat.** A page contains text designed to redirect an automated agent:
"ignore all previous instructions", role reassignment, secret exfiltration,
hidden concealment instructions, or fake chat-role tags.

**Controls.**
* Extraction is **deterministic** (`reach/extractor.py`) — regex and keyword
  rules over sanitized text. There is no model in the discovery path, so page
  text has no channel through which to act as an instruction.
* `reach/sanitizer.py` drops `script`, `style`, `noscript`, `template`,
  `iframe`, `object`, `embed`, `svg`, `canvas`, `form` subtrees, HTML comments,
  and elements hidden by `display:none`, `visibility:hidden`, `opacity:0`,
  `hidden`, `aria-hidden`, `sr-only` and friends.
* Nine injection patterns are scanned for and **reported**, never obeyed;
  `injection_attempt` is a 35-point risk signal.
* Extraction is schema-constrained: only declared fields are produced, so page
  text cannot introduce a new field, a tool request or a policy change.
* Every extracted value is tagged with its source document.
* If a model is connected later, `sanitizer.as_data_block` fences source text
  and `firewall.guard_language_model` gates the call.

**Verified by.** `test_malicious_page_instructions_are_data_not_commands`,
`test_injected_page_raises_risk_and_suppresses_the_honeypot`,
`test_scripts_styles_and_comments_are_dropped`.

## T2 — Honeypot contacts

**Threat.** An address visible only to a crawler, planted to identify and
blocklist automated senders.

**Control.** Addresses found only in hidden markup are classified
`PROHIBITED_PRIVATE_INFORMATION`, recorded with `HIDDEN_MARKUP` evidence, and
**globally suppressed** on discovery. They never appear as contacts.

**Verified by.** `test_hidden_honeypot_address_is_never_a_visible_contact`.

## T3 — SSRF, DNS rebinding and metadata theft

**Threat.** A URL or redirect that points at loopback, a private range,
link-local space or a cloud metadata endpoint.

**Controls** (`reach/netguard.py`):
* Scheme allowlist — `http` and `https` only.
* Port allowlist — 80, 443, 8080, 8443.
* Hostname blocklist — `localhost`, `metadata.google.internal`, `instance-data`,
  and the `.local`, `.internal`, `.svc`, `.onion` suffixes.
* Literal IPs in the URL are checked before any DNS work.
* DNS is resolved up front and **every** returned address is validated. A name
  resolving to one public and one private address is refused outright.
* The connection is made to the pre-validated IP with the original hostname
  carried in the `Host` header and TLS SNI — closing the rebinding window
  between check and connect.
* Cloud metadata addresses are blocked explicitly: `169.254.169.254`,
  `169.254.170.2`, `100.100.100.200`, `192.0.0.192`, `fd00:ec2::254`.
* Redirects are followed manually, at most 3, and **each hop is re-validated
  from scratch**; loops are detected.

**Verified by.** 13 parametrized SSRF cases plus
`test_redirect_into_a_private_range_is_blocked`,
`test_dns_rebinding_is_blocked_by_pre_resolution`,
`test_mixed_public_and_private_resolution_is_refused`.

## T4 — Malicious files and resource exhaustion

**Controls.** Response size capped at 2 MB, enforced while reading rather than
after. MIME allowlist (HTML, plain text, XML/RSS/Atom, JSON). Extension
blocklist covering executables, libraries, scripts and archives.
`Content-Disposition: attachment` responses are refused. 10-second timeout.
**Nothing downloaded is ever executed**, and no browser extension is ever loaded.

**Verified by.** `test_oversized_response_is_blocked`,
`test_executable_download_is_blocked`, six archive/script extension cases,
`test_disallowed_mime_type_is_blocked`.

## T5 — Crawl abuse

**Controls.** robots.txt is fetched and honoured per origin, including
`Crawl-delay`. Per-domain request pacing. Per-campaign domain budgets. The
crawler identifies itself honestly. Per-provider kill switches and a global
emergency stop. Every fetch decision — allowed or refused, with its reason code —
is written to `source_document`.

**Verified by.** `test_robots_disallowed_path_is_not_crawled`,
`test_robots_allowed_path_on_the_same_host_is_crawled`.

## T6 — Login walls, CAPTCHAs and paywalls

**Control.** Detected, never bypassed. `requires_login` and `requires_captcha`
route the opportunity to the NEEDS YOU queue with the reason stated. Headless
browser automation behind a login is a Phase One non-goal with a feature flag
set to `False`.

## T7 — Data-use violation (the AI firewall)

**Threat.** Restricted provider content reaching a language model, a vector
index or model training.

**Control.** `reach/firewall.py` enforces `SourceUsagePolicy` **before** any
model call is constructed. Every contributing source must permit the use; one
forbidden source blocks the whole call, because a model cannot be trusted to
ignore part of its context. An unregistered source is blocked by default. Secret
patterns (bearer tokens, API keys, AWS keys, GitHub tokens, cookies, private
keys, OAuth tokens) are scanned for and refused.

**Verified by.** `test_firewall_blocks_spotify_content_from_model_and_embeddings`,
`test_firewall_refuses_secrets_in_model_context`,
`test_firewall_blocks_unknown_source_by_default`.
This guard caught a real defect during the build: a fact labelled with the
unregistered source `"evidence"` was correctly refused.

## T8 — Credential and contact exposure

**Controls.** Secrets are read from the environment only and never rendered —
templates receive booleans and missing-variable *names*. Contact values are
Fernet-encrypted at rest with a keyed HMAC for dedup and suppression; lists show
a redacted preview. `contacts.reveal` is the single decryption point and is
audited on every call. When `REACH_ENCRYPTION_KEY` is unset the process uses an
ephemeral key and says so on Provider Health — it never falls back to plaintext.

## T9 — Privilege escalation

**Controls.** `rbac.PERMISSIONS` is a module constant; no runtime path adds to
it, so no model or agent can grant itself a permission. Approval, sending,
suppression edits, provider configuration, kill switches and spend approval are
each gated separately. `autopilot.enable` is `OWNER`-only.

**Verified by.** `test_view_only_role_cannot_approve_or_send`,
`test_reviewer_can_approve_but_not_send`, `test_only_owner_may_enable_autopilot`.

## T10 — Approval tampering

**Threat.** A message is approved, then a material field changes before it is
sent.

**Control.** The approval binds to a SHA-256 hash over recipient hash, subject,
body, links, attachments, cost and sending identity. At send time the hash is
**re-derived from the live payload** and compared; a mismatch raises
`ApprovalInvalid` and nothing is sent. Editing a draft actively invalidates its
approvals with a recorded reason.

**Verified by.** `test_editing_a_draft_invalidates_its_approval`,
`test_send_refuses_an_invalidated_approval`,
`test_approved_send_delivers_exactly_the_approved_payload`.

## T11 — Webhook forgery

**Control.** `/reach/webhooks/email` requires the `X-Reach-Signature` header to
match `REACH_EMAIL_WEBHOOK_SECRET`; unsigned requests get 401 and no state
change.

**Verified by.** `test_webhook_rejects_an_unsigned_request`.

## T12 — Audit tampering

**Control.** Each audit event stores the previous event's hash and its own hash
over `(prev, seq, tenant, actor, action, entity, payload, timestamp)`.
`audit.verify_chain` recomputes the chain and reports the first broken sequence
number. Settings shows the live verification state.

**Verified by.** `test_audit_captures_external_actions_and_the_chain_verifies`,
`test_tampering_with_an_audit_row_breaks_the_chain`.

## T13 — Cross-tenant leakage

**Control.** Every scoped query filters on `tenant_id`. Relationship notes and
listings are tenant-private. Suppression is deliberately the one thing that
crosses tenants — a global opt-out must be honoured by every account.

**Verified by.** `test_private_notes_are_not_visible_to_another_tenant`,
`test_suppression_is_global_across_tenants`.

## Residual risks

* **SQLite concurrency.** Fine for a single-process deployment. A multi-worker
  deployment should move to PostgreSQL; the SQL is portable and the job runner
  already claims rows with a conditional update.
* **SPF policy contents.** REACH verifies that an SPF record exists and reads
  it, but does not evaluate whether your sending provider is included in it. A
  published-but-wrong policy passes the check.
* **eTLD+1 heuristic.** `netguard.registrable_domain` handles the common
  two-part public suffixes rather than the full Public Suffix List. Impact is
  limited to dedup and budget granularity, not to security decisions.
* **Live provider calls.** Implemented and unit-tested against transports, but
  never exercised against a real endpoint in this environment.
