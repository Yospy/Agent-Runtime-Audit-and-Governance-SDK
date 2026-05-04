/**
 * Public API surface for the Setorra Node SDK (setorra-ai).
 *
 * Focus: minimal, production-minded surface that matches Python SDK semantics.
 */
export type { OrganizationInfo, EventContext, SetorraEvent } from "./core/events.js";
export { createCollector, SetorraCollector } from "./core/collector.js";
export {
  ActionFirewall,
  firewall,
  type ActionDecision,
  type ActionDecisionStatus,
  type ActionFirewallOptions,
  type ActionPolicyEvaluator,
  type ActionRequest,
  type ActionResult,
} from "./core/firewall.js";
