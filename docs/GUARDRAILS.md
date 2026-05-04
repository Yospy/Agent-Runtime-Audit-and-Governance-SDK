# Runtime Guardrail Bundles

Setorra guardrails let agent developers define private, per-app controls that run *before* policy evaluation. Guardrails sanitize inputs, clamp parameters, or deny dangerous actions deterministically with millisecond latency. Bundles are never exported to buyers; instead the SDK records `guardrail.*` evidence events summarizing each evaluation.

## Bundle Format

Guardrail bundles are JSON or YAML objects with three top-level keys:

```yaml
version: 2025-03-18-calendar
metadata:
  owner: calendar-team
  description: Runtime guardrails for calendar assistant
actions:
  agent.invoke:
    rules:
      - id: deny_mass_delete
        effect: deny
        message: Calendar deletion requests require manual review
        match:
          parameters.inputs.input:
            contains: delete
      - id: sanitize_share
        effect: modify
        message: Strip email addresses before policy evaluation
        match:
          parameters.inputs.input:
            contains: "@"
        set:
          parameters.inputs.input: "[REDACTED_EMAIL]"
```

- `version` – required string used for evidence linking and replay.
- `metadata` – optional object persisted in the evidence `guardrail` block.
- `actions` – map of action names → rule lists. Action names follow the same taxonomy as policy rules (`agent.invoke`, `payment.transfer`, etc.).

### Rule Fields

| Field      | Type            | Description |
|------------|-----------------|-------------|
| `id`       | string (required) | Stable identifier emitted in evidence and summaries. |
| `effect`   | `allow` (no-op), `modify`, or `deny` (default). |
| `match`    | object (optional) | Dotted-path conditions evaluated against the intent payload. Supports the same operators as policy rules (`eq`, `neq`, `lt`, `lte`, `gt`, `gte`, `in`, `not_in`, `contains`, `exists`). |
| `message`  | string (optional) | Friendly reason recorded in evidence and surfaced when a guardrail blocks execution. |
| `set`      | object (optional, modify-only) | Dot-path assignments applied when the rule fires. Paths can target `actor`, `parameters`, or `context` branches. |

Rules are evaluated in declared order. `deny` short-circuits evaluation; `modify` continues so multiple sanitizers can run; `allow` records intent metadata without mutations.

## Runtime Behavior

1. `invoke` (and direct SDK integrations) call `collector.apply_guardrails(...)` before policies or tool execution.
2. The enforcer loads the latest bundle, evaluates rules matching the action, applies mutations, and records a `guardrail.applied`/`modified`/`blocked` event.
3. If the status is `deny`, a `GuardrailViolationError` is raised and the session finalizes with `guardrail_denied` outcome events.
4. The mutated parameters/context are fed into policy enforcement and the agent call, ensuring downstream decisions see sanitized inputs.

Evidence events now include a `guardrail` block with:

```json
{
  "version": "2025-03-18-calendar",
  "bundle_hash": "…",
  "status": "modified",
  "rule_id": "sanitize_share",
  "applied_rules": ["sanitize_share"],
  "modifications": [
    {"rule_id": "sanitize_share", "path": "parameters.inputs.input", "value": "[REDACTED_EMAIL]"}
  ],
  "message": "Strip email addresses before policy evaluation"
}
```

The session summary (`<session>-output.json`) gains a `guardrail_summary` section containing bundle metadata, applied rule IDs, modification counts, and timestamps. This supports replay validation and auditor review without exposing the original bundle.

## Loader Options

- `GuardrailLoader(path)` loads bundles from JSON (`.json`) or YAML (`.yaml`/`.yml`). YAML handling requires `pyyaml` (installed by default in the SDK).
- `SETORRA_GUARDRAIL_PATH` points the collector factory to a bundle for out-of-the-box integrations.
- Custom services can subclass or wrap `GuardrailEnforcer` to add caching or telemetry while reusing the parsing/validation logic.

## Testing Guidance

- Add unit tests for each guardrail bundle using `collector.apply_guardrails` to ensure intended mutations/denials occur.
- Integration tests should assert evidence contains the expected `guardrail.*` events, session summaries include `guardrail_summary`, and policy decisions see sanitized inputs.
- Property-based testing is recommended for redaction rules (e.g., random email strings) to avoid regressions in sanitizers.
