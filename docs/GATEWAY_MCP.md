# Gateway and MCP Foundation

The SDK firewall is the developer integration point. The gateway is the later credential boundary: agents call protected Setorra tools, and Setorra-owned connectors call business systems.

## MVP Boundary

- Agents submit `ActionRequest` payloads.
- Gateway runs the same firewall contract: `action.requested` → guardrails → `policy.decision` → approval if needed → connector execution only when allowed/approved.
- Raw production credentials belong to the gateway/connector process, not the agent.
- Evidence records action IDs, payload/context hashes, policy decisions, approval records, connector result IDs, and compensation metadata. It must not record raw connector secrets.

## MCP Shape

Protected tools should map 1:1 to action names:

- `refund.issue`
- `credit.apply`
- `subscription.cancel`
- `account.update`
- `email.send`
- `ticket.escalate`

Each tool receives an `ActionRequest` and returns an `ActionResult`. The gateway may expose this over REST first and MCP later without changing the SDK evidence contract.

## Local Entrypoint

`tools/local_action_gateway.py` is a no-network local harness. It reads one JSON action request from stdin or a file, runs the firewall, and executes through a fake connector only when the decision is `allow`.
