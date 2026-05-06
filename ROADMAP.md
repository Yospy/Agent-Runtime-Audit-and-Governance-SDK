# Roadmap

Setorra is moving toward an action-agnostic runtime control layer for AI agents with production write access.

## Current Foundation

- Action firewall API
- Policy decisions: allow, deny, require approval, needs more context
- Local approval binding
- Tamper-evident evidence artifacts
- Privacy-first redaction
- Python SDK and early TypeScript SDK
- OpenAI Agents SDK demo

## Near-Term Contributor Lanes

### Action Reliability

- retry and timeout primitives
- idempotency helpers
- fallback execution hooks
- duplicate-action prevention

### Outcome Verification

- connector verification hooks
- before/after state capture
- failed-action classification
- evidence links from action result to verifier result

### Sandbox and Dry Run

- dry-run executor contract
- sandbox-only policy decision support
- preview artifacts for approval review

### Connectors

- Slack approval connector
- GitHub issue/PR action connector
- Zendesk/Intercom ticket connector
- Stripe billing connector
- generic webhook connector

### Policy and Guardrails

- reusable policy examples
- action cap templates
- guardrail examples for malformed or risky payloads
- docs for builder-defined action taxonomies

### Gateway and MCP

- local action gateway hardening
- MCP tool exposure for governed actions
- examples for Claude, OpenAI Agents SDK, LangChain, and custom agents

## Out of Scope for OSS SDK

Hosted dashboards, SSO, managed retention, organization policy distribution, and enterprise evidence review workflows may be built separately from the OSS SDK.

