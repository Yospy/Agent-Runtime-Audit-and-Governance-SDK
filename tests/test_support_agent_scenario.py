import json
from pathlib import Path

from setorra import (
    ActionRequest,
    PolicyDecisionPoint,
    PolicyLoader,
    SetorraCollector,
    StripeRefundConnector,
    firewall,
)
from setorra.action_lifecycle import verify_action_lifecycle


class SupportRefundAgent:
    def __init__(self, agent_id: str, firewall_runtime, stripe_connector: StripeRefundConnector) -> None:
        self.agent_id = agent_id
        self.firewall = firewall_runtime
        self.stripe = stripe_connector

    def request_duplicate_charge_refund(self, ticket: dict):
        request = ActionRequest(
            agent_id=self.agent_id,
            action="refund.issue",
            system="stripe",
            payload={
                "customer_id": ticket["customer_id"],
                "customer_email": ticket["customer_email"],
                "amount": ticket["refund_amount"],
                "currency": "usd",
                "reason": "duplicate_charge",
            },
            context={
                "ticket_id": ticket["ticket_id"],
                "customer_tier": ticket["customer_tier"],
                "reason": ticket["reason"],
            },
            idempotency_key=f"{ticket['ticket_id']}:refund",
            compensation={
                "available": True,
                "type": "refund.reverse",
                "reference": "stripe.refunds.cancel",
            },
        )
        return self.firewall.execute(request)

    def execute_approved_refund(self, pending_result, ticket: dict):
        request = ActionRequest(
            agent_id=self.agent_id,
            action="refund.issue",
            system="stripe",
            payload={
                "customer_id": ticket["customer_id"],
                "customer_email": ticket["customer_email"],
                "amount": ticket["refund_amount"],
                "currency": "usd",
                "reason": "duplicate_charge",
            },
            context={
                "ticket_id": ticket["ticket_id"],
                "customer_tier": ticket["customer_tier"],
                "reason": ticket["reason"],
            },
            idempotency_key=f"{ticket['ticket_id']}:refund",
            compensation={
                "available": True,
                "type": "refund.reverse",
                "reference": "stripe.refunds.cancel",
            },
        )
        return self.firewall.execute_approved_with_connector(
            request,
            action_id=pending_result.action_id,
            intent_hash=pending_result.policy["intent_hash"],
            connector=self.stripe,
        )


def _load_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _load_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _support_refund_policy(tmp_path: Path) -> Path:
    path = tmp_path / "support-refund-policy.json"
    path.write_text(
        json.dumps(
            {
                "version": "support-refund-v1",
                "defaults": {"decision": "allow", "fail_mode": "fail-closed"},
                "actions": {
                    "refund.issue": {
                        "default_decision": "allow",
                        "rules": [
                            {
                                "id": "refund-over-500-manager-approval",
                                "decision": "require_approval",
                                "match": {"parameters.payload.amount": {"gt": 500}},
                                "approval": {
                                    "expires_in": 900,
                                    "requires_token": True,
                                    "approver_roles": ["support_manager"],
                                },
                            }
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_support_agent_duplicate_charge_refund_requires_approval_then_executes(tmp_path: Path):
    policy_path = _support_refund_policy(tmp_path)
    collector = SetorraCollector(
        agent_name="support-agent",
        agent_version="1.0.0",
        storage_dir=tmp_path / "evidence",
        policy_decider=PolicyDecisionPoint(PolicyLoader(policy_path)),
    )
    collector.start_session(
        system_prompt="You are a support refunds agent.",
        user_prompt="Customer reports duplicate charge and asks for refund.",
    )

    stripe = StripeRefundConnector(api_key="sk-test-secret-value")
    fw = firewall(collector)
    agent = SupportRefundAgent("support-agent", fw, stripe)
    ticket = {
        "ticket_id": "ticket_456",
        "customer_id": "cus_123",
        "customer_email": "customer@example.com",
        "refund_amount": 700,
        "customer_tier": "enterprise",
        "reason": "duplicate charge",
    }

    pending = agent.request_duplicate_charge_refund(ticket)

    assert pending.decision == "needs_approval"
    assert pending.status == "pending_approval"
    assert len(stripe.calls) == 0

    fw.approve(
        action_id=pending.action_id,
        intent_hash=pending.policy["intent_hash"],
        approver_id="support-manager-1",
        approver_email="manager@example.com",
    )
    executed = agent.execute_approved_refund(pending, ticket)
    artifacts = collector.end_session(final_output={"status": executed.status, "refund": executed.output})

    evidence_path = Path(artifacts["evidence_path"])
    output_path = Path(artifacts["output_path"])
    events = _load_jsonl(evidence_path)
    output = _load_json(output_path)
    event_types = [event["event_type"] for event in events]
    lifecycle = verify_action_lifecycle(events)
    evidence_text = evidence_path.read_text(encoding="utf-8")

    assert executed.status == "succeeded"
    assert executed.output["external_id"].startswith("re_")
    assert len(stripe.calls) == 1
    assert stripe.calls[0].idempotency_key == "ticket_456:refund"
    assert lifecycle.ok is True
    assert output["actions"]["total"] == 1
    assert output["actions"]["by_status"]["succeeded"] == 1
    assert "action.requested" in event_types
    assert "policy.decision" in event_types
    assert "approval.requested" in event_types
    assert "approval.resolved" in event_types
    assert "action.executed" in event_types
    assert "action.succeeded" in event_types
    assert "customer@example.com" not in evidence_text
    assert "sk-test-secret-value" not in evidence_text
