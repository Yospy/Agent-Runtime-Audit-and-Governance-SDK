"""Privacy-first redaction utilities for Setorra SDK (Python).

This module focuses exclusively on prompt/output redaction and does not alter
any other SDK behavior. It provides a robust yet performant detector registry
that masks PII and secrets before persistence, returning redacted previews only.

Backward compatibility:
- Public function signature remains: `redact(obj) -> (redacted, flags, redacted_fields)`.
- Existing detector flags and placeholders are preserved: email → [email], phone → [phone],
  OpenAI key → [secret], AWS Access Key ID → [id].

Enhancements:
- Finance PII: PAN/Luhn → [card], CVV (context) → [cvv], IBAN/mod‑97 → [iban],
  ABA routing checksum (context) → [routing], account numbers (context) → [acct], SSN → [ssn].
- Secrets: JWT → [jwt], Authorization Bearer → [bearer], PEM private keys → [pem],
  vendor/cloud tokens (GitHub/Slack/Discord/HF/GCP/…) → [token], .env & JSON-quoted secrets → [secret].
- Identity: E.164 phones (in addition to US-like), IPv4/IPv6 → [ip], DOB (context) → [dob],
  addresses (context, heuristic) → [address].

Performance & safety:
- Patterns are precompiled; validators (Luhn/IBAN/ABA) guard expensive or ambiguous matches.
- Context-gated detectors avoid false positives for ambiguous numeric tokens.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

# ---------------------------------------------------------------------------
# Backward‑compatible, exported baseline regexes (kept for importers/tests)
# ---------------------------------------------------------------------------
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(
    r"\b(?:\(?\d{3}\)?[\-\s]?)\d{3}[\-\s]?\d{4}\b"
)
OPENAI_KEY_RE = re.compile(r"sk-[A-Za-z0-9]{16,}")
AWS_KEYID_RE = re.compile(r"AKIA[0-9A-Z]{16}")
API_KEY_ASSIGN_RE = re.compile(r"(api[_-]?key)\s*[:=]\s*([A-Za-z0-9_\-]{4,})", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Additional detector patterns (new)
# ---------------------------------------------------------------------------
PHONE_E164_RE = re.compile(r"\+\d{7,15}")
PHONE_US_334_RE = re.compile(r"\b\d{3}[-\s]?\d{3}[-\s]?\d{4}\b")
IPV4_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d{1,2})\.){3}(?:25[0-5]|2[0-4]\d|1?\d{1,2})\b"
)
# Simplified IPv6 (balanced coverage without excessive complexity)
IPV6_RE = re.compile(r"\b(?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{0,4}\b")

# JSON-quoted secret assignments: "api_key": "...", 'token': '...'
JSON_SECRET_ASSIGN_RE = re.compile(
    r"(?i)([\"'](?:api[-_]?key|openai_api_key|aws_secret_access_key|google_api_key|hf[_-]?token|huggingface[_-]?token|slack[_-]?token|discord[_-]?token|token|secret|password|client[_-]?secret|private[_-]?key)[\"']\s*:\s*[\"'])([^\"']{4,})([\"'])"
)

# Authorization header (works in plain text or JSON values)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{10,}\b")

# Vendor/cloud token examples
GITHUB_TOKEN_RE = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")
SLACK_TOKEN_RE = re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")
DISCORD_MFA_RE = re.compile(r"\bmfa\.[A-Za-z0-9_-]{20,}\b")
HF_TOKEN_RE = re.compile(r"\bhf_[A-Za-z0-9]{20,}\b")
GCP_API_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b")

# PEM private keys
PEM_PRIVATE_RE = re.compile(
    r"-----BEGIN(?: [A-Z]+)? PRIVATE KEY-----[\s\S]+?-----END(?: [A-Z]+)? PRIVATE KEY-----",
    re.MULTILINE,
)

# Finance identifiers
PAN_CANDIDATE_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
IBAN_RE = re.compile(r"\b[A-Z]{2}[0-9A-Z]{13,32}\b", re.IGNORECASE)
ABA_ROUTING_RE = re.compile(r"\b\d{9}\b")
SSN_RE = re.compile(
    r"\b(?!000|666|9\d\d)\d{3}[- ]?(?!00)\d{2}[- ]?(?!0000)\d{4}\b"
)
CVV_RE = re.compile(r"\b\d{3,4}\b")
ACCOUNT_NUMBER_RE = re.compile(r"\b\d{6,20}\b")

# JWT tokens: three base64url segments (commonly start with 'eyJ')
JWT_RE = re.compile(r"\b[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")


# ---------------------------------------------------------------------------
# Validators and helpers
# ---------------------------------------------------------------------------
def _digits_only(s: str) -> str:
    return re.sub(r"\D+", "", s)


def _luhn_ok(s: str) -> bool:
    n = _digits_only(s)
    if not (13 <= len(n) <= 19):
        return False
    total = 0
    odd = False
    for ch in reversed(n):
        d = ord(ch) - 48
        if odd:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        odd = not odd
    return (total % 10) == 0


def _iban_ok(s: str) -> bool:
    raw = re.sub(r"\s+", "", s).upper()
    if not (15 <= len(raw) <= 34):
        return False
    if not re.match(r"^[A-Z]{2}[0-9A-Z]+$", raw):
        return False
    # Move first four chars to the end
    rearranged = raw[4:] + raw[:4]
    # Replace letters with numbers A=10, ..., Z=35
    digits = []
    for ch in rearranged:
        if ch.isdigit():
            digits.append(ch)
        else:
            digits.append(str(ord(ch) - 55))
    number = "".join(digits)
    # Compute mod 97 incrementally to avoid big ints
    mod = 0
    for c in number:
        mod = (mod * 10 + (ord(c) - 48)) % 97
    return mod == 1


def _aba_ok(s: str) -> bool:
    n = _digits_only(s)
    if len(n) != 9:
        return False
    a = sum(int(n[i]) for i in (0, 3, 6))
    b = sum(int(n[i]) for i in (1, 4, 7))
    c = sum(int(n[i]) for i in (2, 5, 8))
    return (3 * a + 7 * b + c) % 10 == 0


# ---------------------------------------------------------------------------
# Detector framework
# ---------------------------------------------------------------------------
RedactValidator = Callable[[re.Match[str], str], bool]


@dataclass
class Detector:
    """A single redaction rule.

    pattern: compiled regex to find candidate substrings.
    placeholder: replacement string (e.g., "[email]").
    flag: privacy flag to add when redaction occurs.
    validator: optional predicate to validate a match before redacting.
    context_keywords: optional list of keywords that must be present within a
      small window around the match; used for ambiguous tokens (CVV, account, address, DOB).
    replace_group: if set, only the specified capturing group is replaced; the
      rest of the match is preserved (useful for JSON assignments).
    """

    pattern: re.Pattern[str]
    placeholder: str
    flag: str
    validator: Optional[RedactValidator] = None
    context_keywords: Optional[Sequence[str]] = None
    replace_group: Optional[int] = None

    def apply(self, text: str, flags: Set[str]) -> Tuple[str, bool]:
        changed = False
        pattern = self.pattern
        placeholder = self.placeholder
        replace_group = self.replace_group
        ctx = None

        def ok(m: re.Match[str]) -> bool:
            nonlocal ctx
            if self.validator and not self.validator(m, text):
                return False
            if self.context_keywords:
                start, end = m.start(), m.end()
                # Window of ±64 characters
                s = max(0, start - 64)
                e = min(len(text), end + 64)
                ctx = text[s:e].lower()
                if not any(k.lower() in ctx for k in self.context_keywords):
                    return False
            return True

        def repl(m: re.Match[str]) -> str:
            nonlocal changed
            if not ok(m):
                return m.group(0)
            changed = True
            if replace_group is None:
                return placeholder
            # Replace only the specified group, preserve surrounding text
            groups = list(m.groups(default=""))
            # Compute slices
            start, end = m.span(replace_group)
            return text[m.start():start] + placeholder + text[end:m.end()]

        new_text = pattern.sub(repl, text)
        if changed:
            flags.add(self.flag)
        return new_text, changed


def _make_detectors(level: str, disable: Set[str], enable: Set[str]) -> List[Detector]:
    """Construct the ordered list of detectors based on level and toggles.

    level: 'standard' (default) or 'strict'.
    disable/enable: per-flag overrides from env.
    """
    # Helper to include/exclude by flag
    def allow(flag: str, default_on: bool = True) -> bool:
        if flag in disable:
            return False
        if flag in enable:
            return True
        return default_on

    detectors: List[Detector] = []

    # Secrets and tokens (cheap, high signal)
    if allow("secret"):
        detectors.append(Detector(JSON_SECRET_ASSIGN_RE, "[secret]", "secret", replace_group=2))
        detectors.append(Detector(OPENAI_KEY_RE, "[secret]", "secret"))
        detectors.append(Detector(API_KEY_ASSIGN_RE, "[secret]", "secret", replace_group=2))
    if allow("bearer"):
        detectors.append(Detector(BEARER_RE, "[bearer]", "bearer"))
    if allow("jwt"):
        detectors.append(Detector(JWT_RE, "[jwt]", "jwt"))
    if allow("pem"):
        detectors.append(Detector(PEM_PRIVATE_RE, "[pem]", "pem"))
    if allow("token"):
        detectors.extend(
            [
                Detector(GITHUB_TOKEN_RE, "[token]", "token"),
                Detector(SLACK_TOKEN_RE, "[token]", "token"),
                Detector(DISCORD_MFA_RE, "[token]", "token"),
                Detector(HF_TOKEN_RE, "[token]", "token"),
                Detector(GCP_API_KEY_RE, "[token]", "token"),
            ]
        )

    # Finance identifiers (card before phone to avoid false phone masking inside PAN; validator avoids phone-like runs)
    if allow("card"):
        detectors.append(
            Detector(
                PAN_CANDIDATE_RE,
                "[card]",
                "card",
                validator=lambda m, t: _luhn_ok(m.group(0)) and not PHONE_US_334_RE.search(m.group(0)),
            )
        )
    if allow("iban"):
        detectors.append(Detector(IBAN_RE, "[iban]", "iban", validator=lambda m, t: _iban_ok(m.group(0))))
    if allow("routing"):
        detectors.append(
            Detector(ABA_ROUTING_RE, "[routing]", "routing", validator=lambda m, t: _aba_ok(m.group(0)), context_keywords=["routing", "aba"])
        )
    if allow("ssn"):
        detectors.append(Detector(SSN_RE, "[ssn]", "ssn", context_keywords=["ssn", "social", "social security", "tax id", "tin"]))
    if allow("cvv"):
        detectors.append(Detector(CVV_RE, "[cvv]", "cvv", context_keywords=["cvv", "cvc", "security code"]))
    if allow("acct"):
        detectors.append(Detector(ACCOUNT_NUMBER_RE, "[acct]", "acct", context_keywords=["account", "acct", "iban", "sort code", "ifsc"]))

    # Identity & keys
    if allow("id"):
        detectors.append(Detector(AWS_KEYID_RE, "[id]", "id"))

    # Emails and phones
    if allow("email"):
        detectors.append(Detector(EMAIL_RE, "[email]", "email"))
    if allow("phone"):
        detectors.append(Detector(PHONE_E164_RE, "[phone]", "phone"))
        detectors.append(
            Detector(
                PHONE_RE,
                "[phone]",
                "phone",
                validator=lambda m, t: (m.start() == 0 or not t[m.start() - 1].isdigit())
                and (m.end() == len(t) or not t[m.end() : m.end() + 1].isdigit()),
            )
        )

    # IP addresses
    if allow("ip"):
        detectors.append(Detector(IPV4_RE, "[ip]", "ip"))
        if level == "strict":
            detectors.append(Detector(IPV6_RE, "[ip]", "ip"))

    # DOB and addresses (strict by default to reduce FPs)
    if allow("dob", default_on=(level == "strict")):
        dob_re = re.compile(
            r"\b(?:(?:19|20)\d{2}[-/](?:0?[1-9]|1[0-2])[-/](?:0?[1-9]|[12]\d|3[01])|(?:0?[1-9]|1[0-2])[-/](?:0?[1-9]|[12]\d|3[01])[-/](?:19|20)\d{2})\b"
        )
        detectors.append(Detector(dob_re, "[dob]", "dob", context_keywords=["dob", "birth", "date of birth", "birthday"]))
    if allow("address", default_on=(level == "strict")):
        addr_re = re.compile(r"\b\d{1,6}\s+[A-Za-z0-9.'\-]+\s+(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Lane|Ln|Dr|Drive|Ct|Court|Way|Pkwy|Parkway)\b", re.IGNORECASE)
        detectors.append(Detector(addr_re, "[address]", "address", context_keywords=["address", "mailing", "shipping", "billing", "street"]))

    return detectors


def _load_env_toggles() -> Tuple[str, Set[str], Set[str]]:
    level = os.getenv("SETORRA_REDACTION_LEVEL", "standard").strip().lower()
    if level not in {"standard", "strict"}:
        level = "standard"
    dis = {s.strip().lower() for s in os.getenv("SETORRA_REDACTION_DISABLE", "").split(",") if s.strip()}
    en = {s.strip().lower() for s in os.getenv("SETORRA_REDACTION_ENABLE", "").split(",") if s.strip()}
    return level, dis, en


_LEVEL, _DISABLE, _ENABLE = _load_env_toggles()
_DETECTORS: List[Detector] = _make_detectors(_LEVEL, _DISABLE, _ENABLE)


def redact(obj: Any) -> Tuple[Any, Set[str], List[str]]:
    """Redact PII/secrets from arbitrary JSON-like objects.

    Returns `(redacted_obj, privacy_flags, redacted_fields)`.
    `redacted_fields` contains string paths (dot/array notation) where masking occurred.
    """
    flags: Set[str] = set()
    redacted_fields: List[str] = []

    def _redact_str(s: str) -> Tuple[str, bool]:
        changed = False
        text = s
        # Apply detectors in order; placeholders do not re-match later rules.
        for det in _DETECTORS:
            text2, ch = det.apply(text, flags)
            if ch:
                changed = True
                text = text2
        return text, changed

    def _walk(o: Any, path: str) -> Any:
        if isinstance(o, str):
            replaced, changed = _redact_str(o)
            if changed:
                redacted_fields.append(path or "$")
            return replaced
        if isinstance(o, list):
            return [_walk(item, f"{path}[{idx}]" if path else f"$[{idx}]") for idx, item in enumerate(o)]
        if isinstance(o, tuple):
            return tuple(_walk(item, f"{path}[{idx}]" if path else f"$[{idx}]") for idx, item in enumerate(o))
        if isinstance(o, dict):
            result: Dict[str, Any] = {}
            for key, value in o.items():
                child_path = f"{path}.{key}" if path else key
                result[key] = _walk(value, child_path)
            return result
        return o

    redacted_obj = _walk(obj, "")
    return redacted_obj, flags, redacted_fields
