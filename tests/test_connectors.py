import json
from pathlib import Path

from setorra import (
    ActionRequest,
    FakeConnector,
    PolicyDecisionPoint,
    PolicyLoader,
    SetorraCollector,
    StripeRefundConnector,
    firewall,
)


def _policy_file(tmp_path: Path) -> Path:
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(
            {
                "version": "connectors-v1",
                "defaults": {"decision": "allow", "fail_mode": "fail-closed"},
                "actions": {
                    "refund.issue": {
                        "default_decision": "allow",
                        "rules": [
                            {
                                "id": "deny-large",
                                "decision": "deny",
                                "match": {"parameters.payload.amount": {"gt": 5000}},
                            },
                            {
                                "id": "approve-medium",
                                "decision": "require_approval",
                                "match": {"parameters.payload.amount": {"gt": 500}},
                                "approval": {"expires_in": 900, "requires_token": True},
                            },
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _collector(tmp_path: Path, policy_path: Path | None = None) -> SetorraCollector:
    decider = PolicyDecisionPoint(PolicyLoader(policy_path)) if policy_path else None
    return SetorraCollector(
        agent_name="support-agent",
        agent_version="1.0.0",
        storage_dir=tmp_path / "evidence",
        policy_decider=decider,
    )


def test_denied_action_never_calls_connector(tmp_path: Path):
    collector = _collector(tmp_path, _policy_file(tmp_path))
    collector.start_session()
    connector = FakeConnector()

    result = firewall(collector).execute_with_connector(
        {
            "agent_id": "support-agent",
            "action": "refund.issue",
            "system": "stripe",
            "payload": {"amount": 6000},
            "context": {"ticket_id": "ticket_123"},
        },
        connector,
    )
    collector.end_session(final_output={"decision": result.decision})

    assert result.decision == "deny"
    assert connector.calls == []


def test_allowed_action_calls_connector_once(tmp_path: Path):
    collector = _collector(tmp_path)
    collector.start_session()
    connector = FakeConnector()

    result = firewall(collector).execute_with_connector(
        ActionRequest(
            agent_id="support-agent",
            action="refund.issue",
            system="stripe",
            payload={"amount": 100},
            context={"ticket_id": "ticket_123"},
        ),
        connector,
    )
    collector.end_session(final_output={"decision": result.decision})

    assert result.status == "succeeded"
    assert len(connector.calls) == 1


def test_approved_action_calls_connector_once(tmp_path: Path):
    collector = _collector(tmp_path, _policy_file(tmp_path))
    collector.start_session()
    connector = FakeConnector()
    fw = firewall(collector)
    request = ActionRequest(
        agent_id="support-agent",
        action="refund.issue",
        system="stripe",
        payload={"amount": 700},
        context={"ticket_id": "ticket_123"},
    )

    pending = fw.execute(request)
    fw.approve(action_id=pending.action_id, intent_hash=pending.policy["intent_hash"], approver_id="manager-1")
    result = fw.execute_approved_with_connector(
        request,
        action_id=pending.action_id,
        intent_hash=pending.policy["intent_hash"],
        connector=connector,
    )
    collector.end_session(final_output={"decision": result.decision})

    assert result.status == "succeeded"
    assert len(connector.calls) == 1


def test_connector_credentials_are_not_written_to_evidence(tmp_path: Path):
    collector = _collector(tmp_path)
    collector.start_session()
    connector = StripeRefundConnector(api_key="sk-test-secret-value")

    result = firewall(collector).execute_with_connector(
        ActionRequest(
            agent_id="support-agent",
            action="refund.issue",
            system="stripe",
            payload={"amount": 100},
            context={"ticket_id": "ticket_123"},
            idempotency_key="ticket_123:refund",
        ),
        connector,
    )
    artifacts = collector.end_session(final_output={"decision": result.decision})
    evidence_text = Path(artifacts["evidence_path"]).read_text(encoding="utf-8")

    assert result.status == "succeeded"
    assert len(connector.calls) == 1
    assert connector.calls[0].idempotency_key == "ticket_123:refund"
    assert "sk-test-secret-value" not in evidence_text
