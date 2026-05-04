# Ingest Pipeline (Backend + Confluent SR/Kafka)

Authoritative checklist for accepting SDK artifacts (evidence NDJSON, output JSON, manifest JSON) and producing them to Kafka with Confluent Schema Registry validation. Aligns with SDK Draft-07 schemas and integrity contracts.

## Scope and Goals
- Accept only schema-conformant SDK payloads; fail closed on drift.
- Enforce idempotency and ordering (evidence first).
- Preserve integrity chain; store raw payloads for replay.
- Produce synchronously to Kafka (acks=all, idempotent).

## Endpoints and Inputs
- POST `/v1/ingest/evidence` (Content-Type: application/x-ndjson)
- POST `/v1/ingest/output` (application/json)
- POST `/v1/ingest/manifest` (application/json)
- Headers:
  - `Authorization: Bearer <session_token>` (from handshake)
  - `Idempotency-Key`: evidence/output → session_id (ULID); manifest → context.session_id (top-level session_id, if present, must match)
  - `Content-Type` as above

## Validation Flow (per request)
1) **Auth + size guard**: Reject 401/403 if token invalid; 413 if body exceeds limits; enforce NDJSON non-empty lines.  
2) **Structural parse**: evidence → parse lines to JSON; output/manifest → JSON parse.  
3) **Idempotency**: header must equal required session_id; duplicates short-circuit with `already_exists` (no re-produce).  
4) **Schema validation (SR first)**: Validate against Confluent SR subject (cloud) with cached schema_id; if SR unreachable or schema_id mismatch vs local canonical, fail closed. Subjects (recommended):  
   - evidence: `setorra.evidence.event.v1`  
   - output: `setorra.output.summary.v1`  
   - manifest: `setorra.manifest.v1`  
   Use Draft-07 validators with FormatChecker for RFC3339Z, ULID (26 upper), semver, sha256 hex, MIME, env oneOf (string|object|null), additionalProperties=false with x-meta escape.  
5) **Semantic**: evidence-first gate; session consistency; manifest contains rules (objects includes evidence.jsonl/output.json with bytes>0, lines>0 for evidence); no chain gaps.  
6) **Integrity**: per NDJSON line, recompute hash over the raw event (pre-normalization): remove `integrity.event_hash` and `integrity.signature`, keep `prev_hash`, JSON dump sorted keys + compact separators, UTF-8, SHA-256 hex; compare to provided hash; check prev_hash linkage and monotonic event_index.  
7) **Persistence**: per-session lock; write raw body to `*.tmp`, fsync file + dir, atomic rename; clean tmp on failure.  
8) **Kafka produce**: synchronous produce only after persistence; topics `setorra.evidence.v1`, `setorra.output.v1`, `setorra.manifest.v1`; key=session_id; producer config `acks=all`, `enable.idempotence=true`, `max.in.flight.requests.per.connection=1`, `compression=zstd`. Respond 202/200 on broker ack.

## Error Model
- 401/403 auth; 413 size; 422 schema/idempotency/semantic with payload `{error, part, schema, schema_id, idempotency_key, session_id, violations[{pointer,line?,message,expected,actual}]}`.  
- Integrity mismatch: 422 `event_hash_mismatch` with line/provided/computed.  
- Duplicates: 202/200 with `already_exists=true`, no re-produce.  
- Consistent JSON pointers; include NDJSON line numbers where applicable.

## Observability
- Stage logs: received → structural → schema → semantic → integrity → persistence → produced. Include session_id, artifact_type, schema_subject/schema_id, timings. Cap SR cache refresh logs. Log only first integrity mismatch per request.

## Testing Matrix (must-have)
- Happy paths: all three artifacts, correct headers, ULID/date-time/semver/hash patterns, env oneOf variants.  
- Idempotency: missing header, mismatched header, suffix (`:evidence`), duplicate POST returns already_exists without new write/produce.  
- Schema/format failures: bad ULID, bad RFC3339Z, bad semver, bad sha256, bad MIME, extra fields (additionalProperties), manifest missing `produced_at`/`hash_alg`/objects/contains.  
- NDJSON hygiene: empty line, malformed JSON line, zero events.  
- Integrity: altered field, bad event_hash, bad prev_hash/event_index, missing agent.started/finished.  
- Ordering: manifest/output before evidence → reject.  
- Kafka path: broker down → fail closed; SR drift → fail closed; cache TTL respected.  
- Atomicity: abort mid-write leaves no final file; per-session lock prevents ENOENT races; concurrent duplicate requests → one write, one already_exists.

## Data Shapes (canonical expectations)
- `schema_version`: evidence/output `"1.3"`, manifest `"manifest-v1"`.  
- `session_id`: ULID uppercase; manifest uses `context.session_id` as source of truth.  
- `context.env`: string | object | null.  
- Hashes: sha256 lowercase hex; `hash_alg=sha256-v1` in manifest.  
- Manifest `objects[]`: must contain entries for `evidence.jsonl` and `output.json` with required `content_type`, `sha256`, `bytes>0`, and `lines>0` for evidence.
