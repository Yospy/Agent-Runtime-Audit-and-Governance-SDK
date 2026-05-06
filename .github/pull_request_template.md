## Summary

Describe the change and why it matters.

## Type of Change

- [ ] Bug fix
- [ ] Feature
- [ ] Connector
- [ ] Policy/guardrail example
- [ ] Documentation
- [ ] CI/tooling

## Verification

- [ ] Python tests pass: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. pytest -q -p no:cacheprovider`
- [ ] TypeScript build passes, if touched: `cd packages/setorra-ai && npm ci && npm run build`
- [ ] No live API calls are required for automated tests

## Security and Privacy

- [ ] No secrets, `.env`, generated `evidence/`, generated `logs/`, internal `sprints/`, or dependency folders are committed
- [ ] Redaction behavior is preserved or updated with tests
- [ ] Policy/approval/evidence changes are documented
- [ ] Evidence schema impact is documented, if any

## DCO

- [ ] My commits include `Signed-off-by: Name <email>` or I include the sign-off below:

Signed-off-by:

