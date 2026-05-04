import json
from pathlib import Path

import pytest

from setorra import (
    ActionRequest,
    ApprovalRuntimeError,
    PolicyDecisionPoint,
    PolicyLoader,
    SetorraCollector,
    firewall,
)
from setorra.action_lifecycle import verify_action_lifecycle


def _load_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _load_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _collector(tmp_path: Path, policy_path: Path | None = None) -> SetorraCollector:
    decider = PolicyDecisionPoint(PolicyLoader(policy_path)) if policy_path else None
    return SetorraCollector(
        agent_name="support-agent",
        agent_version="1.0.0",
        storage_dir=tmp_path / "evidence",
        policy_decider=decider,
    )


def _policy_file(tmp_path: Path) -> Path:
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(
            {
                "version": "support-v1",
                "defaults": {"decision": "allow", "fail_mode": "fail-closed"},
                "actions": {
                    "refund.issue": {
                        "default_decision": "allow",
                        "rules": [
                            {
                                "id": "refund-needs-ticket",
                                "decision": "needs_more_context",
                                "match": {"context.ticket_id": {"exists": False}},
                                "rationale": "ticket context is required",
                            },
                            {
                                "id": "refund-deny-large",
                                "decision": "deny",
                                "match": {"parameters.payload.amount": {"gt": 5000}},
                            },
                            {
                                "id": "refund-approval-medium",
                                "decision": "require_approval",
                                "match": {"parameters.payload.amount": {"gt": 500}},
                                "approval": {
                                    "expires_in": 900,
                                    "requires_token": True,
                                    "approver_roles": ["support_manager"],
                                },
                            },
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_firewall_allows_action_and_records_policy_decision_without_policy(tmp_path: Path):
    collector = _collector(tmp_path)
    collector.start_session()

    result = firewall(collector).execute(
        ActionRequest(
            agent_id="support-agent",
            action="refund.issue",
            system="stripe",
            payload={"amount": 1200, "customer": "alice@example.com"},
            context={"ticket_id": "ticket_123"},
            idempotency_key="ticket_123:refund",
        )
    )
    artifacts = collector.end_session(final_output={"decision": result.decision})

    assert result.decision == "allow"
    assert result.allowed is True
    assert result.action_id.startswith("act_")

    events = _load_jsonl(Path(artifacts["evidence_path"]))
    assert "action.requested" in [event["event_type"] for event in events]
    assert "policy.decision" in [event["event_type"] for event in events]
    assert "action.allowed" in [event["event_type"] for event in events]


def test_firewall_maps_policy_deny_to_blocked_result(tmp_path: Path):
    collector = _collector(tmp_path, _policy_file(tmp_path))
    collector.start_session()

    result = firewall(collector).execute(
        {
            "agentId": "support-agent",
            "action": "refund.issue",
            "system": "stripe",
            "payload": {"amount": 6000},
            "context": {"ticket_id": "ticket_123"},
        }
    )
    collector.end_session(final_output={"decision": result.decision})

    assert result.decision == "deny"
    assert result.status == "blocked"
    assert result.allowed is False
    assert result.policy["rule_id"] == "refund-deny-large"


def test_firewall_maps_approval_to_needs_approval_without_waiting(tmp_path: Path):
    collector = _collector(tmp_path, _policy_file(tmp_path))
    collector.start_session()

    result = firewall(collector).execute(
        ActionRequest(
            agent_id="support-agent",
            action="refund.issue",
            system="stripe",
            payload={"amount": 700},
            context={"ticket_id": "ticket_123"},
        )
    )
    collector.end_session(final_output={"decision": result.decision})

    assert result.decision == "needs_approval"
    assert result.status == "pending_approval"
    assert result.approval is not None
    assert result.approval["status"] == "pending"


def test_firewall_maps_policy_needs_more_context(tmp_path: Path):
    collector = _collector(tmp_path, _policy_file(tmp_path))
    collector.start_session()

    result = firewall(collector).execute(
        ActionRequest(
            agent_id="support-agent",
            action="refund.issue",
            system="stripe",
            payload={"amount": 100},
            context={},
        )
    )
    collector.end_session(final_output={"decision": result.decision})

    assert result.decision == "needs_more_context"
    assert result.status == "needs_more_context"
    assert result.allowed is False
    assert result.policy["rule_id"] == "refund-needs-ticket"


def test_firewall_executes_allowed_action_and_records_lifecycle(tmp_path: Path):
    collector = _collector(tmp_path)
    collector.start_session()

    result = firewall(collector).execute(
        ActionRequest(
            agent_id="support-agent",
            action="refund.issue",
            system="stripe",
            payload={"amount": 100},
            context={"ticket_id": "ticket_123"},
            idempotency_key="ticket_123:refund",
            compensation={
                "available": True,
                "type": "refund.reverse",
                "reference": "stripe.refunds.cancel",
            },
        ),
        executor=lambda request: {"refund_id": "re_123", "amount": request.payload["amount"]},
    )
    artifacts = collector.end_session(final_output={"decision": result.decision})

    output = _load_json(Path(artifacts["output_path"]))
    events = _load_jsonl(Path(artifacts["evidence_path"]))
    event_types = [event["event_type"] for event in events]
    report = verify_action_lifecycle(events)

    assert result.status == "succeeded"
    assert result.result_hash is not None
    assert "action.executed" in event_types
    assert "action.succeeded" in event_types
    assert output["actions"]["total"] == 1
    assert output["actions"]["by_status"]["succeeded"] == 1
    assert output["actions"]["final_statuses"][0]["result_hash"] == result.result_hash
    assert report.ok is True
    assert report.actions_checked == 1


def test_firewall_approval_runtime_binds_identity_and_rejects_tampered_action(tmp_path: Path):
    collector = _collector(tmp_path, _policy_file(tmp_path))
    collector.start_session()
    fw = firewall(collector)

    request = ActionRequest(
        agent_id="support-agent",
        action="refund.issue",
        system="stripe",
        payload={"amount": 700},
        context={"ticket_id": "ticket_123"},
    )
    pending = fw.execute(request)

    assert pending.decision == "needs_approval"
    intent_hash = pending.policy["intent_hash"]
    fw.approve(
        action_id=pending.action_id,
        intent_hash=intent_hash,
        approver_id="manager-1",
        approver_email="manager@example.com",
    )

    tampered = ActionRequest(
        agent_id="support-agent",
        action="refund.issue",
        system="stripe",
        payload={"amount": 701},
        context={"ticket_id": "ticket_123"},
    )
    with pytest.raises(ApprovalRuntimeError):
        fw.execute_approved(
            tampered,
            action_id=pending.action_id,
            intent_hash=intent_hash,
            executor=lambda _: {"refund_id": "bad"},
        )

    executed = fw.execute_approved(
        request,
        action_id=pending.action_id,
        intent_hash=intent_hash,
        executor=lambda _: {"refund_id": "re_approved"},
    )
    artifacts = collector.end_session(final_output={"decision": executed.decision})
    events = _load_jsonl(Path(artifacts["evidence_path"]))
    event_types = [event["event_type"] for event in events]

    assert executed.status == "succeeded"
    assert "approval.requested" in event_types
    assert "approval.resolved" in event_types
    resolved = [event for event in events if event["event_type"] == "approval.resolved"][0]
    assert resolved["approval"]["approver_id"] == "manager-1"
    assert resolved["approval"]["approver_email"] == "manager@example.com"
