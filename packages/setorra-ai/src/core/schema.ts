/**
 * Schema types for Setorra Node SDK (parity with Python v1.3).
 *
 * Additive-only: do not remove or rename fields once released.
 */

export const SCHEMA_VERSION = "1.3";

export interface EvidenceEvent {
  schema_version: string;
  session_id: string;
  event_index: number;
  timestamp: string; // ISO-8601 Z
  agent_name: string;
  agent_version: string;
  event_type: string;
  parameters_redacted: unknown;
  privacy_flags: string[];
  redacted_fields: string[];
  context: Record<string, unknown>;
  execution: Record<string, unknown>;
  policy: Record<string, unknown> | null;
  approval: Record<string, unknown> | null;
  guardrail: Record<string, unknown> | null;
  integrity: { prev_hash: string | null; event_hash: string; signature?: string | null };
}

export interface OutputSummary {
  schema_version: string;
  session_id: string;
  agent_name: string;
  agent_version: string;
  started_at: string;
  finished_at: string;
  duration_ms: number;
  model_id?: string | null;
  prompts: Record<string, { hash: string | null; redacted: string | null }>;
  final_output: unknown;
  run_result: Record<string, unknown>;
  tools_used: Array<Record<string, unknown>>;
  reasoning: Array<Record<string, unknown>>;
  consents: Array<Record<string, unknown>>;
  latency_ms?: number | null;
  cost_estimate?: number | null;
  environment_info: Record<string, unknown>;
  policy_summary: Record<string, unknown>;
  guardrail_summary: Record<string, unknown>;
  actions?: {
    total: number;
    by_status: Record<string, number>;
    final_statuses: Array<Record<string, unknown>>;
  };
  integrity: { first_event_hash: string | null; last_event_hash: string | null; count: number; signature?: string | null };
  organization?: { org_id: string; org_name?: string; key_id_prefix?: string };
}
