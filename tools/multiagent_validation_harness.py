#!/usr/bin/env python3
"""
Synthetic, single-file harness that drives multiple scenarios to exercise
Setorra's evidence/output/manifest schemas — including multi-agent messaging,
guardrails, policy approvals/denials, tool auth failures, and redaction.

All data is generated in-process (no external services). Backend posting is
stubbed so runs stay offline; artifacts land under data/<session>/.
"""

from __future__ import annotations

import os
import random
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Callable, Dict, List

# Ensure repository root is on sys.path when invoked from tools/
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import setorra.collector as collector_mod
from setorra.backend_ingest import PostResult, deliver_artifacts
from setorra.collector import SetorraCollector, setorra_collector


def _ts(offset_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).replace(microsecond=0).isoformat()


def _maybe_install_noop_backend() -> None:
    """Optionally avoid real HTTP ingest; set SETORRA_HARNESS_NOOP=1 to enable."""

    if os.getenv("SETORRA_HARNESS_NOOP", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return

    def _noop_deliver(
        *,
        base_url: str,
        session_token: str,
        org_id: str | None,
        agent_label: str,
        session_id: str,
        evidence_bytes: bytes,
        output_bytes: bytes,
        manifest_bytes: bytes,
        timeout_s: float = 2.5,
    ) -> Dict[str, PostResult]:
        print("[harness] NOOP backend enabled; skipping HTTP ingest")
        return {
            "evidence": PostResult(part="evidence", status=0, ms=0.0, bytes=len(evidence_bytes)),
            "output": PostResult(part="output", status=0, ms=0.0, bytes=len(output_bytes)),
            "manifest": PostResult(part="manifest", status=0, ms=0.0, bytes=len(manifest_bytes)),
        }

    collector_mod.deliver_artifacts = _noop_deliver  # type: ignore[attr-defined]


def _common_env(scenario: str) -> Dict[str, str]:
    return {
        "env": "harness",
        "scenario": scenario,
        "hostname": socket.gethostname(),
    }


def simulate_finance(collector: SetorraCollector, scenario: str) -> None:
    collector.start_conversation(
        participants=[
            {"agent_id": "orchestrator", "role": "planner"},
            {"agent_id": "finance-analyst", "role": "executor"},
            {"agent_id": "risk-approver", "role": "approver"},
        ],
        edges=[
            {"from": "orchestrator", "to": "finance-analyst"},
            {"from": "finance-analyst", "to": "risk-approver"},
        ],
    )
    collector.add_participant(agent_id="audit-bot", role="observer")
    collector.send_message(
        to=["finance-analyst"],
        content={"task": "Transfer 15000 USD to vendor-9921", "urgency": "high"},
    )

    collector.record_action(
        "guardrail.modified",
        parameters={"name": "payment.transfer", "amount": 15000, "currency": "USD", "target": "vendor-9921"},
        execution={"status": "modified", "message": "clamped to policy threshold"},
        guardrail={
            "version": "finance-guardrails-1",
            "bundle_hash": "guard-finance",
            "status": "modified",
            "action": "clamp",
            "applied_rules": ["limit.transfer.amount"],
        },
    )

    approval = {
        "approver_id": "risk-approver",
        "approver_email": "risk@example.com",
        "approved_at": _ts(),
        "signature": "sig-risk-approval",
    }
    policy_pending = {
        "version": "finance-policy-1",
        "decision": "require_approval",
        "bundle_hash": "pol-finance",
        "rule_id": "transfer.over.threshold",
        "intent_hash": "intent-finance-001",
        "fallback_mode": "require_approval",
    }
    collector.record_action(
        "policy.require_approval",
        parameters={"action": "payment.transfer", "amount": 12500, "currency": "USD"},
        execution={"status": "pending_approval"},
        approval=approval,
        policy=policy_pending,
    )

    collector.record_action(
        "tool.start",
        parameters={"name": "payment.transfer", "amount": 12500, "currency": "USD", "to_account": "ACCT-8842"},
        execution={"status": "started"},
        tool_name="payment.transfer",
        input_hash="h-input-finance",
    )

    policy_allow = dict(policy_pending)
    policy_allow["decision"] = "allow"
    collector.record_action(
        "policy.allow",
        parameters={"action": "payment.transfer", "amount": 10000, "currency": "USD"},
        execution={"status": "allow"},
        approval=approval,
        policy=policy_allow,
    )

    collector.record_action(
        "tool.end",
        parameters={
            "name": "payment.transfer",
            "amount": 10000,
            "currency": "USD",
            "to_account": "ACCT-8842",
            "memo": "quarterly retainer",
        },
        execution={"status": "success", "latency_ms": 380, "cost": 0.19},
        tool_name="payment.transfer",
        input_hash="h-input-finance",
        output_hash="h-output-finance",
    )


def simulate_healthcare(collector: SetorraCollector, scenario: str) -> None:
    collector.start_conversation(
        participants=[
            {"agent_id": "orchestrator", "role": "planner"},
            {"agent_id": "nurse-bot", "role": "triage"},
            {"agent_id": "privacy-bot", "role": "privacy"},
        ],
        edges=[
            {"from": "orchestrator", "to": "nurse-bot"},
            {"from": "nurse-bot", "to": "privacy-bot"},
        ],
    )
    collector.send_message(
        to=["nurse-bot"],
        content={
            "patient_name": "Alice Doe",
            "patient_email": "alice@example.com",
            "symptoms": ["cough", "fever"],
        },
    )

    collector.record_action(
        "guardrail.blocked",
        parameters={"action": "patient.lookup", "identifier": "ssn 123-45-6789"},
        execution={"status": "blocked"},
        guardrail={
            "version": "health-guardrails-1",
            "bundle_hash": "guard-health",
            "status": "blocked",
            "action": "deny",
            "applied_rules": ["deny.unmasked.ssn"],
        },
    )

    collector.record_action(
        "tool.start",
        parameters={"name": "patient.lookup", "patient_email": "alice@example.com", "dob": "1990-01-02"},
        execution={"status": "started"},
        tool_name="patient.lookup",
        input_hash="h-input-health",
    )

    collector.record_action(
        "tool.end",
        parameters={"name": "patient.lookup", "result": "labs retrieved", "notes": "PHI redacted"},
        execution={"status": "success", "latency_ms": 210},
        tool_name="patient.lookup",
        input_hash="h-input-health",
        output_hash="h-output-health",
    )

    collector.record_action(
        "reasoning",
        parameters={"topic": "treatment-plan"},
        reasoning_preview="Check vitals and allergies before suggesting meds.",
        reasoning_hash="reason-health-1",
    )


def simulate_marketing(collector: SetorraCollector, scenario: str) -> None:
    collector.start_conversation(
        participants=[
            {"agent_id": "orchestrator", "role": "planner"},
            {"agent_id": "marketing-bot", "role": "executor"},
            {"agent_id": "deliverability-bot", "role": "observer"},
        ],
        edges=[
            {"from": "orchestrator", "to": "marketing-bot"},
            {"from": "marketing-bot", "to": "deliverability-bot"},
        ],
    )
    collector.send_message(
        to=["marketing-bot"],
        content={"campaign": "spring-promo", "audience": 12000, "channel": "email"},
    )

    collector.record_action(
        "tool.start",
        parameters={"name": "email.send", "template": "spring-2025", "audience": 12000},
        execution={"status": "started"},
        tool_name="email.send",
        input_hash="h-input-marketing",
    )

    collector.record_action(
        "tool.error",
        parameters={"name": "email.send", "template": "spring-2025"},
        execution={"status": "error", "latency_ms": 1200},
        error_type="HTTP 401 Unauthorized",
        error_preview="HTTP 401 Unauthorized: token expired",
        tool_name="email.send",
        input_hash="h-input-marketing",
    )

    collector.record_action(
        "tool.end",
        parameters={"name": "email.analytics", "opens": 432, "clicks": 28},
        execution={"status": "success", "latency_ms": 140},
        tool_name="email.analytics",
        input_hash="h-input-analytics",
        output_hash="h-output-analytics",
    )


def simulate_support(collector: SetorraCollector, scenario: str) -> None:
    collector.start_conversation(
        participants=[
            {"agent_id": "orchestrator", "role": "planner"},
            {"agent_id": "support-bot", "role": "resolver"},
            {"agent_id": "finance-analyst", "role": "reviewer"},
        ],
        edges=[
            {"from": "orchestrator", "to": "support-bot"},
            {"from": "support-bot", "to": "finance-analyst"},
        ],
    )
    collector.send_message(
        to=["support-bot"],
        content={"ticket": "TCK-88", "customer_email": "customer@example.com", "issue": "late delivery"},
    )

    collector.record_action(
        "tool.start",
        parameters={
            "name": "refund.issue",
            "amount": 250,
            "currency": "USD",
            "ticket_id": "TCK-88",
            "customer_email": "customer@example.com",
        },
        execution={"status": "started"},
        tool_name="refund.issue",
        input_hash="h-input-support",
    )

    policy_deny = {
        "version": "support-policy-2",
        "decision": "deny",
        "bundle_hash": "pol-support",
        "rule_id": "refund.high_risk",
        "fallback_mode": "fail_closed",
    }
    collector.record_action(
        "policy.deny",
        parameters={"action": "refund.issue", "amount": 250, "currency": "USD"},
        execution={"status": "denied"},
        policy=policy_deny,
    )

    collector.record_action(
        "agent.outcome",
        parameters={"status": "policy_denied", "ticket_id": "TCK-88"},
        execution={"status": "failed", "error_preview": "Policy blocked refund"},
        policy=policy_deny,
    )

    collector.record_inbound_message(
        from_agent="finance-analyst",
        content={"note": "refund denied; approval required", "ticket": "TCK-88"},
    )


def run_scenario(name: str, builder: Callable[[SetorraCollector, str], None]) -> Dict[str, str]:
    collector = setorra_collector(agent_name="orchestrator", agent_version="0.0.0-harness")
    collector._capture_multiagent = True  # ensure capture is on even if env is unset
    collector._organization_info = {"org_id": "org-harness", "org_name": "Harness Org", "key_id_prefix": "orgkey"}

    session_id = collector.start_session(
        environment=_common_env(name),
        system_prompt="You orchestrate specialists and record every action with guardrails and policy.",
        user_prompt=f"Execute scenario: {name}",
        model_id="gpt-4o-mini",
    )

    builder(collector, name)

    collector.end_session(
        final_output=f"{name} completed successfully",
        model_id="gpt-4o-mini",
        latency_ms=random.randint(400, 1200),
        cost_estimate=round(random.uniform(0.01, 0.05), 4),
    )

    # Count events from persisted evidence.jsonl (collector resets in-memory state after end_session)
    ev_file = ROOT / "data" / session_id / "evidence.jsonl"
    ev_count = 0
    if ev_file.exists():
        ev_count = sum(1 for _ in ev_file.read_text().splitlines() if _.strip())

    return {"scenario": name, "session_id": session_id, "events": ev_count}


def main() -> int:
    os.environ.setdefault("SETORRA_MULTIAGENT_CAPTURE", "1")
    os.environ.setdefault("SETORRA_ERRORS_AS_OUTPUT", "1")
    os.environ.setdefault("SETORRA_LOCAL_DATA_DIR", str(ROOT / "data"))
    Path(os.environ["SETORRA_LOCAL_DATA_DIR"]).mkdir(parents=True, exist_ok=True)
    _maybe_install_noop_backend()

    scenarios: List[tuple[str, Callable[[SetorraCollector, str], None]]] = [
        ("finance_approval", simulate_finance),
        ("healthcare_guardrail", simulate_healthcare),
        ("marketing_auth_failure", simulate_marketing),
        ("support_refund_deny", simulate_support),
    ]

    results = [run_scenario(name, builder) for name, builder in scenarios]

    # Optionally deliver artifacts to a live backend (default: deliver).
    deliver_enabled = os.getenv("SETORRA_HARNESS_NOOP", "").strip().lower() not in {"1", "true", "yes", "on"}
    base_url = os.getenv("SETORRA_BACKEND") or os.getenv("SETORRA_CONTROL_URL") or "http://127.0.0.1:5001"
    session_token = os.getenv("SETORRA_SESSION_TOKEN") or os.getenv("SETORRA_API_KEY") or "harness-token"
    org_id = os.getenv("SETORRA_ORG_ID") or "org-harness"
    if deliver_enabled:
        print(f"\n[deliver] Posting artifacts to backend {base_url} (token={'present' if session_token else 'missing'})")
        for res in results:
            session_id = res["session_id"]
            session_dir = ROOT / "data" / session_id
            try:
                evidence_bytes = (session_dir / "evidence.jsonl").read_bytes()
                output_bytes = (session_dir / "output.json").read_bytes()
                manifest_bytes = (session_dir / "manifest.json").read_bytes()
            except FileNotFoundError:
                print(f"[deliver] missing artifacts for session {session_id}, skipping")
                continue
            try:
                metrics = deliver_artifacts(
                    base_url=base_url,
                    session_token=session_token,
                    org_id=org_id,
                    agent_label="harness/0.0.0",
                    session_id=session_id,
                    evidence_bytes=evidence_bytes,
                    output_bytes=output_bytes,
                    manifest_bytes=manifest_bytes,
                )
                e = metrics.get("evidence")
                o = metrics.get("output")
                m = metrics.get("manifest")
                print(
                    f"[deliver] session={session_id} "
                    f"status(e/o/m)={(e.status if e else 'n/a')}/{(o.status if o else 'n/a')}/{(m.status if m else 'n/a')} "
                    f"bytes(e/o/m)={(e.bytes if e else 0)}/{(o.bytes if o else 0)}/{(m.bytes if m else 0)}"
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[deliver] error for session {session_id}: {exc}")
    else:
        print("\n[deliver] Skipped (SETORRA_HARNESS_NOOP enabled)")

    print("\n=== Harness Results ===")
    for res in results:
        print(f"{res['scenario']}: session={res['session_id']} events={res['events']}")
    print("\nArtifacts written under data/<session>/ with validation already applied via collector.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
