# Security Policy

Setorra handles policy decisions, approvals, redaction, and audit evidence for AI agent actions. Please report security issues privately.

## Reporting a Vulnerability

Do not open a public issue for:

- secret or credential exposure
- redaction bypasses
- policy bypasses
- approval binding flaws
- integrity-chain tampering
- connector credential leakage
- evidence export privacy issues

Report vulnerabilities by emailing:

```text
security@setorra.dev
```

If that address is unavailable, contact the maintainer privately through GitHub and request a secure disclosure path.

Include:

- affected version or commit
- reproduction steps
- impact
- whether secrets or personal data may be exposed
- suggested fix, if known

## Supported Versions

Until the project publishes stable releases, security fixes target the default branch.

## Security Expectations for Contributions

- Never log raw API keys, tokens, passwords, or private customer payloads.
- Preserve redaction-before-persistence behavior.
- Keep approval decisions bound to exact action intent and payload/context hashes.
- Add tests for policy, approval, redaction, and integrity changes.

