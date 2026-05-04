import { resolveEnv } from "../config/env.js";
import { HandshakeClient, HandshakeResult } from "../remote/handshake.js";
import { FileSink } from "../sinks/fileSink.js";
import type { EventContext, OrganizationInfo, SetorraEvent } from "./events.js";
import { SCHEMA_VERSION, type EvidenceEvent, type OutputSummary } from "./schema.js";
import { canonicalJson, computeEventHash } from "./integrity.js";
import { redact } from "./redact.js";
import { join } from "node:path";
import { writeFileSync } from "node:fs";
import crypto from "node:crypto";

/** Soft buffer limit per session (~10MB). */
const DEFAULT_MAX_BUFFER_BYTES = 10 * 1024 * 1024;

/**
 * SetorraCollector (Node): minimal collector with optional handshake.
 * - On create, attempts handshake if env present; otherwise runs offline.
 * - Stamps context.organization on every event when linked.
 * - Persists events to NDJSON via FileSink (redaction expected upstream).
 *
 * Non-goals (for initial cut): policy/guardrail enforcement, signatures, chain.
 */
export class SetorraCollector {
  private readonly sink: FileSink;
  private readonly errorsAsOutput: boolean;
  private org?: OrganizationInfo; // when linked
  private readonly storageDir: string;

  // Session state (null when no active session)
  private _sessionId: string | null = null;
  private _agentName = "";
  private _agentVersion = "";
  private _startedAt: Date | null = null;
  private _events: EvidenceEvent[] = [];
  private _bytesEstimate = 0;
  private _firstHash: string | null = null;
  private _lastHash: string | null = null;
  private _prompts: Record<string, { hash: string | null; redacted: string | null }> = {};
  private _promptHashes: Record<string, string | null> = {};
  private _environmentInfo: Record<string, unknown> = {};
  private _runResult: Record<string, unknown> = {};
  private _outcomeEmitted = false;
  private _capturePlans = false;
  private _currentPlanId: string | null = null;
  private _currentPlanVersion = 0;
  private _maxBufferBytes = DEFAULT_MAX_BUFFER_BYTES;
  private _actionStatuses = new Map<string, Record<string, unknown>>();
  private _actionTotal = 0;

  private constructor(opts: { sink: FileSink; errorsAsOutput: boolean; org?: OrganizationInfo; storageDir: string }) {
    this.sink = opts.sink;
    this.errorsAsOutput = opts.errorsAsOutput;
    this.org = opts.org;
    this.storageDir = opts.storageDir;
  }

  /** Return currently linked organization (if any). */
  organization(): OrganizationInfo | undefined {
    return this.org;
  }

  /** Emit a minimal event, stamping organization if available. */
  async emit<T extends Record<string, unknown>>(type: string, payload: T): Promise<void> {
    const context: EventContext = {};
    if (this.org) context.organization = this.org;
    const evt: SetorraEvent<T> = {
      type,
      time: new Date().toISOString(),
      context,
      payload,
    };
    await this.sink.append(JSON.stringify(evt));
  }

  async close(): Promise<void> {
    // No background workers yet; nothing to flush.
  }

  /**
   * Factory: builds a collector using env. Handshake is optional and non-blocking.
   * Logs only safe identifiers.
   */
  static async create(): Promise<SetorraCollector> {
    const cfg = resolveEnv();
    const sink = new FileSink(cfg.storageDir || "data", "events.ndjson");

    let org: OrganizationInfo | undefined;
    if (cfg.backendUrl && cfg.apiKey) {
      try {
        const hs: HandshakeResult = await HandshakeClient.handshake(cfg.backendUrl, cfg.apiKey);
        org = { org_id: hs.org_id, org_name: hs.org_name, key_id_prefix: hs.key_id_prefix };
        // eslint-disable-next-line no-console
        console.log(
          `[setorra-ai] linked org_id=${hs.org_id}` + (hs.org_name ? ` org_name=${hs.org_name}` : "") +
            (hs.key_id_prefix ? ` key_id_prefix=${hs.key_id_prefix}` : "")
        );
      } catch (err: any) {
        // eslint-disable-next-line no-console
        console.warn(`[setorra-ai] handshake unavailable (${err?.code || err?.message || "unknown"}); running offline`);
      }
    } else {
      // eslint-disable-next-line no-console
      console.log("[setorra-ai] offline (missing SETORRA_API_KEY or SETORRA_BACKEND)");
    }

    return new SetorraCollector({ sink, errorsAsOutput: cfg.errorsAsOutput, org, storageDir: cfg.storageDir || "data" });
  }

  // ----------------------- Session API (additive) -----------------------

  /** Start a session; emits agent.started, session.manifest, prompt.capture. */
  startSession(opts?: {
    agentName?: string;
    agentVersion?: string;
    environment?: Record<string, unknown>;
    systemPrompt?: string | null;
    userPrompt?: string | null;
  }): string {
    if (this._sessionId) throw new Error("A session is already active. Call endSession() first.");
    const sid = ulid();
    this._sessionId = sid;
    this._agentName = opts?.agentName || "node-agent";
    this._agentVersion = opts?.agentVersion || "0.0.0";
    this._startedAt = new Date();
    this._events = [];
    this._bytesEstimate = 0;
    this._firstHash = null;
    this._lastHash = null;
    this._runResult = {};
    this._outcomeEmitted = false;
    this._environmentInfo = opts?.environment || {};
    this._capturePlans = truthy(process.env.SETORRA_CAPTURE_PLANS);
    this._currentPlanId = null;
    this._currentPlanVersion = 0;
    this._actionStatuses = new Map();
    this._actionTotal = 0;

    // Prepare prompts
    const sys = opts?.systemPrompt || null;
    const usr = opts?.userPrompt || null;
    const sysHash = sys ? sha256(sys) : null;
    const usrHash = usr ? sha256(usr) : null;
    const sysRed = sys ? redact(sys).redacted : null;
    const usrRed = usr ? redact(usr).redacted : null;
    this._prompts = { system: { hash: sysHash, redacted: sysRed }, user: { hash: usrHash, redacted: usrRed } };
    this._promptHashes = { system: sysHash, user: usrHash };

    // Emit started
    this._emitEvent("agent.started", {}, { status: "started" }, { environment: this._environmentInfo });
    // Manifest
    const manifestParams: Record<string, unknown> = { environment: this._environmentInfo };
    this._emitEvent("session.manifest", manifestParams, { status: "ready" });
    // Prompt capture
    const promptParams = { system: this._prompts.system, user: this._prompts.user };
    this._emitEvent("prompt.capture", promptParams, { status: "captured" }, { prompts: this._promptHashes });
    return sid;
  }

  /** Record a generic action (tool/llm/etc.) with redaction + context stitching. */
  recordAction(eventType: string, opts?: {
    parameters?: unknown;
    status?: string;
    modelId?: string;
    promptHashes?: Record<string, string | null>;
    environment?: Record<string, unknown>;
    outputPreview?: unknown;
    latencyMs?: number;
    inputHash?: string;
    outputHash?: string;
    reasoningHash?: string;
    reasoningPreview?: unknown;
    errorType?: string;
    errorPreview?: unknown;
    policy?: Record<string, unknown> | null;
    toolName?: string;
    approval?: Record<string, unknown> | null;
    guardrail?: Record<string, unknown> | null;
  }): string | null {
    if (!this._sessionId) return null;

    const { redacted: pRed, privacy_flags: flagsP, redacted_fields: fieldsP } = redact(opts?.parameters ?? {});
    const { redacted: outRed, privacy_flags: flagsOut, redacted_fields: fieldsOut } = redact(opts?.outputPreview ?? null);
    const { redacted: errRed, privacy_flags: flagsErr, redacted_fields: fieldsErr } = redact(opts?.errorPreview ?? null);
    const { redacted: reasonRed, privacy_flags: flagsReason, redacted_fields: fieldsReason } = redact(
      opts?.reasoningPreview ?? null
    );
    const privacyFlags = Array.from(new Set([...flagsP, ...flagsOut, ...flagsErr, ...flagsReason])).sort();
    const redactedFields = Array.from(new Set([...fieldsP, ...fieldsOut, ...fieldsErr, ...fieldsReason])).sort();

    const exec: Record<string, unknown> = {};
    if (opts?.status) exec.status = opts.status; else exec.status = exec.status || "success";
    if (opts?.latencyMs != null) exec.latency_ms = opts.latencyMs;
    if (opts?.inputHash) exec.input_hash = opts.inputHash;
    if (opts?.outputHash) exec.output_hash = opts.outputHash;
    if (opts?.reasoningHash) exec.reasoning_hash = opts.reasoningHash;
    if (reasonRed != null) exec.reasoning_preview = reasonRed;
    if (outRed != null) exec.output_preview = outRed;
    if (opts?.errorType) exec.error_type = opts.errorType;
    if (errRed != null) exec.error_preview = errRed;

    const ctx: Record<string, unknown> = {};
    if (opts?.modelId) ctx.model_id = opts.modelId;
    if (opts?.promptHashes) ctx.prompts = opts.promptHashes;
    if (opts?.environment) ctx.environment = opts.environment;
    if (this._capturePlans && this._currentPlanId) {
      ctx.plan = { plan_id: this._currentPlanId, version: this._currentPlanVersion || 1 };
    }

    return this._emitEvent(
      eventType,
      pRed ?? {},
      exec,
      ctx,
      privacyFlags,
      redactedFields,
      opts?.policy ?? {},
      opts?.approval ?? null,
      opts?.guardrail ?? {}
    );
  }

  /** Record an overall run outcome (once). */
  recordOutcome(status: string, opts?: { errorType?: string; errorMessage?: string; linkedEventHash?: string }): string | null {
    if (!this._sessionId || this._outcomeEmitted) return null;
    const { redacted: errRed } = redact(opts?.errorMessage ?? null);
    const hash = this.recordAction("agent.outcome", {
      status,
      errorType: opts?.errorType,
      errorPreview: errRed,
      parameters: {},
      outputPreview: null,
    });
    const linked = opts?.linkedEventHash ?? hash ?? undefined;
    this._runResult = { status, error_type: opts?.errorType, error_preview: errRed, linked_event_hash: linked };
    this._outcomeEmitted = true;
    return hash;
  }

  /** End session and write evidence/output files. */
  endSession(finalOutput: unknown, opts?: { modelId?: string | null; latencyMs?: number | null; costEstimate?: number | null }): {
    session_id: string; evidence_path: string; output_path: string;
  } {
    if (!this._sessionId || !this._startedAt) throw new Error("No active session");
    if (!this._outcomeEmitted) this.recordOutcome("success");
    const finishedAt = new Date();
    const durationMs = finishedAt.getTime() - this._startedAt.getTime();
    const { redacted: outRed } = redact(finalOutput);

    const output: OutputSummary = {
      schema_version: SCHEMA_VERSION,
      session_id: this._sessionId,
      agent_name: this._agentName,
      agent_version: this._agentVersion,
      started_at: this._startedAt.toISOString().replace(/\.\d{3}Z$/, "Z"),
      finished_at: finishedAt.toISOString().replace(/\.\d{3}Z$/, "Z"),
      duration_ms: durationMs,
      model_id: opts?.modelId ?? null,
      prompts: this._prompts,
      final_output: outRed,
      run_result: this._runResult,
      tools_used: [],
      reasoning: [],
      consents: [],
      latency_ms: opts?.latencyMs ?? null,
      cost_estimate: opts?.costEstimate ?? null,
      environment_info: this._environmentInfo,
      policy_summary: {},
      guardrail_summary: {},
      actions: this._buildActionSummary(),
      integrity: { first_event_hash: this._firstHash, last_event_hash: this._lastHash, count: this._events.length },
      organization: this.org ? { org_id: this.org.org_id, org_name: this.org.org_name, key_id_prefix: this.org.key_id_prefix } : undefined,
    };

    const evidencePath = join(this.storageDir, `${this._sessionId}-evidence.jsonl`);
    const outputPath = join(this.storageDir, `${this._sessionId}-output.json`);
    // Write evidence JSONL
    const lines = this._events.map((e) => JSON.stringify(e));
    // Use FileSink helpers
    // Evidence: write via append to a new file
    writeFileSync(evidencePath, lines.join("\n") + "\n", { encoding: "utf-8" });
    FileSink.writeJsonAtomic(outputPath, output);

    // Clear
    const sid = this._sessionId;
    this._sessionId = null;
    this._startedAt = null;
    this._events = [];
    this._bytesEstimate = 0;
    this._firstHash = null;
    this._lastHash = null;
    this._prompts = {};
    this._promptHashes = {};
    this._environmentInfo = {};
    this._runResult = {};
    this._outcomeEmitted = false;
    this._currentPlanId = null;
    this._currentPlanVersion = 0;
    this._actionStatuses = new Map();
    this._actionTotal = 0;

    return { session_id: sid, evidence_path: evidencePath, output_path: outputPath };
  }

  // ----------------------- Planning API (gated) -----------------------

  recordPlanCreate(opts: { summary: string; steps: Array<string | unknown>; rationale?: string | null }): string | null {
    if (!this._sessionId) return null;
    // Always track internally; emit only when gated
    const planId = this._currentPlanId || ulid();
    this._currentPlanId = planId;
    this._currentPlanVersion = 1;
    const canon = canonicalPlan({ summary: opts.summary, steps: opts.steps });
    const planHash = sha256(canonicalJson(canon));
    const { redacted: ratRed, privacy_flags: flags, redacted_fields: fields } = redact(opts.rationale ?? null);
    const params: Record<string, unknown> = { plan_id: planId, stage: "proposed", summary: canon.summary, steps: canon.steps, plan_hash: planHash };
    if (ratRed != null) params.rationale_preview = ratRed;
    if (this._capturePlans) {
      this._emitEvent("plan.create", params, { status: "proposed" }, { plan: { plan_id: planId, version: 1 } }, flags, fields);
    }
    return planId;
  }

  recordPlanRefine(opts?: { summary?: string; steps?: Array<string | unknown>; rationale?: string | null }): string | null {
    if (!this._sessionId || !this._currentPlanId) return null;
    this._currentPlanVersion += 1;
    const canon = canonicalPlan({ summary: opts?.summary, steps: opts?.steps });
    const planHash = sha256(canonicalJson(canon));
    const { redacted: ratRed, privacy_flags: flags, redacted_fields: fields } = redact(opts?.rationale ?? null);
    const params: Record<string, unknown> = { plan_id: this._currentPlanId, stage: "refined", version: this._currentPlanVersion, plan_hash: planHash };
    if (canon.summary) params.summary = canon.summary;
    if (canon.steps) params.steps = canon.steps;
    if (ratRed != null) params.rationale_preview = ratRed;
    if (this._capturePlans) {
      this._emitEvent("plan.refine", params, { status: "refined" }, { plan: { plan_id: this._currentPlanId, version: this._currentPlanVersion } }, flags, fields);
    }
    return this._currentPlanId;
  }

  recordPlanAdopt(): string | null {
    if (!this._sessionId || !this._currentPlanId) return null;
    if (this._currentPlanVersion === 0) this._currentPlanVersion = 1;
    if (this._capturePlans) {
      this._emitEvent(
        "plan.adopt",
        { plan_id: this._currentPlanId, stage: "adopted", version: this._currentPlanVersion },
        { status: "adopted" },
        { plan: { plan_id: this._currentPlanId, version: this._currentPlanVersion } }
      );
    }
    return this._currentPlanId;
  }

  recordPlanAbandon(opts?: { reason?: string | null }): string | null {
    if (!this._sessionId || !this._currentPlanId) return null;
    const pid = this._currentPlanId;
    const { redacted: reasonRed, privacy_flags: flags, redacted_fields: fields } = redact(opts?.reason ?? null);
    if (this._capturePlans) {
      const params: Record<string, unknown> = { plan_id: pid, stage: "abandoned", version: this._currentPlanVersion || 1 };
      if (reasonRed != null) params.reason_preview = reasonRed;
      this._emitEvent("plan.abandon", params, { status: "abandoned" }, { plan: { plan_id: pid, version: this._currentPlanVersion || 1 } }, flags, fields);
    }
    // Clear plan context
    this._currentPlanId = null;
    this._currentPlanVersion = 0;
    return pid;
  }

  // ----------------------- internals -----------------------

  private _emitEvent(
    eventType: string,
    parameters: unknown,
    execution?: Record<string, unknown>,
    context?: Record<string, unknown>,
    privacyFlags?: string[],
    redactedFields?: string[],
    policy?: Record<string, unknown> | null,
    approval?: Record<string, unknown> | null,
    guardrail?: Record<string, unknown> | null
  ): string {
    if (!this._sessionId) throw new Error("No active session");
    const idx = this._events.length;
    const evt: EvidenceEvent = {
      schema_version: SCHEMA_VERSION,
      session_id: this._sessionId,
      event_index: idx,
      timestamp: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
      agent_name: this._agentName,
      agent_version: this._agentVersion,
      event_type: eventType,
      parameters_redacted: parameters ?? {},
      privacy_flags: privacyFlags ?? [],
      redacted_fields: redactedFields ?? [],
      context: context ? { ...context } : {},
      execution: execution ? { ...execution } : {},
      policy: policy ?? {},
      approval: approval ?? null,
      guardrail: guardrail ?? {},
      integrity: { prev_hash: this._lastHash, event_hash: "" },
    };
    // stamp organization (optional)
    if (this.org) {
      (evt.context as any).organization = {
        org_id: this.org.org_id,
        ...(this.org.org_name ? { org_name: this.org.org_name } : {}),
        ...(this.org.key_id_prefix ? { key_id_prefix: this.org.key_id_prefix } : {}),
      };
    }
    const hash = computeEventHash(evt as any);
    evt.integrity.event_hash = hash;
    if (this._firstHash == null) this._firstHash = hash;
    this._lastHash = hash;
    this._events.push(evt);
    this._trackActionEvent(eventType, parameters, execution ?? {});
    this._bytesEstimate += Buffer.byteLength(JSON.stringify(evt));
    // Soft compaction
    if (this._bytesEstimate > this._maxBufferBytes && this._events.length > 6) {
      const head = this._events.slice(0, 3);
      const tail = this._events.slice(-3);
      const dropped = this._events.length - 6;
      const summary: EvidenceEvent = {
        schema_version: SCHEMA_VERSION,
        session_id: this._sessionId,
        event_index: head.length,
        timestamp: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
        agent_name: this._agentName,
        agent_version: this._agentVersion,
        event_type: "agent.buffer_compacted",
        parameters_redacted: { dropped_events: dropped },
        privacy_flags: [],
        redacted_fields: [],
        context: {},
        execution: { status: "compacted" },
        policy: {},
        approval: null,
        guardrail: {},
        integrity: { prev_hash: this._lastHash, event_hash: "" },
      };
      summary.integrity.event_hash = computeEventHash(summary as any);
      this._events = [...head, summary, ...tail];
      this._bytesEstimate = Buffer.byteLength(this._events.map((e) => JSON.stringify(e)).join("\n"));
    }
    return hash;
  }

  private _trackActionEvent(eventType: string, parameters: unknown, execution: Record<string, unknown>): void {
    if (!eventType.startsWith("action.")) return;
    const params = parameters && typeof parameters === "object" ? parameters as Record<string, unknown> : {};
    const rawActionId = params.action_id ?? execution.action_id;
    if (!rawActionId) return;
    const actionId = String(rawActionId);
    if (eventType === "action.requested") this._actionTotal += 1;
    const existing = this._actionStatuses.get(actionId) ?? { action_id: actionId };
    const status = String(execution.status ?? params.decision ?? eventType.split(".")[1] ?? "unknown");
    this._actionStatuses.set(actionId, {
      ...existing,
      action: params.action ?? existing.action,
      system: params.system ?? existing.system,
      idempotency_key: params.idempotency_key ?? existing.idempotency_key,
      payload_hash: params.payload_hash ?? execution.payload_hash ?? existing.payload_hash,
      context_hash: params.context_hash ?? execution.context_hash ?? existing.context_hash,
      result_hash: params.result_hash ?? existing.result_hash,
      status,
      event_type: eventType,
    });
  }

  private _buildActionSummary(): { total: number; by_status: Record<string, number>; final_statuses: Array<Record<string, unknown>> } {
    const finalStatuses = Array.from(this._actionStatuses.values());
    const byStatus: Record<string, number> = {};
    for (const item of finalStatuses) {
      const status = String(item.status ?? "unknown");
      byStatus[status] = (byStatus[status] ?? 0) + 1;
    }
    return { total: this._actionTotal, by_status: byStatus, final_statuses: finalStatuses };
  }
}

// Utilities -------------------------------------------------------------

function truthy(val?: string | null): boolean {
  if (!val) return false;
  const s = val.trim().toLowerCase();
  return s === "true" || s === "1" || s === "yes" || s === "on";
}

function ulid(): string {
  // Simple ULID-like unique id (timestamp + random). Good enough for local artifacts.
  const ts = Date.now().toString(36);
  const rnd = Math.random().toString(36).slice(2, 12);
  return `${ts}${rnd}`.padEnd(26, "0").slice(0, 26);
}

function sha256(s: string): string { return crypto.createHash("sha256").update(s, "utf8").digest("hex"); }

function canonicalPlan(input: { summary?: string; steps?: Array<string | unknown> | undefined }): { summary?: string; steps?: string[] } {
  const out: { summary?: string; steps?: string[] } = {};
  if (typeof input.summary === "string" && input.summary.trim()) out.summary = input.summary.trim().slice(0, 256);
  if (input.steps && Array.isArray(input.steps)) {
    out.steps = input.steps.map((s) => String(s ?? "").trim()).filter((s) => s).slice(0, 50);
  }
  return out;
}

/** Convenience alias matching Python's factory naming. */
export async function createCollector(): Promise<SetorraCollector> {
  return SetorraCollector.create();
}
