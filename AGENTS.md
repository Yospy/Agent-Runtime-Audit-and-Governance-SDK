# AGENTS.md — Setorra SDK Agent Guide

Scope: This file governs the entire repository and all subdirectories. It specifies how the agent should work within this project, what the product is, the constraints we must uphold, and the order of execution for building features. Follow these instructions for all changes in this repo.

## Mission & Product Summary

Setorra (formerly Tisac) is an SDK that integrates into AI agents to capture their runs and enforce policies and guardrails for security and governance. It produces compliance‑grade, tamper‑evident audit evidence suitable for CISO/CTO review, vendor assessments, and SOC2/ISO workflows. Setorra is not just observability; it is a security and governance layer that proves what happened, why, and under which policy.

Key outcomes for CISOs/CTOs:
- Internal greenlighting: Show that risky actions are governed, privacy is protected, and evidence is complete.
- External assurance: Provide exportable evidence packs to accelerate vendor due diligence and trust.

Core guarantees:
- Policy enforcement on sensitive actions (allow, deny, require approval, request more context, or sandbox later).
- Privacy‑first logging with PII detection and redaction before persistence/export.
- Tamper‑evident, hash‑linked, signed audit chains with replay support.
- Traceable configuration (agent version, policy version, model/prompt context) bound into each decision.
- Evidence packs that map to SOC2/ISO controls for audits.

Non-goals for MVP:
- Building a complex UI; focus on SDK + evidence exports.
- Replacing developer tracing tools; Tisac complements, not duplicates, them.

## Core Concepts (Authoritative Definitions)

- Run: A single agent execution session with a stable session ID and optional parent linkage.
- Step: A discrete unit of agent reasoning or action decision within a run.
- Action: A concrete operation an agent attempts (examples: payment.transfer, refund.issue, email.send, ticket.update).
- Context: Model identity/version, prompt hash, environment and framework metadata associated with the action.
- Policy: A versioned set of rules determining action outcomes and required controls.
- Decision: The policy outcome for an action: ALLOW, DENY, REQUIRE_APPROVAL, NEEDS_MORE_CONTEXT, or SANDBOX.
- Approval: Human attestation bound to a specific decision, identity, and timestamp.
- Evidence Event: A structured, redacted, signed record capturing who, what, when, under which policy, and with what result.
- Integrity Chain: Hash‑linked sequence of signed events, enabling tamper evidence and replay.

## What We Capture (Event Schema — conceptual)

Every event includes, at minimum:
- Who acted: Agent name, version, session ID, framework/runtime.
- What was attempted: Action type and parameters; sensitive fields redacted or hashed.
- Context: Model, prompt hash, environment, tool metadata.
- Policy linkage: Policy ID/version applied and the decision outcome.
- Approval evidence: Approver identity, time, and cryptographic binding (when required).
- Execution result: Status, latency, cost, output preview with redaction.
- Integrity: Event hash, prior hash, signature, event index.

Design notes:
- Redaction happens before any persistence or export; raw PII never leaves the process when feasible.
- Parameters and outputs must support field‑level redaction and hashing with reversible scope limited to authorized approvers only (for non‑MVP, reversible escrow may be deferred).
- Events are idempotent: repeated submissions do not fork integrity chains.

## Policy & Enforcement Model

- Decision space: ALLOW, DENY, REQUIRE_APPROVAL, NEEDS_MORE_CONTEXT. SANDBOX is deferred; MONITOR remains a future consideration.
- Inputs to policy evaluation include risk signals (PII present, target domain, amount thresholds), actor attributes, and environment constraints.
- Approvals must bind the approver identity, policy version, action parameters hash, and time.
- Enforcement must occur before the action executes. For REQUIRE_APPROVAL, execution is deferred until approval evidence is attached.
- Failover precedence: action `fail_mode` → bundle defaults → SDK defaults. Map `fail-open` → allow, `fail-closed` → deny, `require-approval` → require approval.
- Policy bundles are JSON artifacts (see `docs/POLICY.md`) loaded via `PolicyLoader` and evaluated through `PolicyDecisionPoint`; `SetorraCollector.enforce_policy` must guard tool calls and emit `policy.*` evidence.

### Errors-as-Output Mode (Optional Wrapper Behavior)
- Purpose: Provide a simple, privacy‑safe message to show end‑users when an action is blocked, without changing enforcement or evidence.
- Behavior: When enabled, the integration wrapper (`invoke`) catches guardrail/policy exceptions and returns a standardized payload containing a `public_message` and `status` (e.g., `guardrail_denied`, `policy_denied`, `approval_timeout`, `approval_cancelled`).
- Defaults: Disabled. Canonical behavior remains exception‑based control signals.
- Enablement: per‑call `errors_as_output=True` or environment `SETORRA_ERRORS_AS_OUTPUT` ∈ {true, 1, yes, on}.
- Privacy: Messages are minimal and redacted by design; detailed rationale remains in evidence artifacts and run summaries.
- Evidence: Unchanged. All `policy.*`, `guardrail.*`, `agent.policy_*`, and integrity events are emitted exactly as before.

### Runtime Guardrails (Developer-Owned)

- Guardrails are defined by agent builders in JSON or YAML bundles; they remain private to the application and are not surfaced to downstream buyers.
- Loaded via `GuardrailLoader` (or `SETORRA_GUARDRAIL_PATH`) and enforced before policy evaluation, allowing developers to sanitize parameters, clamp inputs, or fail closed instantly.
- Rules can `allow`, `modify`, or `deny` using the same condition operators as policies; modifications rely on dot-path assignments into `actor`, `parameters`, or `context` payloads.
- Enforcement emits `guardrail.applied` / `guardrail.modified` / `guardrail.blocked` events; the evidence schema now includes a `guardrail` block carrying bundle metadata, applied rule IDs, and status.
- Guardrail denials raise `GuardrailViolationError`, finalizing the session with a `guardrail_denied` outcome and preserving integrity links.
- Output summaries include a `guardrail_summary` with bundle hash, applied rule log, and modification counters for audit reconstruction.

Operational note: Sample agent uses errors‑as‑output to print SDK‑provided blocked messages and removes app‑owned exception text; this is an integration choice and not required for enforcement.

Governance defaults (MVP examples):
- Finance: Transfers above threshold require approval; unauthorized recipients are denied.
- Support: Refunds above threshold require approval; outbound PII redaction enforced on logs.

## Evidence Packs (Compliance‑Ready Exports)

- Contents: Events, policy definitions and version map, signing metadata, environment manifest, approver roster and attestations, and a controls mapping index.
- Properties: Reproducible, tamper‑evident, minimally sufficient for SOC2/ISO control evidence.
- Privacy posture: Contains redacted data only; hashed fields documented; no raw PII.
- Delivery: Single artifact bundle suitable for vendor DD and internal audits.

## Architectural Principles

- Minimal friction: Drop‑in wrappers or middleware around common agent frameworks (e.g., tool/skill calls, function calls, actions).
- Deterministic by default: Stable IDs, consistent redaction, and canonical hashing.
- Asynchronous, backpressure‑aware pipeline: Never block agent critical path beyond configured budgets.
- Fail‑safe: If policy evaluation is unavailable, default to safest configured mode (deny or require approval) rather than silent allow.
- Portable artifacts: Evidence and policy references must be readable without the original runtime.
- Extensibility: Pluggable PII detectors, event sinks (file, local DB, remote service), and policy engines.

## MVP Scope (Phase 1)

- Language/runtime: Python first; TypeScript/Node to follow.
- Instrumentation: Interception of tool calls and high‑risk actions with minimal code changes to host agents.
- Policy evaluation: Threshold‑based and attribute‑based decisions producing ALLOW/DENY/REQUIRE_APPROVAL/SANDBOX.
- Redaction: Built‑in detectors for common PII (email, phone) and configurable field masking.
- Signing & chaining: Local key management with hash‑linked event chains.
- Storage: Local file/SQLite sink and an export format for evidence packs.
- Samples: Finance and Support agent scenarios with realistic audit trails.

Acceptance for Phase 1:
- Demonstrate both scenarios produce complete evidence (start/end, decision, integrity, approvals).
- Redaction verified: No unredacted emails/phones appear in stored/exported artifacts.
- Replay tool can reconstruct sequence and verify chain integrity without contacting external services.

## Phase 2+ (Guided by Customers)

- Remote attestation: Server‑verified time, WORM storage, and HSM/KMS‑backed signing.
- Advanced policy: Pluggable policy engines and external policy stores; risk scoring.
- Sandbox modes: Emulation for filesystem/network and shadow execution.
- Approvals: SSO‑bound just‑in‑time approval flows and audit‑ready attestations.
- Compliance integrations: Mappings export for SOC2/ISO and connectors to common platforms.

## Security & Privacy Requirements

- Data minimization: Log only what is necessary to establish evidence.
- Redaction precedence: Redact before persistence; only store redacted previews, never raw secrets or tokens.
- Key management: Clearly separate signing keys from encryption keys; document rotation and key provenance.
- Time source: Prefer monotonic clocks with periodic external anchoring in later phases.
- Multi‑tenant isolation: Ensure identifiers, keys, and storage are tenant‑scoped and segregated.
- Retention & deletion: Configurable retention; cryptographically prove deletion (non‑MVP may mark tombstones only).

## Performance & Reliability Targets (MVP)

- Overhead budget: Median per action decision under a small fixed budget suitable for interactive agents.
- Backpressure: Queue and batch events; drop only non‑critical telemetry, never policy or integrity events.
- Resilience: If sinks are unavailable, buffer locally within limits and fail closed per policy.

## Testing & Validation Strategy

- Unit: Deterministic redaction, hashing, decision outputs for known inputs.
- Property‑based: Chain integrity holds across arbitrary sequences and retries.
- Integration: Finance and Support end‑to‑end runs validate policy, approval, and evidence generation.
- Negative tests: Denials and blocked outbound operations are recorded correctly and non‑bypassable.
- Replay: Black‑box reconstruction matches original sequences and signatures.

## Open Questions (Track and resolve)

- Approval identity binding: Minimum identity proof acceptable for MVP without SSO?
- Evidence pack format: Single archive vs directory with manifest—choose for portability and size.
- Reversible hashing escrow: Is there a near‑term need, or are one‑way hashes sufficient?
- Policy language: Internal simple rules vs adopting an existing engine in Phase 2.

## Non‑Goals (to avoid scope creep)

- Full‑fledged PII detection library beyond common patterns; rely on pluggable detectors for advanced needs.
- Competing with developer tracing UIs; integrate or export to those tools if needed.
- Building a centralized SaaS before SDK proves value; start local/offline‑first.

## How to Work in This Repo (Agent Instructions)

- Align to this document first: All architectural and product choices must be consistent with the concepts and constraints above. If a change conflicts, update this file with rationale before implementing.
- Keep changes minimal and focused: Do not refactor unrelated code when solving a task.
- Respect privacy and security defaults: Never introduce logs containing raw secrets or PII.
- Prefer explicit over implicit: Version policy artifacts and schemas; ensure traceability in changes.
- Configure local policy enforcement via `policy_loader` or `SETORRA_POLICY_PATH`; document bundle versions alongside releases.
- Testing is mandatory: Add or update tests matching the Testing & Validation Strategy when changing logic that affects evidence or policy.
- Compatibility first: Design public SDK surface to be stable and easy to integrate with common agent frameworks.
- Dependency hygiene: Minimize new dependencies; prefer mature, audited libraries for cryptography and parsing.
- Documentation: Update README and docs when adding features that affect users, policies, or evidence exports.

### Repository Organization (expected)

- sdk core: Policy evaluation, event model, redaction, signing, and sinks.
- integrations: Framework adapters and middleware for popular agent stacks.
- samples: Finance and Support agent examples with scripted scenarios for evidence generation.
- tools: Evidence pack bundler and replay verifier.
- docs: Product docs, policies, and compliance mappings.

### Coding & Review Conventions

- Clarity over cleverness; no one‑letter variable names.
- Strong typing and docstrings where supported by language.
- Avoid global state; make components testable and injectable.
- No unrelated fixes; mention discovered issues to maintainers instead.
- Do not add license headers unless explicitly requested.
 - When introducing wrapper‑level UX changes (e.g., errors‑as‑output), ensure enforcement semantics, evidence structure, and privacy guarantees remain unchanged. Document the env/flag.

### Decision Making & Changes

- Record rationale for changes to policy semantics, redaction rules, or evidence structure in this file.
- When adding or changing event fields, include notes on privacy impact and compliance mapping.
 - 2025-10-28: Additive evidence and output fields for tool authentication failures.
   - Evidence: `tool.error.execution` may include `auth_failed` (boolean), `error_code` (401/403), and `reason_code` (stable string) when the SDK deterministically classifies an error as an authentication/permission failure.
   - Output: `OutputSummary.errors[]` lists tool errors with trace pointers (event_hash, event_index) and the same classification fields for UI consumption; capped by `errors_truncated`.
   - Privacy: Classification operates on redacted previews only. No raw secrets/PII are added. This is additive and backward‑compatible.
 - 2025-10-13: Approver email capture guidance added. Use `approval.metadata.approver_email` to store clear work emails for internal audits. Privacy: treat as PII; exclude or sanitize in external/vendor exports.
 - 2025-10-14: Integration verification (harness-based). LangChain and LangGraph integrations passed end-to-end with wrapper-only setup; events captured for baseline, guardrails (modified/blocked), policies (allow/deny/require_approval with approved/expired/cancelled), exceptions, and buffer compaction. PII masking verified across prompts/inputs/outputs (no raw PII). OpenAI and Anthropic tool/function calling simulated via harness stubs; tool lifecycle and enforcement events captured. CrewAI/AutoGen harnesses exist but crashed on import due to environment-native wheel issues (numpy/torch); not an SDK problem. Artifacts under `harness_results/` (see `tests.md:1`).
 - 2025-10-19: Backend handshake + organization identity stamping added. The SDK optionally performs a control‑plane handshake using only `SETORRA_API_KEY` and `SETORRA_BACKEND` to retrieve `{org_id, key_id, session_token, org_name?}`.
   - Evidence: every event includes `context.organization = { org_id, org_name?, key_id_prefix? }` when linked; output summaries include `organization = { org_id, org_name?, key_id_prefix? }`.
   - Privacy: No raw API keys or session tokens are persisted. `org_name` is a non‑secret label; still treat consistently with data minimization (do not over‑log).
   - Compatibility: Handshake is optional and non‑blocking. If unreachable or unset, SDK remains offline‑first and produces artifacts exactly as before (no organization block).
  - Local dev: Prefer `SETORRA_BACKEND=http://127.0.0.1:5001` to avoid IPv6 localhost issues. Factory `setorra_collector(...)` performs the handshake; direct constructors bypass it by design.
  - Harness: Added `harness_tests/handshake_langchain_harness.py` for handshake validation. Other harness modules remain unchanged and continue to use direct constructors.

 - 2025-10-29: Runner integration facade (single import).
   - Purpose: standardize developer integration to three steps with one import: `from setorra import runner` → `run = runner(agent_name, agent_version, policy=..., guardrails=..., environment=...)` → `run.invoke(...)`.
   - Behavior: Delegates to the existing wrapper and collector factory; enforcement (guardrails → policy), redaction, evidence, integrity chaining, and handshake remain unchanged.
   - UX: For Runner only, the default is `errors_as_output=True` to surface privacy-safe blocked messages in user-facing apps. Legacy wrapper default remains exception-based unless enabled via arg or `SETORRA_ERRORS_AS_OUTPUT`.
   - Compatibility: Legacy `setorra_collector` + `invoke` remain supported and unchanged; Runner is the preferred default going forward.
 - 2026-05-04: Action Firewall API introduced.
   - Purpose: first-class `firewall.execute(ActionRequest)` runtime control before high-risk business actions.
   - Policy: `needs_more_context` is now a supported policy decision for action-firewall flows; it pauses execution without treating the request as denied.
   - Evidence: `action.requested` and terminal `action.*` events are additive and reuse existing redaction, policy, approval, and integrity guarantees.

## Product Refinements Derived from PRD

- Emphasize policy-bound evidence: Every decision ties back to a specific policy version and environment manifest.
- Guarantee baseline events for every run: `agent.started` → `session.manifest` → `prompt.capture` → actions/LLM → `agent.outcome` → `agent.finished`.
- Approval binding: Captured approvals must include who, when, and exactly what was approved (parameter hash), with cryptographic binding.
- Action taxonomy: Normalize action names into a small, well‑documented set to simplify policies (payment.transfer, refund.issue, email.send, ticket.update, etc.).
- Outcome clarity: Always capture both attempted and final outcome (including sandboxed or denied states) with integrity chain continuity.
- Audit completeness rule: Every attempted action must have start and end events, with decisions and signatures present.
- Evidence export ergonomics: One command to produce an audit‑ready pack that a vendor can review without specialized tools.
 - Optional UX path for blocked actions: Provide standardized, privacy‑safe messages via wrapper mode without weakening control flows or evidence guarantees.

## Initial Milestones (Execution Order)

1) Event model and redaction rules finalized with examples for Finance/Support.
2) Signing and hash‑linking with local key management.
3) Policy evaluation for thresholds and attribute‑based decisions.
4) Interceptors for common agent tool/action call sites.
5) Local sinks and evidence pack exporter with controls mapping.
6) Replay verifier for integrity and completeness.

This AGENTS.md is authoritative for this repo. If requirements change, update this document first to keep the team and artifacts aligned.
