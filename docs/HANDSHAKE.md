# Setorra Local Control Plane — Handshake Spec (Emulator)

Purpose
- Provide backend engineers a minimal, production-aligned local control plane to test: API key → org_id mapping, session token issuance, and ingest verification.
- SDK expectation: Developer supplies only `api_key`; SDK handles handshake, stamps `org_id` on all events, loads policies/guardrails, and (optionally) mirrors events to ingest.

Scope
- Dev/local only. No external dependencies required. Replace with real control plane/ingest later without changing SDK behavior.

Overview
- Components:
  - Auth/Handshake endpoint: maps `api_key` → `{ org_id, key_id }`, issues short-lived session token (JWT-HS256 for dev).
  - Policy/Guardrail delivery: inline payload or local file paths included in handshake response.
  - Ingest endpoint (optional): verifies Bearer token and `org_id` in event payload; writes accepted events to per-org NDJSON file.
  - JSONL key store: hashed API keys with metadata; no raw API keys stored on disk.

Directory Layout (suggested)
- `tools/local_backend/`
  - `server.py` — stdlib HTTP server (no frameworks) implementing endpoints below
  - `keys.jsonl` — API key registry (one JSON object per line)
  - `config.json` — salts and token secrets (by ID), server port, default `stream_dir`
  - `ingest/` — created at runtime; per-org NDJSON files (gitignored)

API Surface

1) POST /v1/auth/handshake
- Headers
  - `Authorization: Bearer <api_key>`
- Request body (optional)
  ```json
  { "env": "local", "sdk_version": "0.0.0-dev" }
  ```
- Processing
  - Extract `<api_key>` from `Authorization` header.
  - Compute `key_hash = HMAC-SHA256(salt, api_key)` using the salt referenced by the JSONL row’s `salt_id`.
  - Look up a row in `keys.jsonl` where `key_hash` matches and `status == "active"`.
  - Issue a short-lived JWT (HS256 using `token_secrets[token_secret_id]`).
    - Header: `{ "alg": "HS256", "typ": "JWT", "kid": "<token_secret_id>" }`
    - Claims (example):
      ```json
      {
        "iss": "setorra-local",
        "aud": "setorra-ingest-local",
        "sub": "<key_id>",
        "org_id": "org_12345",
        "key_id": "key_abcd1234",
        "env": "local",
        "scopes": ["ingest", "policy.read"],
        "iat": 1728996400,
        "exp": 1729000000,
        "jti": "ulid_01HX...",
        "v": 1
      }
      ```
- Response (200)
  ```json
  {
    "org_id": "org_12345",
    "org_name": "Acme Corp",
    "key_id": "key_abcd1234",
    "session_token": "<jwt>",
    "policy": { "inline": { /* bundle JSON */ } } ,
    "guardrails": { "inline": { /* bundle JSON */ } },
    "stream_endpoint": "http://localhost:8081",
    "expires_in": 3600
  }
  ```
- Errors
  - 401 `{ "error": "invalid_api_key", "key_id_prefix": "key_abcd" }`
  - 403 `{ "error": "revoked_key", "key_id": "key_abcd1234" }`
  - 429 `{ "error": "rate_limited" }`

2) POST /v1/ingest (optional but recommended)
- Purpose: validate tokens and demonstrate org-based routing.
- Headers
  - `Authorization: Bearer <session_token>`
- Body
  - Single JSON event or NDJSON line conforming to SDK evidence event shape, including:
    ```json
    { "context": { "organization": { "org_id": "org_12345" } }, ... }
    ```
- Processing
  - Verify JWT (`exp`, `aud`, `iss`, `scopes` contains `ingest`).
  - Require `token.org_id == event.context.organization.org_id`.
  - On success, append event to `ingest/org_org_12345.jsonl` (create if missing).
- Responses
  - 202 `{ "status": "accepted" }`
  - 400 `{ "error": "invalid_event" }`
  - 401/403 `{ "error": "invalid_token" | "org_mismatch" }`

3) GET /healthz
- Response: 200 `{ "ok": true }`

JSONL Key Store (keys.jsonl)
- One JSON object per line. No raw API keys are stored.
- Fields (per row)
  - `key_id` (string): audit-safe identifier (e.g., `key_abcd1234`).
  - `key_hash` (string): hex HMAC-SHA256 of the raw API key with a per-row or per-bucket salt.
  - `salt_id` (string): references a salt value in `config.json`.
  - `org_id` (string): e.g., `org_12345`.
  - `scopes` (array): e.g., `["ingest", "policy.read"]`.
  - `env` (string): `local` | `dev` | `prod` (for claim stamping).
  - `status` (string): `active` | `revoked`.
  - `policy_inline` (object, optional): JSON policy bundle. Mutually exclusive with `policy_path`.
  - `policy_path` (string, optional): path to policy bundle on disk.
  - `guardrail_inline` (object, optional): JSON guardrail bundle.
  - `guardrail_path` (string, optional): path to guardrail bundle on disk.
  - `token_secret_id` (string): references signing secret in `config.json`.
  - `stream_dir` (string, optional): directory for per-org NDJSON files (defaults from config).

Example lines
```jsonl
{"key_id":"key_abcd1234","key_hash":"5d2c...","salt_id":"s1","org_id":"org_12345","scopes":["ingest","policy.read"],"env":"local","status":"active","policy_path":"policies/finance.json","guardrail_path":"guardrails/support.yaml","token_secret_id":"dev1","stream_dir":"tools/local_backend/ingest"}
{"key_id":"key_efgh5678","key_hash":"91ba...","salt_id":"s1","org_id":"org_12345","scopes":["ingest","policy.read"],"env":"local","status":"revoked","policy_inline":{"version":"2024-06-15","sdk":{"decision":"allow","fail_mode":"fail-open"}},"guardrail_inline":{},"token_secret_id":"dev1"}
```

config.json
```json
{
  "port": 8081,
  "salts": { "s1": "base64:7pJBI..." },
  "token_secrets": { "dev1": "base64:0E4m..." },
  "default_stream_dir": "tools/local_backend/ingest"
}
```

Token (JWT) Requirements (dev)
- Algorithm: HS256 with secret from `token_secrets[token_secret_id]`.
- Header: include `kid` for rotation testing.
- Claims: `iss`, `aud`, `sub`=`key_id`, `org_id`, `key_id`, `env`, `scopes`(array), `iat`, `exp` (≤ 1h), `jti`, `v`.

Handshake Flow (end-to-end)
1. SDK calls `POST /v1/auth/handshake` with API key.
2. Backend hashes the key with `salt_id` and finds the matching row in `keys.jsonl`.
3. Backend issues JWT and returns `{ org_id, key_id, session_token, policy/guardrails, stream_endpoint }`.
4. SDK caches `org_id` and `session_token` in memory, stamps `context.organization.org_id` on every event, loads policies/guardrails.
5. SDK writes local artifacts; if configured, mirrors events to `POST /v1/ingest` using the session token.

Error Semantics
- Invalid key → 401; SDK continues offline (cached bundles) with no ingest.
- Revoked key → 403; SDK continues offline.
- Token expired → 401; SDK re-handshakes on next mirror attempt.
- Org mismatch at ingest → 403; event rejected; SDK preserves local evidence and logs mirror failure.

Privacy & Logging (dev)
- Do not log raw API keys or tokens. Log only `key_id` (or 6–8 char prefix).
- Avoid logging event bodies; if necessary for debugging, redact sensitive fields first.

Security Notes (dev)
- Rate-limit handshake (e.g., token bucket) and add small jitter to errors to reduce key enumeration risk.
- Use TLS for remote testing; for localhost, plain HTTP is acceptable.
- Keep secrets and salts out of VCS; use `.gitignore` for `config.json` and `ingest/`.

Acceptance Criteria
- Handshake returns `org_id`, `key_id`, `session_token`, and policy/guardrails; SDK stamps `org_id` on all events.
- Ingest accepts events with matching `org_id` and valid token and writes to per-org NDJSON.
- No raw API keys/tokens appear in logs or files.

Manual Test Snippets (curl)
```bash
# Handshake
curl -s -X POST \
  -H "Authorization: Bearer agt_live_XXXX" \
  -H "Content-Type: application/json" \
  -d '{"env":"local"}' \
  http://localhost:8081/v1/auth/handshake | jq

# Ingest (example event)
TOKEN="<paste session_token>"
cat > /tmp/event.json <<'EOF'
{"context":{"organization":{"org_id":"org_12345"}},"event_type":"agent.started","parameters_redacted":{}}
EOF
curl -s -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @/tmp/event.json \
  http://localhost:8081/v1/ingest | jq
```

Production Migration Notes
- Replace JSONL with a database and key management (KMS/HSM for token signing).
- Keep endpoint shapes and claims the same to avoid SDK changes.
- Add SSO-bound approvals and remote bundle distribution when moving beyond MVP.
