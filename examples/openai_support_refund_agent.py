"""Live OpenAI Agents SDK demo protected by Setorra.

Run manually:
    python3 -m examples.openai_support_refund_agent

This demo calls OpenAI, but the business action is still local and safe:
the refund tool routes through Setorra's action firewall and the Stripe
connector is a deterministic stub.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from setorra import ActionRequest, StripeRefundConnector, firewall, runner


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "examples" / "policies" / "openai_support_refund_policy.json"
AGENT_ID = "openai-support-refund-agent"
AGENT_VERSION = "1.0.0"
DEFAULT_MODEL = "gpt-4.1-mini"


class SupportRefundRuntime:
    """Reusable support-action runtime used by both the live demo and tests."""

    def __init__(self, *, agent_id: str, firewall_runtime: Any, stripe_connector: StripeRefundConnector) -> None:
        self.agent_id = agent_id
        self.firewall = firewall_runtime
        self.stripe = stripe_connector
        self.last_tool_result: Optional[dict[str, Any]] = None

    def build_refund_request(
        self,
        *,
        ticket_id: str,
        customer_id: str,
        customer_email: str,
        amount: int,
        reason: str,
        customer_tier: str = "enterprise",
    ) -> ActionRequest:
        return ActionRequest(
            agent_id=self.agent_id,
            action="refund.issue",
            system="stripe",
            payload={
                "customer_id": customer_id,
                "customer_email": customer_email,
                "amount": amount,
                "currency": "usd",
                "reason": reason,
            },
            context={
                "ticket_id": ticket_id,
                "customer_tier": customer_tier,
                "reason": reason,
            },
            idempotency_key=f"{ticket_id}:refund",
            compensation={
                "available": True,
                "type": "refund.reverse",
                "reference": "stripe.refunds.cancel",
            },
        )

    def request_refund(self, request: ActionRequest):
        return self.firewall.execute_with_connector(request, self.stripe)

    def approve_and_execute(self, *, request: ActionRequest, pending_result: Any):
        self.firewall.approve(
            action_id=pending_result.action_id,
            intent_hash=pending_result.policy["intent_hash"],
            approver_id="support-manager-demo",
            approver_email="manager@example.com",
            method="local-demo",
            metadata={"scenario": "openai_support_refund_agent"},
        )
        return self.firewall.execute_approved_with_connector(
            request,
            action_id=pending_result.action_id,
            intent_hash=pending_result.policy["intent_hash"],
            connector=self.stripe,
        )

    def request_refund_tool(
        self,
        *,
        ticket_id: str,
        customer_id: str,
        customer_email: str,
        amount: int,
        reason: str,
        customer_tier: str = "enterprise",
    ) -> str:
        request = self.build_refund_request(
            ticket_id=ticket_id,
            customer_id=customer_id,
            customer_email=customer_email,
            amount=amount,
            reason=reason,
            customer_tier=customer_tier,
        )
        initial = self.request_refund(request)
        tool_result: dict[str, Any] = {
            "action_id": initial.action_id,
            "decision": initial.decision,
            "status": initial.status,
            "approval_status": None,
            "connector_executed": len(self.stripe.calls) > 0,
            "refund_external_id": None,
        }

        if initial.decision == "needs_approval":
            executed = self.approve_and_execute(request=request, pending_result=initial)
            tool_result.update(
                {
                    "decision": executed.decision,
                    "status": executed.status,
                    "approval_status": "approved",
                    "connector_executed": True,
                    "refund_external_id": executed.output.get("external_id") if isinstance(executed.output, dict) else None,
                }
            )
        elif initial.decision == "allow" and isinstance(initial.output, dict):
            tool_result.update(
                {
                    "connector_executed": True,
                    "refund_external_id": initial.output.get("external_id"),
                }
            )

        self.last_tool_result = tool_result
        return json.dumps(tool_result, sort_keys=True)


def build_support_ticket_prompt() -> str:
    return (
        "Ticket ticket_456: Enterprise customer cus_123 reports a duplicate charge. "
        "Customer email is customer@example.com. The duplicate amount is 700 USD. "
        "Call request_refund exactly once with reason duplicate_charge, then summarize the result."
    )


def artifact_dir_for_session(session_id: str) -> Path:
    root = Path(os.getenv("SETORRA_LOCAL_DATA_DIR") or "evidence")
    return (Path.cwd() / root / session_id).resolve() if not root.is_absolute() else root / session_id


def load_dotenv_if_present(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def require_openai_setup():
    load_dotenv_if_present()
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or _looks_like_placeholder(api_key):
        raise RuntimeError("OPENAI_API_KEY is missing or still a placeholder. Set it in the environment or .env before running.")
    try:
        from agents import Agent, Runner as OpenAIAgentRunner, function_tool
    except ImportError as exc:
        raise RuntimeError(
            "OpenAI Agents SDK is not installed. Install demo dependencies with: "
            "python3 -m pip install -r examples/requirements-openai-agent.txt"
        ) from exc
    return Agent, OpenAIAgentRunner, function_tool


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(token in lowered for token in ("placeholder", "your_api_key", "sk-...", "replace_me")) or len(value) < 20


async def _run_openai_agent(openai_runner: Any, agent: Any, prompt: str) -> Any:
    result = await openai_runner.run(agent, prompt)
    return getattr(result, "final_output", result)


def run_live_demo() -> dict[str, Any]:
    Agent, OpenAIAgentRunner, function_tool = require_openai_setup()
    model = os.getenv("OPENAI_AGENT_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL

    setorra_run = runner(
        AGENT_ID,
        AGENT_VERSION,
        policy=str(POLICY_PATH),
        environment="local",
        errors_as_output=True,
    )
    firewall_runtime = firewall(setorra_run.collector)
    stripe = StripeRefundConnector(api_key="sk-demo-connector-secret", mode="demo-stub")
    support_runtime = SupportRefundRuntime(
        agent_id=AGENT_ID,
        firewall_runtime=firewall_runtime,
        stripe_connector=stripe,
    )

    @function_tool
    def request_refund(
        ticket_id: str,
        customer_id: str,
        customer_email: str,
        amount: int,
        reason: str,
        customer_tier: str = "enterprise",
    ) -> str:
        return support_runtime.request_refund_tool(
            ticket_id=ticket_id,
            customer_id=customer_id,
            customer_email=customer_email,
            amount=amount,
            reason=reason,
            customer_tier=customer_tier,
        )

    agent = Agent(
        name="Setorra Support Refund Agent",
        model=model,
        instructions=(
            "You are a support refund agent. For refund requests, you must call "
            "request_refund before answering. Do not claim a refund was executed "
            "unless the tool result says status=succeeded."
        ),
        tools=[request_refund],
    )

    prompt = build_support_ticket_prompt()
    result = setorra_run.invoke(
        lambda user_prompt: asyncio.run(_run_openai_agent(OpenAIAgentRunner, agent, user_prompt)),
        inputs=prompt,
        model_id=model,
        system_prompt="OpenAI Agents SDK support-refund demo protected by Setorra.",
        user_prompt=prompt,
        meta={"scenario": "openai_support_refund_agent"},
    )

    artifact_dir = artifact_dir_for_session(result.session_id)
    summary = {
        "session_id": result.session_id,
        "decision": (support_runtime.last_tool_result or {}).get("decision"),
        "approval_status": (support_runtime.last_tool_result or {}).get("approval_status"),
        "connector_executed": (support_runtime.last_tool_result or {}).get("connector_executed", False),
        "refund_external_id": (support_runtime.last_tool_result or {}).get("refund_external_id"),
        "artifact_dir": str(artifact_dir),
        "evidence_path": str(artifact_dir / "evidence.jsonl"),
        "output_path": str(artifact_dir / "output.json"),
        "manifest_path": str(artifact_dir / "manifest.json"),
    }
    return summary


def main() -> None:
    try:
        summary = run_live_demo()
    except RuntimeError as exc:
        print(f"Setup error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    print("Setorra OpenAI support refund demo complete")
    for key, value in summary.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
