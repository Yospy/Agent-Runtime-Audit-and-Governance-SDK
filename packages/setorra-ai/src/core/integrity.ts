import crypto from "node:crypto";

/**
 * Return a canonical JSON string with stable key ordering.
 * Keeps numbers/booleans/strings as-is; sorts object keys recursively.
 */
export function canonicalJson(value: unknown): string {
  return JSON.stringify(sortKeys(value), (_k, v) => v, 0);
}

function sortKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(value as Record<string, unknown>).sort()) {
      out[key] = sortKeys((value as Record<string, unknown>)[key]);
    }
    return out;
  }
  return value;
}

/** Compute SHA-256 over canonical JSON of the event. */
export function computeEventHash(evt: Record<string, unknown>): string {
  const copy = JSON.parse(canonicalJson(evt));
  // Ensure signature field does not influence the hash
  if (copy.integrity && typeof copy.integrity === "object") {
    delete (copy.integrity as Record<string, unknown>)["signature"];
  }
  const input = canonicalJson(copy);
  return crypto.createHash("sha256").update(input, "utf8").digest("hex");
}

