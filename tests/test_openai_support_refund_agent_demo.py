import json
from pathlib import Path

from examples.openai_support_refund_agent import AGENT_ID, POLICY_PATH, SupportRefundRuntime
from setorra import PolicyDecisionPoint, PolicyLoader, SetorraCollector, StripeRefundConnector, firewall
from setorra.action_lifecycle import verify_action_lifecycle


def _load_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _load_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_openai_support_demo_runtime_requires_approval_then_writes_evidence(tmp_path: Path):
    collector = SetorraCollector(
        agent_name=AGENT_ID,
        agent_version="1.0.0",
        storage_dir=tmp_path / "evidence",
        policy_decider=PolicyDecisionPoint(PolicyLoader(POLICY_PATH)),
    )
    collector.start_session(
        system_prompt="OpenAI Agents SDK support-refund demo protected by Setorra.",
        user_prompt="Customer reports duplicate charge and asks for a 700 USD refund.",
    )
    stripe = StripeRefundConnector(api_key="sk-demo-connector-secret", mode="demo-stub")
    runtime = SupportRefundRuntime(
        agent_id=AGENT_ID,
        firewall_runtime=firewall(collector),
        stripe_connector=stripe,
    )
    request = runtime.build_refund_request(
        ticket_id="ticket_456",
        customer_id="cus_123",
        customer_email="customer@example.com",
        amount=700,
        reason="duplicate_charge",
        customer_tier="enterprise",
    )

    pending = runtime.request_refund(request)

    assert pending.decision == "needs_approval"
    assert pending.status == "pending_approval"
    assert len(stripe.calls) == 0

    executed = runtime.approve_and_execute(request=request, pending_result=pending)
    artifacts = collector.end_session(final_output={"status": executed.status, "refund": executed.output})

    assert executed.status == "succeeded"
    assert len(stripe.calls) == 1

    artifact_dir = Path(artifacts["artifact_dir"])
    evidence_path = artifact_dir / "evidence.jsonl"
    output_path = artifact_dir / "output.json"
    manifest_path = artifact_dir / "manifest.json"

    assert evidence_path.exists()
    assert output_path.exists()
    assert manifest_path.exists()

    events = _load_jsonl(evidence_path)
    output = _load_json(output_path)
    lifecycle = verify_action_lifecycle(events)
    event_types = [event["event_type"] for event in events]
    evidence_text = evidence_path.read_text(encoding="utf-8")

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
    assert "sk-demo-connector-secret" not in evidence_text

