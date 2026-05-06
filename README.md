# Setorra SDK

Setorra is an SDK that integrates with AI agents to capture runs and produce auditor‑friendly evidence, with privacy‑first redaction and integrity chains. It writes a fixed set of artifacts per run for simple offline validation:

- `evidence/<session_id>/output.json` — redacted run summary with prompts, outcome, environment, tool hashes
- `evidence/<session_id>/evidence.jsonl` — tamper‑evident event stream (agent.started → session.manifest → prompt.capture → tool/LLM/reasoning → outcome → agent.finished)
- `evidence/<session_id>/manifest.json` — sizes, hashes, and chain summary (commit marker)

Artifacts are written to the `evidence/` directory by default for local/dev testing.

## Why Setorra

AI agents are moving from suggestions to production actions. Setorra gives builders a runtime control layer for those actions: policy checks, approvals, connector execution, redaction, and tamper-evident evidence before and after an agent touches real systems.

The OSS SDK focuses on local, developer-owned building blocks:

- action firewall and policy enforcement
- approval binding for risky actions
- evidence logs and replayable integrity chains
- redaction before persistence
- connector and framework integration foundations

Hosted dashboards, SSO, team approvals, managed retention, and enterprise policy distribution can live outside the OSS SDK.

## Quick Start (Runner — single import)

- Install via Poetry (local dev):
- `poetry install`
- Integrate in ≤10 lines with a single import:

```python
from setorra import runner

run = runner(
    agent_name="my-agent",
    agent_version="1.0.0",
    # Optional local bundles (or set via env):
    policy="policy.json",
    guardrails="guardrails.yaml",
    environment="prod",
)

result = run.invoke(my_agent, inputs={"query": "..."})
print("session:", result.session_id)
```

This produces a per-run folder under `evidence/<session_id>/` containing:
- `output.json`
- `evidence.jsonl`
- `manifest.json`

## Action Firewall API

Use `firewall.execute(...)` before any high-risk business action. It records `action.requested`, evaluates guardrails and policy, emits `policy.decision`, and returns a decision without calling external systems.

```python
from setorra import ActionRequest, firewall, setorra_collector

collector = setorra_collector("support-agent", "1.0.0")
collector.start_session()

decision = firewall(collector).execute(ActionRequest(
    agent_id="support-agent",
    action="refund.issue",
    system="stripe",
    payload={"customer_id": "cus_123", "amount": 1200},
    context={"ticket_id": "ticket_456", "reason": "duplicate charge"},
    idempotency_key="ticket_456:refund",
))

if decision.decision == "allow":
    pass  # call Stripe in the next connector phase
elif decision.decision == "needs_approval":
    pass  # surface approval request
```

When you provide an executor callback, the firewall records `action.executed` and then `action.succeeded` or `action.failed` with hashes and compensation metadata. Denied or approval-pending actions never call the executor.

Approval flows can be resolved locally for MVP demos/tests. Approvals are keyed by `action_id + intent_hash`, bind approver metadata, and `execute_approved(...)` rejects payload/context hash mismatches before execution.

Connector foundations are available for the support-agent MVP: `FakeConnector`, an allowlisted `HttpConnector`, `StripeRefundConnector` stub, and `SlackApprovalConnector` stub. Credentials stay inside connector/gateway objects and are not written into evidence.

Notes
- Guardrails are enforced before policy; denials block before execution.
- Evidence, redaction, and integrity chaining are identical to the legacy path.
- Runner defaults to errors‑as‑output on (privacy‑safe return values for blocked actions). You can override per call.

## OpenAI Agents SDK demo

The live demo in `examples/openai_support_refund_agent.py` uses the OpenAI Agents SDK to run a support refund agent, but the refund action is governed by Setorra before any connector execution.

```bash
python3 -m pip install -r examples/requirements-openai-agent.txt
export OPENAI_API_KEY=sk-...
python3 -m examples.openai_support_refund_agent
```

The demo uses `examples/policies/openai_support_refund_policy.json`:
- refunds `<= 500` are allowed
- refunds `> 500` require local manager approval
- refunds `> 5000` are denied

Output prints the `session_id` and artifact paths. Logs/evidence are written to `evidence/<session_id>/`:
- `evidence.jsonl` — event chain with action request, policy decision, approval, execution, and result
- `output.json` — redacted summary and action counts
- `manifest.json` — artifact hashes and chain summary

## Contributing

Contributors can improve Setorra through:

- connectors for real systems
- policy and guardrail examples
- retry, idempotency, fallback, and outcome verification
- sandbox/dry-run executors
- MCP gateway and framework integrations
- evidence export, replay, and validation tooling
- docs and examples for agent builders

Start with `CONTRIBUTING.md` and `ROADMAP.md`. This project uses Apache-2.0 with DCO sign-off for contributions.

Do not commit `.env`, generated `evidence/`, generated `logs/`, internal `sprints/`, dependency folders, or secrets. Use `.env.example` for safe local configuration.

### Legacy integration (supported)

```python
from setorra import setorra_collector, invoke

collector = setorra_collector(agent_name="my-agent", agent_version="1.0.0")
result = invoke(
    collector,
    agent_or_executor=my_agent,
    inputs={"query": "..."},
    system_prompt="You are ...",
    user_prompt="...",
)
print("session:", result.session_id)
```

### Optional: Link SDK to your backend (handshake)

Provide just an API key and a backend URL; the SDK will authenticate once and stamp your tenant identity (`org_id`) onto all evidence. This does not change enforcement, redaction, or integrity — it only adds tenant context.

- `SETORRA_API_KEY` – your developer API key (required to enable handshake)
- `SETORRA_BACKEND` – backend base URL for the control plane (e.g., `http://127.0.0.1:8081` for local)

On success, events will include `context.organization.org_id` (and `org_name` when provided) and the run summary will include `organization.org_id` (and `org_name`). No raw API keys or session tokens are persisted.

### Optional configuration

- `SETORRA_ENVIRONMENT` – label for the current deployment environment (default `local`).
- `SETORRA_HOSTNAME` – override hostname if you do not want to expose the actual machine name.
- `SETORRA_SIGNING_KEY` / `SETORRA_SIGNING_KEY_ID` – enable HMAC signatures on every evidence event.
- `SETORRA_POLICY_PATH` – absolute or relative path to a JSON policy bundle loaded by the SDK at runtime.
- `SETORRA_POLICY_AUTO_WAIT` – when set to `false`, the SDK surfaces approval requirements without blocking; defaults to waiting for approval tokens.
- `SETORRA_GUARDRAIL_PATH` – optional JSON or YAML guardrail bundle that applies developer-defined runtime guardrails before policy evaluation.
- `SETORRA_ERRORS_AS_OUTPUT` – when `true/1/yes/on`, `invoke(...)` returns a standardized, privacy‑safe blocked message (instead of raising) for guardrail/policy denials and approval timeouts/cancellations.
  - Legacy wrapper default: disabled unless set here or passed as an argument.
  - Runner default: enabled (can be overridden per call).

### Optional: Schema Registry validation

SDK artifact validation against Confluent Schema Registry is disabled by default so local runs and tests never make outbound validation calls implicitly. Enable it explicitly with:

- `SETORRA_VALIDATE_PAYLOADS` – `true/1/yes/on` to validate artifacts before persistence/delivery
- `SETORRA_DISABLE_VALIDATION` – `true/1/yes` to force-disable validation even when enabled

Notes
- Validation is opt-in and should stay disabled for default offline test runs.

### Planning capture (optional)

Capture planning steps as additive events that do not change enforcement or evidence semantics for actions. Enable via:

- `SETORRA_CAPTURE_PLANS` – when `true/1/yes/on`, the SDK emits `plan.create`, `plan.refine`, `plan.adopt`, and `plan.abandon` events and threads the current `plan_id` into subsequent action events under `context.plan`.

In your integration code (after `start_session`), call:

```python
plan_id = collector.record_plan_create(
    summary="branch -> edit -> test -> commit",
    steps=["repo.branch_create", "fs.write", "ci.run_tests", "repo.commit"],
    rationale="short rationale here",
)
collector.record_plan_adopt()
# subsequent collector.record_action(...) will include context.plan
```

Previews are redacted and clipped; only hashed/canonicalized structures are used for identity.

### TypeScript/Node SDK (setorra-ai)

- Install: `npm install setorra-ai`
- Legacy emit (unchanged):
  ```ts
  import { createCollector } from 'setorra-ai';
  const collector = await createCollector();
  await collector.emit('agent.started', { run_id: 'run_123' });
  ```
  - Sessions + planning (opt-in): enable `SETORRA_CAPTURE_PLANS=1`, then:
  ```ts
  const coll = await createCollector();
  const sid = coll.startSession({ agentName: 'my-agent', agentVersion: '1.0.0', userPrompt: 'Do X' });
  coll.recordPlanCreate({ summary: 'branch -> write -> test', steps: ['repo.branch_create','fs.write','ci.run_tests'] });
  coll.recordPlanAdopt();
  coll.recordAction('tool.custom_action', { parameters: { name: 'demo' }, status: 'success' });
  coll.endSession({ ok: true });
  ```
  - Artifacts per session: `evidence/<session_id>/{evidence.jsonl,output.json,manifest.json}` (written to `SETORRA_STORAGE_DIR`).

## Policy enforcement

Policies are versioned JSON bundles that describe action rules, approval requirements, and fail-open/closed behavior. Place a bundle on disk, point the SDK to it via `SETORRA_POLICY_PATH`, or instantiate `PolicyLoader` programmatically:

```python
from setorra import PolicyLoader, SetorraCollector

loader = PolicyLoader("policies/finance.json")
collector = SetorraCollector(
    agent_name="finance-agent",
    agent_version="1.1.0",
    policy_loader=loader,
)

decision = collector.enforce_policy(
    "payment.transfer",
    actor={"roles": ["finance"]},
    parameters={"amount": 2500, "currency": "USD"},
)

if decision.decision == "require_approval":
    requirement = collector.pending_approvals()[0]
    # trigger Slack/email workflow, then resume once token arrives
    collector.resume_with_approval(
        intent_hash=requirement.intent_hash,
        token="token-from-approver",
        approver_id="manager-123",
        method="slack",
        metadata={"approver_email": "manager@company.com"},
    )
```

See `docs/POLICY.md` for policy schema details, operators, fallback precedence (action → bundle → SDK), and the runtime approval handshake.

 

## Errors as Output (optional)

By default, guardrail/policy denials are surfaced as exceptions and your code decides how to display messages. If you prefer the SDK to return a user‑displayable, privacy‑safe message, enable the opt‑in mode:

```python
from setorra import setorra_collector, invoke

collector = setorra_collector(agent_name="my-agent", agent_version="1.0.0")
result = invoke(
    collector,
    agent_or_executor=my_agent,
    inputs={"query": "delete all"},
    errors_as_output=True,  # or set env SETORRA_ERRORS_AS_OUTPUT=true
)

print(result.output)  # e.g., "Blocked by policy." or "Blocked by guardrails."
```

Notes
- Enforcement and evidence are unchanged; this only alters how the wrapper returns errors to your app.
- Messages are minimal and privacy‑safe. Detailed reasons remain in evidence artifacts and summaries with redaction applied.

## Runtime guardrails

Guardrails are developer-owned bundles that live with the agent codebase and sanitize or block risky inputs before policy evaluation or tool execution. They are resolved locally (never shared with downstream buyers) and emit `guardrail.*` evidence events for audit trails.

Load a bundle from JSON or YAML via `GuardrailLoader` or the `SETORRA_GUARDRAIL_PATH` environment variable:

```python
from setorra import GuardrailLoader, SetorraCollector, invoke

guardrails = GuardrailLoader("guardrails/calendar.yaml")
collector = SetorraCollector(
    agent_name="calendar-agent",
    agent_version="1.3.0",
    guardrail_loader=guardrails,
)

result = invoke(
    collector,
    agent_or_executor=my_agent,
    inputs={"input": "delete all events"},
    user_prompt="delete all events",
)
```

During a session you can call `collector.apply_guardrails(...)` manually (after `start_session`) if you need custom integration points. Each rule can `allow`, `modify`, or `deny` matching intents using the same condition operators as policies. Modifications support dot-path assignments (e.g., `parameters.inputs.input`) to clamp values, redact strings, or coerce payloads before the agent or policy sees them. Guardrail evaluations surface in evidence (`guardrail.applied`, `guardrail.modified`, `guardrail.blocked`) with bundle metadata, applied rule IDs, and modification summaries. See `docs/GUARDRAILS.md` for schema details and testing guidance.

## Design Guarantees
- Privacy-first: emails/phones/secrets masked before persistence; redacted fields tracked explicitly
- Immutable evidence: every event carries `prev_hash`/`event_hash` (+ optional HMAC signature)
- Deterministic artifacts: three files per run (`evidence.jsonl`, `output.json`, `manifest.json`); prompt capture always present
- Full causality: system/user prompts, tool input/output hashes, reasoning snapshots, outcome (success/failure) captured
- Explicit user consent: every approval/denial is logged as `user.consent` with approver, timestamp, and scope
- Framework-agnostic wrapper API with LangChain callback adapter (more adapters coming)
- Policy enforcement before tool execution with evidence for decisions, approvals, and fallbacks

See `docs/SCHEMA.md` for record formats and `AGENTS.md` for product and engineering conventions.

### Errors (Auth/Tool failures) — Schema 1.3

Starting with schema 1.3, the SDK classifies tool errors that indicate authentication/permission failures and surfaces them in a dedicated Errors section in the run summary:

- Evidence (`tool.error`): execution may include `auth_failed: true`, `error_code: 401|403`, and a stable `reason_code`.
- Output summary (`output.json`): `errors[]` lists each tool error with a redacted preview and trace pointers (`event_hash`, `event_index`). A small cap is enforced; excess errors are counted in `errors_truncated`.

Classification is privacy‑safe and deterministic, operating only on redacted previews and safe exception names.

## Redaction (PII & Secrets)

Setorra masks PII and secrets before any persistence. Detectors cover emails,
phones (US/E.164), OpenAI keys and generic API keys, JSON‑quoted secrets,
Authorization Bearer tokens, JWTs, PEM private keys, vendor/cloud tokens,
AWS Access Key IDs, PAN/IBAN/ABA/account/SSN, IPv4 (standard), and IPv6/DOB/
Address (strict+context). Placeholders appear in previews (e.g., `[email]`,
`[phone]`, `[secret]`, `[card]`, …) and `privacy_flags` carry detector names.

Environment (optional):
- `SETORRA_REDACTION_LEVEL` = `standard` | `strict` (default `standard`)
- `SETORRA_REDACTION_DISABLE` = `flag1,flag2`
- `SETORRA_REDACTION_ENABLE` = `flag3,flag4`

See `docs/REDACTION.md` for the complete detector list, placeholders, and
evidence fields.

### PII Testing (targeted)

A focused recheck harness lives under `PII testing/` with a small set of cases
for routing/phone/CVV, PAN/IBAN/ABA, JSON‑quoted secrets/Bearer/JWT, email/
phone/IP, and stress lines. Run:

- `python3 "PII testing/run_pii_tests.py" --level standard`
- `python3 "PII testing/run_pii_tests.py" --level strict`

Summaries: `PII testing/summary-standard.json`, `PII testing/summary-strict.json`.

## Status
- Schema v1.3 adds an Errors section in the output summary and optional classification fields on `tool.error` to flag authentication/permission failures (privacy‑safe, additive-only).
- Schema v1.2 adds planning capture events and optional `context.plan`; additive-only and backward-compatible
- Signing/HMAC optional via `SETORRA_SIGNING_KEY`; Require Approval flows pause execution until `resume_with_approval` succeeds
- Optional errors‑as‑output mode available via `errors_as_output=True` or `SETORRA_ERRORS_AS_OUTPUT`.
