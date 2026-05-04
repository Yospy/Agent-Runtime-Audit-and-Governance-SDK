import hashlib
import json
from pathlib import Path

from setorra.collector import SetorraCollector
from setorra.integrity import compute_event_hash


def _load_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _load_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_end_session_returns_artifact_paths_and_writes_to_storage_dir(tmp_path: Path):
    storage = tmp_path / "evidence"
    collector = SetorraCollector(
        agent_name="contract-agent",
        agent_version="1.0.0",
        storage_dir=storage,
    )

    session_id = collector.start_session(
        system_prompt="You are a support agent.",
        user_prompt="Refund alice@example.com",
    )
    collector.record_action(
        "tool.end",
        parameters={"name": "refund.issue", "customer": "alice@example.com"},
        output_preview={"status": "ok"},
        tool_name="refund.issue",
    )

    result = collector.end_session(final_output={"content": "done"})

    artifact_dir = storage / session_id
    assert Path(result["artifact_dir"]) == artifact_dir
    assert Path(result["evidence_path"]) == artifact_dir / "evidence.jsonl"
    assert Path(result["output_path"]) == artifact_dir / "output.json"
    assert Path(result["manifest_path"]) == artifact_dir / "manifest.json"
    assert artifact_dir.exists()

    output = _load_json(Path(result["output_path"]))
    manifest = _load_json(Path(result["manifest_path"]))
    evidence = _load_jsonl(Path(result["evidence_path"]))

    assert output["schema_version"] == "1.3"
    assert output["session_id"] == session_id
    assert output["errors"] == []
    assert output["errors_truncated"] == 0
    assert output["integrity"]["chain_length"] == len(evidence)

    assert manifest["schema_version"] == "manifest-v1"
    assert manifest["session_id"] == session_id
    assert manifest["chain_summary"]["chain_length"] == len(evidence)

    object_by_name = {item["name"]: item for item in manifest["objects"]}
    assert object_by_name["evidence.jsonl"]["sha256"] == hashlib.sha256(
        Path(result["evidence_path"]).read_bytes()
    ).hexdigest()
    assert object_by_name["output.json"]["sha256"] == hashlib.sha256(
        Path(result["output_path"]).read_bytes()
    ).hexdigest()


def test_evidence_events_use_event_index_and_hash_chain(tmp_path: Path):
    collector = SetorraCollector(
        agent_name="contract-agent",
        agent_version="1.0.0",
        storage_dir=tmp_path / "evidence",
    )
    collector.start_session()
    collector.record_action("tool.start", parameters={"name": "ticket.update"})
    result = collector.end_session(final_output="ok")

    events = _load_jsonl(Path(result["evidence_path"]))

    previous_hash = None
    for expected_index, event in enumerate(events):
        assert event["event_index"] == expected_index
        assert "chain_index" not in event
        assert event["integrity"]["prev_hash"] == previous_hash
        assert event["integrity"]["event_hash"] == compute_event_hash(event)
        previous_hash = event["integrity"]["event_hash"]


def test_tool_error_summary_uses_event_index_trace_pointer(tmp_path: Path):
    collector = SetorraCollector(
        agent_name="contract-agent",
        agent_version="1.0.0",
        storage_dir=tmp_path / "evidence",
    )
    collector.start_session()
    collector.record_action(
        "tool.error",
        parameters={"name": "gmail.send"},
        error_type="HttpError",
        error_preview="HTTP 403 forbidden: insufficient scopes",
    )
    result = collector.end_session(final_output="done")

    output = _load_json(Path(result["output_path"]))
    events = _load_jsonl(Path(result["evidence_path"]))
    error = output["errors"][0]

    assert error["tool"] == "gmail.send"
    assert error["auth_failed"] is True
    assert error["error_code"] == 403
    assert error["event_index"] >= 0
    assert "chain_index" not in error

    event = events[error["event_index"]]
    assert event["event_type"] == "tool.error"
    assert error["event_hash"] == event["integrity"]["event_hash"]
