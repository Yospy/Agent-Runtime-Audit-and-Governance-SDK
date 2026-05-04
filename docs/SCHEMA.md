# Setorra SDK Schemas (v1.3)

For backend ingestion, see `docs/BACKEND_INGESTION_PLAN.md` for transport, validation, and Avro encoding guidance that complements the schema details below.

Schema version **1.3** guarantees that every agent run — whether it completes or
fails — produces the same evidence footprint. All payloads are redacted before
storage.

## Storage Layout

- Local (default): `evidence/<session_id>/`
  - `evidence.jsonl` — event stream
  - `output.json` — run summary
  - `manifest.json` — commit marker with sizes/hashes/chain summary
## Output Summary (`evidence/<session_id>/output.json`)

- `schema_version`: always `"1.3"`
- `session_id`
- `timestamp`: ISO-8601 UTC finish timestamp
- `content`: redacted final output string
- `prompts`: prompt hash object containing `system` and `user`
- `tools_used`: array of
  `{ "name", "count", "total_duration_ms", "last_input_hash", "last_output_hash" }`
- `integrity`: `{ "first_event_hash", "final_event_hash", "chain_length" }`

### Errors (additive in 1.3)

- `errors`: array of tool error summaries. Each item contains:
  - `event_hash`: hash of the corresponding evidence event (trace pointer)
  - `event_index`: event index within the run
  - `timestamp`: ISO-8601 UTC timestamp
  - `tool`: tool name (from parameters_redacted.name)
  - `error_type`: exception type (string)
  - `message_preview`: redacted error preview (privacy-safe)
  - `auth_failed`: boolean (true when the error is classified as an auth/permission failure)
  - `error_code` (optional): numeric code when available (401 or 403)
  - `reason_code` (optional): one of `AUTH_UNAUTHENTICATED | AUTH_FORBIDDEN | AUTH_EXPIRED | AUTH_SCOPE_INSUFFICIENT | AUTH_SMTP_535 | AUTH_GENERIC`
- `errors_truncated`: integer count of additional errors not included when the list is capped for size.
- `actions`: action summary with total requested actions, final status counts, and final status records containing action ID, system, hashes, idempotency key, and result hash when execution completed.

## Evidence Stream (`evidence/<session_id>/evidence.jsonl`)

Each line is a JSON object with the following keys:

- `schema_version`: `"1.3"`
- `session_id`: stable run identifier
- `event_index`: sequential integer (0-based)
- `timestamp`: ISO-8601 UTC string
- `agent_name`, `agent_version`
- `event_type`: taxonomy such as `agent.started`, `session.manifest`,
  `prompt.capture`, `user.consent`, `plan.create`, `plan.refine`, `plan.adopt`, `plan.abandon`, `tool.start`, `tool.end`, `llm.start`,
  `llm.end`, `reasoning`, `policy.decision`, `policy.approval`,
  `agent.outcome`, `agent.finished`
- `parameters_redacted`: masked payload for the event
- `privacy_flags`: array of detected PII categories. Common values: `email`, `phone`, `secret`, `id`, `card`, `iban`, `routing`, `acct`, `ssn`, `jwt`, `bearer`, `pem`, `token`, `ip`, `dob`, `address`. The set may grow as detectors are extended; additions are backward‑compatible.
- `redacted_fields`: array of field paths that were masked
- `context`: optional metadata (model id, prompt hashes, environment, policy)
  - When the SDK links to a backend via handshake, `context.organization = { "org_id", "org_name"?, "key_id_prefix"? }` is included on every event. This field is optional and absent in offline/local-only runs.
  - When planning capture is enabled, `context.plan = { "plan_id", "version" }` may be present on action events.
- `execution`:
  `{ "status", "latency_ms"?, "input_hash"?, "output_hash"?,
     "reasoning_hash"?, "reasoning_preview"?, "error_type"?,
     "error_preview"?, "linked_event_hash"? }`
  - Additive in 1.3 (only for `tool.error` events):
    - `auth_failed`: boolean, present and `true` when the error is classified as authentication/permission failure.
    - `error_code`: number, present when confidently detected (401 or 403).
    - `reason_code`: stable string when classified; values as listed above.
- `policy`: `{ "version", "decision", "intent_hash"?, "rule"?, "bundle_hash"?, "capability_scope"?, "fallback_mode"?, "fallback_trigger"? }`
- `approval`: null or `{ "approver_id", "approved_at", "method", "signature"?, "metadata"? }`
-  - When set by the integrator, `approval.metadata.approver_email` may contain the approver’s work email (cleartext) for internal audit.
-  - Treat as PII: retain only in internal evidence; omit or hash in external/vendor exports per your privacy policy.
- `integrity`: `{ "prev_hash", "event_hash", "signature"? }`

### Mandatory Event Sequence

1. `agent.started`
2. `session.manifest` (environment, policy, signing metadata)
3. `prompt.capture` (system/user prompts, hashes)
4. Zero or more `user.consent` events
5. Zero or more reasoning / LLM / tool events
6. `agent.outcome` (success/failure with linked hash)
7. `agent.finished`

Additional governance events (e.g., `policy.violation`, `policy.approval`) may
appear when available.

## Hashing & Signatures

- Canonicalize JSON (sorted keys, compact separators) before hashing.
- `event_hash`: SHA-256 of the canonical object excluding `event_hash` and
  `signature` fields.
- `signature`: optional HMAC-SHA256; present only when a signing key is
  configured. Null otherwise.
- Events are linked via `prev_hash` to form a tamper-evident chain.
