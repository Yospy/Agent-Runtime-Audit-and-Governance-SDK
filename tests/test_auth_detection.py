import json
from pathlib import Path

from setorra.collector import SetorraCollector


def _read_output(run_dir: Path):
    with (run_dir / "output.json").open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _read_evidence(run_dir: Path):
    ev = []
    with (run_dir / "evidence.jsonl").open("r", encoding="utf-8") as fh:
        for line in fh:
            ev.append(json.loads(line))
    return ev


def test_tool_error_auth_401_classified(tmp_path: Path):
    storage = tmp_path / "evidence"
    c = SetorraCollector(
        agent_name="test-agent",
        agent_version="0.0.0",
        storage_dir=storage,
    )
    session_id = c.start_session()
    # Emit a tool error that should be classified as auth failure (401)
    c.record_action(
        "tool.error",
        parameters={"name": "gmail.send"},
        error_type="HttpError",
        error_preview="HTTP 401 Unauthorized (invalid_grant)",
    )
    # Finish the session
    result = c.end_session(final_output="done")
    run_dir = Path(result["evidence_path"]).parent

    out = _read_output(run_dir)
    assert out["schema_version"] == "1.3"
    assert isinstance(out.get("errors"), list)
    # Find the gmail error entry
    items = [e for e in out["errors"] if e.get("tool") == "gmail.send"]
    assert items, "expected gmail.send error entry in output summary"
    item = items[0]
    assert item.get("auth_failed") is True
    assert item.get("error_code") == 401
    assert item.get("reason_code") in {"AUTH_UNAUTHENTICATED", "AUTH_EXPIRED"}

    # Evidence should include additive fields on the tool.error event
    ev = _read_evidence(run_dir)
    tool_errs = [e for e in ev if e.get("event_type") == "tool.error"]
    assert tool_errs, "expected a tool.error event in evidence"
    exec_payload = tool_errs[0].get("execution", {})
    assert exec_payload.get("auth_failed") is True
    assert exec_payload.get("error_code") == 401


def test_tool_error_non_auth_not_classified(tmp_path: Path):
    storage = tmp_path / "evidence"
    c = SetorraCollector(
        agent_name="test-agent",
        agent_version="0.0.0",
        storage_dir=storage,
    )
    c.start_session()
    # Emit a non-auth tool error (e.g., rate limit or network)
    c.record_action(
        "tool.error",
        parameters={"name": "search.api"},
        error_type="HttpError",
        error_preview="HTTP 429 Too Many Requests",
    )
    result = c.end_session(final_output="done")
    run_dir = Path(result["evidence_path"]).parent

    out = _read_output(run_dir)
    items = [e for e in out.get("errors", []) if e.get("tool") == "search.api"]
    assert items, "expected search.api error entry"
    # Summary shows it as not auth related
    assert items[0].get("auth_failed") is False

    ev = _read_evidence(run_dir)
    tool_errs = [e for e in ev if e.get("event_type") == "tool.error"]
    exec_payload = tool_errs[0].get("execution", {})
    # Evidence should not include auth fields when not classified
    assert "auth_failed" not in exec_payload
    assert "error_code" not in exec_payload

