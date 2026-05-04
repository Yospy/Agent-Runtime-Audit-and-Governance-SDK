/**
 * Minimal, privacy-first redactors for Node SDK.
 *
 * Patterns: emails, phone-like numbers, common key formats (sk-..., AKIA..., api_key=...)
 */

const EMAIL_RE = /\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/g;
const PHONE_RE = /\b(?:\+?\d{1,3}[\s-]?)?(?:\(?\d{3}\)?[\s-]?)?\d{3}[\s-]?\d{4}\b/g;
const OPENAI_KEY_RE = /sk-[A-Za-z0-9]{16,}/g;
const AWS_KEYID_RE = /AKIA[0-9A-Z]{16}/g;
const API_KEY_ASSIGN_RE = /(api[_-]?key)\s*[:=]\s*([A-Za-z0-9_\-]{4,})/gi;

export interface RedactResult<T = unknown> {
  redacted: T;
  privacy_flags: string[];
  redacted_fields: string[];
}

export function redact<T = unknown>(input: T, previewLimit = 512): RedactResult<T> {
  const flags = new Set<string>();
  const redactedFields: string[] = [];

  function redactString(s: string, path: string): string {
    let out = s;
    function apply(re: RegExp, replacement: string, flag: string) {
      if (re.test(out)) {
        out = out.replace(re, replacement);
        flags.add(flag);
        redactedFields.push(path || "$);");
      }
    }
    apply(EMAIL_RE, "[email]", "email");
    apply(PHONE_RE, "[phone]", "phone");
    apply(OPENAI_KEY_RE, "[secret]", "secret");
    apply(AWS_KEYID_RE, "[id]", "id");
    if (API_KEY_ASSIGN_RE.test(out)) {
      out = out.replace(API_KEY_ASSIGN_RE, (_m, key) => `${key}=[secret]`);
      flags.add("secret");
      redactedFields.push(path || "$);");
    }
    // Clip to preview budget to avoid large storage writes
    if (out.length > previewLimit) out = out.slice(0, previewLimit);
    return out;
  }

  function walk(value: unknown, path: string): unknown {
    if (typeof value === "string") return redactString(value, path);
    if (Array.isArray(value)) return value.map((v, i) => walk(v, `${path}[${i}]`));
    if (value && typeof value === "object") {
      const out: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(value)) out[k] = walk(v, path ? `${path}.${k}` : k);
      return out as T;
    }
    return value;
  }

  const redacted = walk(input, "") as T;
  return { redacted, privacy_flags: Array.from(flags).sort(), redacted_fields: redactedFields.sort() };
}

