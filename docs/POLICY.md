# Setorra Policy Bundles

Policies describe which agent actions are allowed, denied, require human approval, or need more context. Bundles are versioned JSON documents that the SDK loads locally (cloud distribution can be layered on later). This document explains the JSON structure, evaluation semantics, approval workflow, and available SDK hooks.

## Bundle Structure

```jsonc
{
  "version": "2024-06-15",
  "metadata": { "owner": "fraud" },
  "sdk": {
    "decision": "allow",          // SDK-wide default decision when no rule matches
    "fail_mode": "fail-open"       // SDK-wide fallback when evaluation fails
  },
  "defaults": {
    "decision": "deny",            // Bundle-level default (overrides sdk.decision)
    "fail_mode": "fail-closed"     // Bundle-level fallback (overrides sdk.fail_mode)
  },
  "actions": {
    "payment.transfer": {
      "description": "Outbound fund transfers",
      "fail_mode": "require-approval",    // Action fallback (overrides defaults.fail_mode)
      "default_decision": "deny",          // Action default decision when no rule matches
      "approval": {                         // Optional action-level approval spec
        "expires_in": 900,
        "approver_roles": ["finance.manager"]
      },
      "rules": [
        {
          "id": "low-value-autopass",
          "decision": "allow",
          "match": {"parameters.amount": {"lte": 1000}},
          "rationale": "Transfers ≤ $1000 auto-allow"
        },
        {
          "id": "mid-tier-approval",
          "decision": "require_approval",
          "match": {"parameters.amount": {"lte": 5000}},
          "approval": {"expires_in": 600, "approver_roles": ["finance.senior"]}
        }
      ]
    }
  }
}
```

### Field Reference

- `version` (string, required): immutable identifier recorded in evidence.
- `metadata` (object, optional): free-form bundle annotations (owner, release notes, etc.).
- `sdk` (object, required): SDK-wide defaults when no bundle/action overrides apply.
  - `decision`: one of `allow`, `deny`, `require_approval`, `needs_more_context`.
  - `fail_mode`: one of `fail-open`, `fail-closed`, `require-approval`.
- `defaults` (object, optional): bundle-level overrides for `sdk` defaults.
- `actions` (object, optional): map of action names to detailed rules.
  - `default_decision`: decision used when no rule matches (must be `allow`, `deny`, `require_approval`, or `needs_more_context`).
  - `fail_mode`: fallback strategy for the action (overrides bundle/sdk fail modes).
  - `rules`: ordered list of rule objects. First match wins. Each rule supports:
    - `id` (string, optional): stable identifier recorded in evidence. Auto-generated if omitted.
    - `decision`: `allow`, `deny`, `require_approval`, or `needs_more_context`.
    - `match`: object describing conditions over the normalized intent.
    - `rationale` (string, optional): human-readable reason recorded in evidence.
    - `approval` (object, optional): overrides action-level approval metadata when `decision` is `require_approval`.
  - `approval` (object, optional): default approval metadata used when `default_decision` is `require_approval` or fallbacks demand it.

### Match Operators

Rule `match` objects address intent fields using dotted paths (e.g., `parameters.amount`, `actor.roles`). Operators are evaluated conjunctively (all conditions must pass).

Supported operators:

- `eq`, `neq`: equality/inequality.
- `lt`, `lte`, `gt`, `gte`: numeric comparisons (values coerced with `float`).
- `in`, `not_in`: membership against a list/tuple.
- `contains`: substring or set/list membership.
- `exists`: boolean flag asserting presence or absence.
- Raw literals or arrays imply strict equality.

### Failover Precedence

If evaluation cannot complete (e.g., malformed intent, missing data), the SDK derives a fallback decision using the highest-priority fail mode available:

1. Action-level `fail_mode`
2. Bundle `defaults.fail_mode`
3. `sdk.fail_mode`

Fail modes map to decisions:

- `fail-open` → `allow`
- `fail-closed` → `deny`
- `require-approval` → `require_approval`

Fallback usage is recorded in the evidence stream with `policy.decision` events that include `fallback_mode` and `fallback_trigger` fields.

## Runtime Enforcement

The SDK loads policies locally; remote distribution/versioning can wrap `PolicyLoader` later. There is no network dependency in the MVP.

```python
from setorra import PolicyLoader, SetorraCollector

loader = PolicyLoader("policies/finance.json")
collector = SetorraCollector(
    agent_name="finance-agent",
    agent_version="1.1.0",
    policy_loader=loader,
)

collector.start_session(system_prompt="...", user_prompt="...")

# Normalize tool intent before execution
decision = collector.enforce_policy(
    "payment.transfer",
    actor={"roles": ["finance"]},
    parameters={"amount": 2500, "currency": "USD"},
    context={"tool": "bank.core"},
)

if decision.decision == "allow":
    result = initiate_transfer()
elif decision.decision == "require_approval":
    requirement = collector.pending_approvals()[0]
    dispatch_slack_card(requirement.intent_hash)
    # Later, when the approver responds:
    collector.resume_with_approval(
        intent_hash=requirement.intent_hash,
        token="approval-token",
        approver_id="finance-manager",
        method="slack",
        metadata={"approver_email": "jane.doe@company.com"},
    )
elif decision.decision == "needs_more_context":
    request_more_context(decision.intent_hash)
    result = {"status": "needs_more_context"}
else:
    raise RuntimeError("Transfer denied by policy")

collector.record_outcome(status="success")
collector.end_session(final_output=result, model_id="gpt-4o-mini")
```

### Approval Lifecycle

- `require_approval` decisions include an `intent_hash` (SHA-256 over policy version + normalized intent) and expiry timestamp.
- The SDK blocks by default (`SETORRA_POLICY_AUTO_WAIT` toggles this) until `resume_with_approval` provides a token that references the same `intent_hash`.
- Action-firewall approvals are additionally keyed by `action_id + intent_hash`; approved execution is rejected if the current payload/context hashes differ from the approved record.
- Evidence events emitted automatically:
  - `policy.decision` — evaluation result, including intent hash, rule ID, rationale, and fallback metadata.
  - `policy.approval` — approval tokens (approved, denied, expired, cancelled) with approver identity and method when supplied.

### ASCII Workflow

```
Agent Tool Call
      |
      v
+-----------------+
|  Build Intent   |  (action + actor + parameters + context)
+-----------------+
      |
      v
+-----------------+
| Policy Loader   |  (version, defaults, action rules)
+-----------------+
      |
      v
+-------------------------+
| PolicyDecisionPoint     |
| - Evaluate rules        |
| - Compute intent hash   |
| - Apply failover order  |
+-------------------------+
      |
      +---------------------------+
      |                           |
      v                           v
Decision = ALLOW           Fallback triggered?
      |                           |
      |                           v
      |                   Fail mode precedence
      |                 (action → bundle → sdk)
      |                           |
      |                           v
      |                   ALLOW / DENY / REQUIRE_APPROVAL
      |                           |
      v                           v
Proceed to tool call       +--------------------------+
                            | REQUIRE_APPROVAL issued |
                            | - return intent hash    |
                            | - block or surface wait |
                            +-----------+-------------+
                                        |
                           Approval token (same intent hash)
                                        |
                                        v
                               Decision promoted to ALLOW
                                        |
                                        v
                               Proceed to tool call

All stages emit evidence: `policy.decision` (evaluation, fallback) and `policy.approval` (approval outcomes) with policy version, rule identifiers, and hashes.
```

## Environment Variables

- `SETORRA_POLICY_PATH`: path to a JSON bundle. When set, `setorra.setorra_collector` automatically loads the policy.
- `SETORRA_POLICY_AUTO_WAIT`: defaults to `true`. Set to `false` to surface approval requirements without blocking.

### Sample Bundle

Use the support-agent firewall tests as the current policy examples. They cover allow, deny, approval, and needs-more-context decisions without external services.

## Notes

- Supported decisions are `allow`, `deny`, `require_approval`, and `needs_more_context`. `sandbox` is reserved for a later release.
- Approval expiry defaults to 900 seconds when unspecified.
- Policy bundles are hashed (`bundle_hash`) for evidence integrity; update the bundle version for every change.
