# Redaction (Privacy-First)

Setorra masks PII and secrets before writing any artifacts. Redaction applies to
prompts (system/user), error previews, and the final output preview. Raw PII is
never persisted.

## What is Masked

Placeholders are deterministic and appear in redacted previews; `privacy_flags`
carry detector names for evidence queries.

- Email → `[email]`
- Phone (E.164 and US 3-3-4) → `[phone]`
- OpenAI keys `sk-…` and generic API key assignments → `[secret]`
- JSON‑quoted secrets: only the value is masked → `[secret]`
- Authorization Bearer tokens → `[bearer]`
- JWT (`xxx.yyy.zzz`) → `[jwt]`
- PEM private keys → `[pem]`
- Vendor/cloud tokens (GitHub/Slack/Discord/HF/GCP) → `[token]`
- AWS Access Key ID `AKIA…` → `[id]`
- Card PAN (Luhn) → `[card]`
- IBAN (mod‑97) → `[iban]`
- ABA routing (checksum; context) → `[routing]`
- Account number (context) → `[acct]`
- SSN (context) → `[ssn]`
- IPv4 (standard); IPv6 (strict) → `[ip]`
- DOB (context; strict) → `[dob]`
- Address (context; strict) → `[address]`

Context-gated detectors require nearby keywords to reduce false positives.

## Configuration

Environment toggles (optional; defaults are safe and backward‑compatible):

- `SETORRA_REDACTION_LEVEL` = `standard` | `strict`
  - Standard: default; includes core detectors with conservative patterns.
  - Strict: includes additional detectors (IPv6, DOB, Address) that can be
    noisier without context.
- `SETORRA_REDACTION_DISABLE` — comma‑separated flags to disable, e.g.
  `dob,address`.
- `SETORRA_REDACTION_ENABLE` — comma‑separated flags to force enable.

Detector flags can appear in `privacy_flags`: `email`, `phone`, `secret`, `id`,
`card`, `iban`, `routing`, `acct`, `ssn`, `jwt`, `bearer`, `pem`, `token`,
`ip`, `dob`, `address`.

## Performance

- All regexes are precompiled; heavy/ambiguous patterns are validated (e.g.,
  Luhn for PAN, mod‑97 for IBAN, checksum for ABA).
- Detectors run in an ordered registry to minimize re‑work and avoid pattern
  collisions (PAN before phone, etc.).
- Context‑gated rules keep false positives low without scanning entire inputs.

## Evidence Mapping

- `prompt.capture.parameters_redacted.{system|user}.redacted` — preview with
  placeholders.
- `privacy_flags` — detector names present in the event.
- `redacted_fields` — field path list indicating where masking occurred.

See `docs/SCHEMA.md` for full event structure.

