/**
 * Minimal event types aligned with Python SDK schema.
 *
 * Stamping rule: every emitted event carries context.organization
 * when handshake succeeded (org linked). Offline mode omits it.
 */

export interface OrganizationInfo {
  org_id: string;
  org_name?: string;
  key_id_prefix?: string;
}

export interface EventContext {
  organization?: OrganizationInfo;
  // Future: model, prompt hash, environment metadata, etc.
}

export interface SetorraEvent<T = Record<string, unknown>> {
  type: string;
  time: string; // ISO 8601
  context: EventContext;
  payload: T;
}

