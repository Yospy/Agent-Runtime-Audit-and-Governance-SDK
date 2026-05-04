Setorra Node SDK — setorra-ai

Overview
- Minimal Node/TypeScript SDK aligned with the Python SDK.
- Offline‑first; privacy‑first; optional control‑plane handshake for organization stamping.
- Additive planning capture and action firewall APIs (schema v1.3); legacy emit remains unchanged.

Install
- npm install setorra-ai

Environment
- SETORRA_API_KEY: Optional; link org via control‑plane handshake.
- SETORRA_BACKEND: Control‑plane base URL (preferred). Example: http://127.0.0.1:5001
- SETORRA_CONTROL_URL: Deprecated alias; accepted with a one‑time warning.
- SETORRA_STORAGE_DIR: Directory for evidence/output artifacts (default: ./data).
- SETORRA_CAPTURE_PLANS: Enable planning (`true/1/yes/on`), see below. Default: off.
- SETORRA_ERRORS_AS_OUTPUT: Optional wrapper UX flag (true/1/yes/on). No effect on evidence.

Usage — legacy emit (unchanged)
```ts
import { createCollector } from "setorra-ai";
const collector = await createCollector();
await collector.emit("agent.started", { run_id: "run_123" });
```

Usage — sessions + planning (opt‑in; schema v1.3)
```ts
import { createCollector } from "setorra-ai";

process.env.SETORRA_CAPTURE_PLANS = '1'; // opt‑in planning
process.env.SETORRA_STORAGE_DIR = './evidence';

const coll = await createCollector();
const sessionId = coll.startSession({
  agentName: 'my-agent', agentVersion: '1.0.0',
  systemPrompt: 'You are …', userPrompt: 'Do X',
});

// Planning lifecycle
const planId = coll.recordPlanCreate({
  summary: 'branch -> write -> test -> commit',
  steps: ['repo.branch_create','fs.write','ci.run_tests','repo.commit'],
  rationale: 'Contact alice@example.com', // will be redacted
});
coll.recordPlanAdopt();

// Downstream actions automatically include context.plan
coll.recordAction('tool.custom_action', { parameters: { name: 'demo' }, status: 'success' });

coll.recordOutcome('success');
const res = coll.endSession({ ok: true });
console.log('evidence:', res.evidence_path, 'output:', res.output_path);
```

Usage — action firewall
```ts
import { createCollector, firewall } from "setorra-ai";

const coll = await createCollector();
coll.startSession({ agentName: "support-agent", agentVersion: "1.0.0" });

const request = {
  agent_id: "support-agent",
  action: "refund.issue",
  system: "stripe",
  payload: { customer_id: "cus_123", amount: 1200 },
  context: { ticket_id: "ticket_456", reason: "duplicate charge" },
  idempotency_key: "ticket_456:refund",
  compensation: { available: true, type: "refund.reverse", reference: "stripe.refunds.cancel" },
};

const result = firewall(coll).execute(request, () => ({ refund_id: "re_123" }));
```

What gets written
- `<session_id>-evidence.jsonl`: tamper‑evident event stream (agent.started → manifest → prompts → plan.* → tool/LLM → outcome)
- `<session_id>-output.json`: redacted run summary (prompts, outcome, environment, first/last hash)

Planning capture (schema v1.3)
- Gate via `SETORRA_CAPTURE_PLANS` (default off).
- Emits `plan.create`, `plan.refine`, `plan.adopt`, `plan.abandon`.
- Deterministic `plan_hash` from canonicalized summary/steps; rationale previews are redacted and clipped.
- After adopt, all actions include `context.plan = { plan_id, version }`.

Privacy & integrity
- Emails/phones/keys are redacted before persistence; `privacy_flags` and `redacted_fields` are recorded.
- Every evidence event includes `schema_version`, `event_index`, `integrity.prev_hash/event_hash` (optional signature in a future release).

Non‑disruption guarantee
- Legacy `emit()` path is unchanged and continues to write `events.ndjson`.
- Sessions + planning are additive; no changes unless you opt in.

Testing locally (single‑folder)
- Build SDK: `(cd packages/setorra-ai && npm run build)`
- Run: `node ts_planning_tests/run_all.mjs`
- Review: `ts_planning_tests/summary.json` and per‑scenario evidence/output files stored in the same folder.

Notes
- Prefer IPv4 loopback in SETORRA_BACKEND (http://127.0.0.1:5001) to avoid IPv6 `localhost` pitfalls.
- Handshake (SETORRA_API_KEY + SETORRA_BACKEND) is optional and stamps `context.organization` when available.
