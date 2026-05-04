/**
 * Environment/config resolution (Node >=18).
 *
 * Inputs:
 *  - SETORRA_API_KEY: required to link org via handshake.
 *  - SETORRA_BACKEND: control-plane base URL (preferred).
 *  - SETORRA_CONTROL_URL: deprecated alias (fallback only).
 *  - SETORRA_ERRORS_AS_OUTPUT: optional flag (true/1/yes/on).
 *  - SETORRA_STORAGE_DIR: optional local sink directory.
 *
 * Privacy: never log raw API keys or tokens. Only log safe prefixes.
 */

export interface ResolvedEnvConfig {
  apiKey?: string;
  backendUrl?: string;
  errorsAsOutput: boolean;
  storageDir?: string;
}

function truthy(val?: string | null): boolean {
  if (!val) return false;
  const s = val.trim().toLowerCase();
  return s === "true" || s === "1" || s === "yes" || s === "on";
}

export function resolveEnv(): ResolvedEnvConfig {
  const apiKey = process.env.SETORRA_API_KEY || undefined;
  const backendPrimary = process.env.SETORRA_BACKEND || undefined;
  const backendFallback = process.env.SETORRA_CONTROL_URL || undefined;
  const backendUrl = backendPrimary || backendFallback || undefined;

  // One-time guidance if deprecated alias is used without preferred var.
  if (!backendPrimary && backendFallback) {
    // eslint-disable-next-line no-console
    console.warn("[setorra-ai] SETORRA_CONTROL_URL is deprecated; prefer SETORRA_BACKEND");
  }

  const errorsAsOutput = truthy(process.env.SETORRA_ERRORS_AS_OUTPUT);
  const storageDir = process.env.SETORRA_STORAGE_DIR || undefined;

  return { apiKey, backendUrl, errorsAsOutput, storageDir };
}

