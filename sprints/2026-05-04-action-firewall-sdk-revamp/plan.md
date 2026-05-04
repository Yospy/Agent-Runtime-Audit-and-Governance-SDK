# Action Firewall SDK Revamp Sprint

## Scope

Revamp Setorra from a run-capture/evidence SDK into an Agent Action Firewall SDK that controls high-risk agent actions before execution. The sprint starts with contract stabilization, then introduces action-first APIs, action lifecycle evidence, approval runtime, and connector/gateway foundations.

This sprint does not implement a full web dashboard or production SaaS backend. It defines the staged work needed to reach the support-agent MVP.

## Current Architecture Summary

- Python SDK is the primary implementation today.
- `SetorraCollector` owns sessions, evidence events, policy checks, guardrails, redaction, integrity hashes, local artifact generation, optional handshake, and backend ingest.
- `invoke` / `runner` wrap whole agent runs, not individual business actions.
- Policy supports `allow`, `deny`, and `require_approval`.
- Guardrails run before policy and can allow, deny, or modify inputs.
- TypeScript package exists but is thinner than Python and still uses schema `1.2`.
- Local artifacts currently write to `data/<session_id>/...`, while docs/tests still expect `evidence/<session_id>/...` or returned artifact paths.

## Confirmed Findings

- `PYTHONPATH=. SETORRA_DISABLE_VALIDATION=1 pytest -q` fails: `tests/test_auth_detection.py` expects `result["evidence_path"]`, but Python `end_session()` returns only `session_id`.
- `setorra/collector.py` appends tool errors to `_errors`, but output summary reads `_output_errors`, so `errors[]` is not populated.
- Python evidence uses `event_index`; several docs/tools still reference `chain_index`.
- TypeScript SDK builds successfully, but its schema is behind Python and has no action firewall API.
- `packages/setorra-node/` is an empty package shell and should be removed or intentionally populated later.
- LangChain adapter references `redact()` without importing it.
- Local validation can attempt external Schema Registry calls when environment variables are present; tests should disable or mock this by default.
- Documentation still contains old `Tisac` naming in several places.

## Assumptions

- New product direction is Agent Action Firewall, not generic agent observability.
- TypeScript becomes the primary SDK for new action-firewall developer experience.
- Python remains supported and should retain evidence/security parity.
- Support-agent workflows are the MVP wedge.
- Gateway/MCP is required eventually because SDK-only control is bypassable.
- Existing evidence, redaction, policy, and integrity work should be preserved, not rewritten.

## Architectural Decisions

- Introduce action-first control as a new layer instead of forcing it into `runner.invoke`.
- Keep run capture as a compatibility layer.
- Add a canonical action lifecycle schema before building connectors.
- Extend decisions to include `needs_more_context`.
- Use policy/guardrail engines already in the repo where possible.
- Make artifact contracts stable before adding new behavior.
- Prefer local deterministic tests over live backend or Schema Registry tests.
- Treat gateway/connectors as a later layer over the same action execution contract.

## Execution Plans

### Plan 1: Stabilize Existing SDK Contracts

Goal: Make current Python and TypeScript SDK behavior reliable before adding new firewall APIs.

Tasks:
1. Fix Python artifact return contract from `end_session()`.
2. Decide canonical local artifact root: `evidence/` or `data/`; update code, tests, and docs consistently.
3. Fix `_errors` / `_output_errors` mismatch and include `errors_truncated`.
4. Align `event_index` vs `chain_index` across schemas, docs, tools, and tests.
5. Disable/mask external validation in default tests.
6. Fix LangChain adapter missing `redact` import.
7. Remove or document empty `packages/setorra-node/`.
8. Add contract tests for `output.json`, `evidence.jsonl`, and `manifest.json`.

Verification:
- `PYTHONPATH=. SETORRA_DISABLE_VALIDATION=1 pytest -q`
- `npm run build` in `packages/setorra-ai`
- Golden artifact shape assertions for one success run and one tool-error run.

### Plan 2: Add Action Firewall API

Goal: Add first-class action-level SDK control.

Tasks:
1. Define canonical `ActionRequest` fields: `agent_id`, `action`, `system`, `payload`, `context`, `idempotency_key`.
2. Define canonical `ActionDecision`: `allow`, `deny`, `needs_approval`, `needs_more_context`.
3. Add TypeScript `firewall.execute(request)` as the primary API.
4. Add Python equivalent for parity.
5. Map action requests into existing guardrail and policy evaluation.
6. Return a structured result without executing external systems yet.

Verification:
- Unit tests for allow, deny, approval, and needs-more-context decisions.
- TypeScript build and Python tests.
- Evidence includes action request and policy decision events.

### Plan 3: Add Action Lifecycle Evidence

Goal: Make every controlled action auditable from request through result.

Tasks:
1. Add lifecycle events: `action.requested`, `policy.decision`, `approval.requested`, `approval.resolved`, `action.executed`, `action.succeeded`, `action.failed`.
2. Add `action_id`, `idempotency_key`, `system`, `payload_hash`, `context_hash`, and `result_hash`.
3. Add compensation metadata shape: `compensation.type`, `compensation.reference`, `compensation.available`.
4. Update manifest/output summaries to include action counts and final action statuses.
5. Add replay/verification utility for action lifecycle completeness.

Verification:
- Golden event sequence tests.
- Integrity replay passes.
- Missing terminal action status fails validation.

### Plan 4: Add Approval Runtime

Goal: Make approvals product-ready rather than only in-memory SDK waits.

Tasks:
1. Introduce pending approval records keyed by `action_id` and `intent_hash`.
2. Support approve, deny, request-more-context, expired, and cancelled outcomes.
3. Bind approver identity and optional email metadata.
4. Ensure approved execution resumes only for the same action hash.
5. Add local file/in-memory approval store for MVP tests.

Verification:
- Approval happy path.
- Deny path blocks execution.
- Expired approval cannot execute.
- Tampered payload after approval is rejected.

### Plan 5: Add Connector and Gateway Foundations

Goal: Prepare for support-agent MVP with protected action execution.

Tasks:
1. Add generic connector interface.
2. Add generic HTTP connector with allowlisted method/url/action mapping.
3. Add Stripe refund connector stub with idempotency-key support.
4. Add Slack approval connector stub.
5. Add MCP/gateway design document and minimal local gateway entrypoint.
6. Ensure credentials are owned by Setorra/gateway, not the agent.

Verification:
- Fake connector tests for success/failure/retry/idempotency.
- No raw credentials in evidence.
- Denied actions never call connector.
- Approved actions call connector exactly once.

## Risks

- Adding gateway too early could slow SDK iteration.
- Maintaining Python and TypeScript parity can double work unless schema/contracts are centralized.
- Policy language can grow too complex; keep MVP rules simple.
- Approval persistence can become backend-heavy; start with local/store interface.
- Evidence schema drift is already present; fix contract tests before adding events.

## Verification Strategy

- Contract tests first.
- Golden artifacts for key scenarios.
- No live network required for default test suite.
- Python and TypeScript build/test gates for every plan.
- Diff review after every plan against this sprint intent.
- Side-effect review focused on privacy, evidence compatibility, and policy bypass risk.

## Staff-Engineer Review Checklist

- Is this the minimal correct change for the current plan?
- Does the change preserve existing evidence and redaction guarantees?
- Can a denied action still reach an external connector?
- Does every risky action have a stable action ID and terminal status?
- Are policy decisions tied to the exact payload/context hash?
- Are tests deterministic and offline by default?
- Is TypeScript parity explicitly handled?
