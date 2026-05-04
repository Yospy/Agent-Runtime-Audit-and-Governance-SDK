# Action Firewall SDK Revamp Todo

## Active Plan

Plan 5: Add Connector and Gateway Foundations.

## Tasks

- [x] Complete Plan 1 stabilization checkpoint.
- [x] Complete Plan 2 action firewall API checkpoint.
- [x] Complete Plan 3 action lifecycle checkpoint.
- [x] Complete Plan 4 approval runtime checkpoint.
- [x] Add generic connector interface.
- [x] Add generic HTTP connector with allowlisted action mapping.
- [x] Add Stripe refund connector stub with idempotency support.
- [x] Add Slack approval connector stub.
- [x] Add local gateway/MCP design document and entrypoint.
- [x] Add connector safety tests.

## Verification

- [x] `PYTHONPATH=. SETORRA_DISABLE_VALIDATION=1 pytest -q`
- [x] `npm run build` in `packages/setorra-ai`
- [x] Diff review against sprint scope.
