# Contributing to Setorra

Setorra is an open-source runtime control layer for AI agent actions. The most valuable contributions improve action safety, reliability, policy control, evidence quality, or builder ergonomics.

## Setup

```bash
git clone https://github.com/Yospy/Agent-Runtime-Audit-and-Governance-SDK.git
cd Agent-Runtime-Audit-and-Governance-SDK
python3 -m pip install -e .
python3 -m pip install pytest pyyaml
```

For the TypeScript package:

```bash
cd packages/setorra-ai
npm ci
npm run build
```

The OpenAI Agents SDK demo is optional and should not be required for normal tests:

```bash
python3 -m pip install -r examples/requirements-openai-agent.txt
cp .env.example .env
python3 -m examples.openai_support_refund_agent
```

## Running Tests

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. pytest -q -p no:cacheprovider
```

```bash
cd packages/setorra-ai
npm ci
npm run build
```

## Contribution Areas

- Action registry and action schema improvements
- Policy examples and guardrail bundles
- Connectors for real systems
- Retry, idempotency, fallback, and outcome verification
- Sandbox and dry-run execution
- MCP gateway and framework integrations
- Evidence export, replay, validation, and docs

## Contribution Rules

- Do not commit secrets, `.env`, generated `evidence/`, generated `logs/`, internal `sprints/`, or dependency folders.
- Keep SDK behavior deterministic in automated tests.
- Do not add live API calls to CI.
- Redact sensitive values before persistence or export.
- Add or update tests when changing policy, approvals, redaction, evidence, execution lifecycle, or public APIs.
- Update docs for user-facing behavior.

## DCO Sign-off

This project uses the Developer Certificate of Origin instead of a CLA.

By contributing, you certify that you have the right to submit the contribution and that it can be licensed under the project license. Add a sign-off to every commit:

```bash
git commit -s -m "Your commit message"
```

The sign-off line looks like:

```text
Signed-off-by: Your Name <you@example.com>
```

If you forgot, amend the latest commit:

```bash
git commit --amend --signoff
```

## Pull Request Checklist

- Tests pass locally.
- No secrets or generated artifacts are committed.
- Privacy impact is considered.
- Evidence/schema impact is documented when relevant.
- Public behavior is documented.
- Commits or PR body include a DCO sign-off.

