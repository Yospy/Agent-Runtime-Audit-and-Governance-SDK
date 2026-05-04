/**
 * Handshake client: API key -> { org_id, org_name?, key_id, session_token, expires_in }.
 *
 * Contract (parity with Python SDK and local backend):
 *  - POST {backend}/v1/auth/handshake with header Authorization: Bearer <api_key>.
 *  - 200: { org_id, org_name?, key_id, session_token, expires_in }
 *  - 401: { error: "invalid_api_key" }
 *  - 403: { error: "revoked_key", key_id? }
 *
 * Privacy: never log raw api keys or tokens. Safe prefixes only.
 */

export interface HandshakeResponse {
  org_id: string;
  org_name?: string;
  key_id: string;
  session_token: string;
  expires_in: number;
}

export interface HandshakeResult extends HandshakeResponse {
  key_id_prefix?: string; // derived locally for safe logging
}

export class HandshakeError extends Error {
  public readonly status?: number;
  public readonly code: string;
  constructor(message: string, code: string, status?: number) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

function safePrefix(s: string, n = 6): string {
  return s.slice(0, Math.max(0, n));
}

function normalizeBase(url: string): string {
  return url.endsWith("/") ? url.slice(0, -1) : url;
}

async function postJson(url: string, headers: Record<string, string>): Promise<Response> {
  return fetch(url, {
    method: "POST",
    headers,
    // No body required by the current handshake contract
  });
}

export class HandshakeClient {
  /**
   * Perform handshake; retries once replacing localhost with 127.0.0.1 if connection fails.
   */
  static async handshake(backendBaseUrl: string, apiKey: string): Promise<HandshakeResult> {
    const base = normalizeBase(backendBaseUrl);
    const endpoint = `${base}/v1/auth/handshake`;

    const headers = { Authorization: `Bearer ${apiKey}` };

    try {
      const res = await postJson(endpoint, headers);
      return await this.#parseResponse(res);
    } catch (err) {
      // IPv6 localhost pitfall: retry with 127.0.0.1 if host is localhost
      try {
        const url = new URL(backendBaseUrl);
        if (url.hostname === "localhost") {
          url.hostname = "127.0.0.1";
          const retryEndpoint = `${normalizeBase(url.toString())}/v1/auth/handshake`;
          const res2 = await postJson(retryEndpoint, headers);
          return await this.#parseResponse(res2);
        }
      } catch {
        // ignore URL parse errors; fall through to throw original
      }
      throw err;
    }
  }

  static async #parseResponse(res: Response): Promise<HandshakeResult> {
    const status = res.status;
    const text = await res.text();
    let parsed: any;
    try {
      parsed = text ? JSON.parse(text) : {};
    } catch {
      throw new HandshakeError("invalid_json", "invalid_json", status);
    }

    if (status === 200) {
      const { org_id, org_name, key_id, session_token, expires_in } = parsed as HandshakeResponse;
      if (!org_id || !key_id || !session_token || typeof expires_in !== "number") {
        throw new HandshakeError("invalid_payload", "invalid_payload", status);
      }
      return { org_id, org_name, key_id, session_token, expires_in, key_id_prefix: safePrefix(key_id) };
    }

    if (status === 401) {
      throw new HandshakeError("invalid_api_key", "invalid_api_key", status);
    }
    if (status === 403) {
      throw new HandshakeError("revoked_key", "revoked_key", status);
    }

    throw new HandshakeError(`unexpected_status_${status}`, "unexpected_status", status);
  }
}

