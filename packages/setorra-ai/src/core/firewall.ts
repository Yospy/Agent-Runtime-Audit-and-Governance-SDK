import crypto from "node:crypto";
import { canonicalJson } from "./integrity.js";
import type { SetorraCollector } from "./collector.js";

export type ActionDecisionStatus = "allow" | "deny" | "needs_approval" | "needs_more_context";

export interface ActionRequest {
  agent_id: string;
  action: string;
  system: string;
  payload: unknown;
  context?: Record<string, unknown>;
  idempotency_key?: string | null;
  compensation?: Record<string, unknown> | null;
}

export interface ActionDecision {
  decision: ActionDecisionStatus;
  policy_version?: string;
  intent_hash?: string;
  rule_id?: string;
  rationale?: string;
  reason_code?: string;
  approval?: Record<string, unknown> | null;
}

export interface ActionResult {
  action_id: string;
  decision: ActionDecisionStatus;
  status: "allowed" | "blocked" | "pending_approval" | "needs_more_context" | "succeeded";
  allowed: boolean;
  payload_hash: string;
  context_hash: string;
  policy: ActionDecision;
  approval?: Record<string, unknown> | null;
  missing_context?: string[];
  result_hash?: string | null;
  output?: unknown;
}

export type ActionPolicyEvaluator = (request: ActionRequest) => ActionDecision | ActionDecisionStatus;

export interface ActionFirewallOptions {
  evaluate?: ActionPolicyEvaluator;
}

export class ActionFirewall {
  constructor(private readonly collector: SetorraCollector, private readonly opts: ActionFirewallOptions = {}) {}

  execute(input: ActionRequest, executor?: (request: ActionRequest) => unknown): ActionResult {
    const request = normalizeRequest(input);
    const actionId = deriveActionId(request);
    const payloadHash = sha256Json(request.payload ?? {});
    const contextHash = sha256Json(request.context ?? {});
    const missing = missingRequiredFields(request);

    const requestedHash = this.collector.recordAction("action.requested", {
      parameters: {
        action_id: actionId,
        agent_id: request.agent_id,
        action: request.action,
        system: request.system,
        idempotency_key: request.idempotency_key ?? null,
        payload: request.payload ?? {},
        context: request.context ?? {},
        payload_hash: payloadHash,
        context_hash: contextHash,
        compensation: compensationPayload(request.compensation),
      },
      status: "requested",
    });
    if (requestedHash == null) throw new Error("No active session. Call startSession() before firewall.execute().");

    if (missing.length > 0) {
      const policy = {
        decision: "needs_more_context" as const,
        reason_code: "MISSING_REQUIRED_FIELDS",
        rationale: `Missing required fields: ${missing.join(", ")}`,
      };
      this.recordTerminal(request, actionId, policy, "needs_more_context", payloadHash, contextHash, missing);
      return {
        action_id: actionId,
        decision: policy.decision,
        status: "needs_more_context",
        allowed: false,
        payload_hash: payloadHash,
        context_hash: contextHash,
        policy: { ...policy },
        missing_context: missing,
      };
    }

    const evaluated = this.opts.evaluate ? this.opts.evaluate(request) : "allow";
    const policy = normalizeDecision(evaluated);
    this.collector.recordAction("policy.decision", {
      parameters: {
        action: request.action,
        stage: "evaluated",
        status: policy.decision,
        rationale: policy.rationale,
      },
      status: policy.decision,
      policy: {
        version: policy.policy_version ?? "unconfigured",
        decision: policy.decision,
        intent_hash: policy.intent_hash ?? sha256Json({ request, policy_version: policy.policy_version ?? "unconfigured" }),
        rule_id: policy.rule_id,
        reason_code: policy.reason_code,
      },
      approval: policy.approval ?? null,
    });

    const status = terminalStatus(policy.decision);
    this.recordTerminal(request, actionId, policy, status, payloadHash, contextHash);
    if (policy.decision === "allow" && executor) {
      this.collector.recordAction("action.executed", {
        parameters: {
          action_id: actionId,
          action: request.action,
          system: request.system,
          idempotency_key: request.idempotency_key ?? null,
          payload_hash: payloadHash,
          context_hash: contextHash,
          compensation: compensationPayload(request.compensation),
        },
        status: "executed",
        inputHash: payloadHash,
        outputHash: contextHash,
        policy: { ...policy },
      });
      try {
        const output = executor(request);
        const resultHash = sha256Json(output ?? {});
        this.collector.recordAction("action.succeeded", {
          parameters: {
            action_id: actionId,
            action: request.action,
            system: request.system,
            idempotency_key: request.idempotency_key ?? null,
            payload_hash: payloadHash,
            context_hash: contextHash,
            result_hash: resultHash,
            compensation: compensationPayload(request.compensation),
          },
          status: "succeeded",
          outputPreview: output,
          outputHash: resultHash,
          policy: { ...policy },
        });
        return {
          action_id: actionId,
          decision: policy.decision,
          status: "succeeded",
          allowed: true,
          payload_hash: payloadHash,
          context_hash: contextHash,
          result_hash: resultHash,
          policy: { ...policy },
          approval: policy.approval ?? null,
          output,
        };
      } catch (err) {
        this.collector.recordAction("action.failed", {
          parameters: {
            action_id: actionId,
            action: request.action,
            system: request.system,
            idempotency_key: request.idempotency_key ?? null,
            payload_hash: payloadHash,
            context_hash: contextHash,
            compensation: compensationPayload(request.compensation),
          },
          status: "failed",
          errorType: err instanceof Error ? err.name : "Error",
          errorPreview: err instanceof Error ? err.message : String(err),
          policy: { ...policy },
        });
        throw err;
      }
    }
    return {
      action_id: actionId,
      decision: policy.decision,
      status,
      allowed: policy.decision === "allow",
      payload_hash: payloadHash,
      context_hash: contextHash,
      policy,
      approval: policy.approval ?? null,
    };
  }

  private recordTerminal(
    request: ActionRequest,
    actionId: string,
    decision: ActionDecision,
    status: ActionResult["status"],
    payloadHash: string,
    contextHash: string,
    missing?: string[]
  ): void {
    this.collector.recordAction(terminalEventType(decision.decision), {
      parameters: {
        action_id: actionId,
        action: request.action,
        system: request.system,
        decision: decision.decision,
        missing_context: missing ?? [],
      },
      status,
      policy: {
        version: decision.policy_version ?? "unconfigured",
        decision: decision.decision,
        intent_hash: decision.intent_hash ?? sha256Json({ request, policy_version: decision.policy_version ?? "unconfigured" }),
        rule_id: decision.rule_id,
        reason_code: decision.reason_code,
      },
      approval: decision.approval ?? null,
      inputHash: payloadHash,
      outputHash: contextHash,
    });
  }
}

export function firewall(collector: SetorraCollector, opts?: ActionFirewallOptions): ActionFirewall {
  return new ActionFirewall(collector, opts);
}

function normalizeRequest(input: ActionRequest): ActionRequest {
  return {
    agent_id: String((input as any).agentId ?? input.agent_id ?? ""),
    action: String(input.action ?? ""),
    system: String(input.system ?? ""),
    payload: input.payload ?? {},
    context: input.context ?? {},
    idempotency_key: (input as any).idempotencyKey ?? input.idempotency_key ?? null,
    compensation: input.compensation ?? null,
  };
}

function missingRequiredFields(request: ActionRequest): string[] {
  const missing: string[] = [];
  if (!request.agent_id) missing.push("agent_id");
  if (!request.action) missing.push("action");
  if (!request.system) missing.push("system");
  return missing;
}

function normalizeDecision(value: ActionDecision | ActionDecisionStatus): ActionDecision {
  if (typeof value === "string") return { decision: value };
  return value;
}

function terminalStatus(decision: ActionDecisionStatus): ActionResult["status"] {
  if (decision === "allow") return "allowed";
  if (decision === "needs_approval") return "pending_approval";
  if (decision === "needs_more_context") return "needs_more_context";
  return "blocked";
}

function terminalEventType(decision: ActionDecisionStatus): string {
  if (decision === "allow") return "action.allowed";
  if (decision === "needs_approval") return "action.needs_approval";
  if (decision === "needs_more_context") return "action.needs_more_context";
  return "action.blocked";
}

function deriveActionId(request: ActionRequest): string {
  if (request.idempotency_key) {
    return `act_${sha256Json({
      agent_id: request.agent_id,
      action: request.action,
      system: request.system,
      idempotency_key: request.idempotency_key,
    }).slice(0, 32)}`;
  }
  return `act_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`;
}

function sha256Json(value: unknown): string {
  return crypto.createHash("sha256").update(canonicalJson(value), "utf8").digest("hex");
}

function compensationPayload(value?: Record<string, unknown> | null): Record<string, unknown> {
  if (!value) return { available: false };
  return {
    available: value.available ?? true,
    type: value.type ?? null,
    reference: value.reference ?? null,
  };
}
