Setorra SDK — Changelog

Released — Schema 1.3 (Auth/Tool Errors)
- Added: Authentication/permission failure classification for `tool.error` events (privacy‑safe; additive-only).
  - Evidence: `execution.auth_failed` (boolean), `execution.error_code` (401/403), `execution.reason_code` (stable enum) on `tool.error` when classified.
  - Output summary: new `errors[]` section listing tool errors with trace pointers (`event_hash`, `event_index`), redacted `message_preview`, `auth_failed`, `error_code?`, and `reason_code?`. Includes `errors_truncated` when capped.
- Docs: `docs/SCHEMA.md` updated to v1.3 with the Errors section and additive execution fields.
- Harness: Added `harness_tests/auth_failure_testing/` with collector and adapter paths; summary artifacts under `harness_results/`.
- Tests: classifier unit tests and harness cases cover HTTP 401/403, OAuth invalid_grant/insufficient scopes, SMTP 535, keyword fallbacks, negatives, localization safe-mode, and overflow.

Unreleased (Schema 1.2)
- Added: Redaction engine upgrade (Python SDK only)
  - New detectors with validation/context gating: PAN/Luhn → [card], IBAN/mod‑97 → [iban], ABA checksum (context) → [routing], account (context) → [acct], SSN (context) → [ssn], JWT → [jwt], Authorization Bearer → [bearer], PEM → [pem], vendor/cloud tokens → [token], IPv4 (standard), IPv6/DOB/Address (strict+context). Existing detectors kept: email, phone (US/E.164), OpenAI/API keys, AWS ID.
  - Env toggles: `SETORRA_REDACTION_LEVEL=standard|strict`, `SETORRA_REDACTION_DISABLE`, `SETORRA_REDACTION_ENABLE`.
  - Performance: precompiled regex; ordered registry; validators for PAN/IBAN/ABA; phone pattern tightened to avoid false positives.
  - Backward compatible: same `redact(obj)` API; placeholders and privacy semantics preserved; only additive flags.
- Docs: `docs/REDACTION.md` with detector list, placeholders, env, and evidence mapping.
- Tests: targeted PII harness under `PII testing/` (routing/phone/CVV, PAN/IBAN/ABA, JSON‑quoted secrets/Bearer/JWT, email/phone/IP, stress lines) with standard/strict summaries.
- Added: Planning capture events (`plan.create`, `plan.refine`, `plan.adopt`, `plan.abandon`).
  - Gated by `SETORRA_CAPTURE_PLANS` (default: off).
  - Redacted, clipped previews for summaries/rationales; canonical hashing for `plan_hash`.
  - Optional `context.plan = { plan_id, version }` on subsequent action events.
- Added: Async/thread-safe planning context propagation via `setorra.context.get_plan_id/set_plan_id`.
- Added: Harness `planning_harness.py` and summary artifact `harness_results/planning_harness_summary.json`.
- Added (TypeScript/Node): Planning capture in package `setorra-ai` with session API parity.
  - Evidence JSONL + output JSON per session written to `SETORRA_STORAGE_DIR`.
  - Single-folder test suite `ts_planning_tests/` produces `summary.json` and per-scenario artifacts.
- Changed: `schema.SCHEMA_VERSION` → `1.2` (additive-only; backward-compatible with prior events).
- Docs: README planning section with env flag and usage snippet.

Notes
- Enforcement semantics unchanged: Guardrails/policy still apply at action boundaries.
- Privacy posture unchanged: No raw PII; redaction before persistence; signatures and hash links preserved.
