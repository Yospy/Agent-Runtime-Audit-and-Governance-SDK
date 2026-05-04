Multi‑Agent Capture (Python SDK)
================================

Goal
- Capture inter‑agent messages and handoffs without changing your agent logic.
- Additive only: existing single‑agent capture remains unchanged.

Enablement
- Set `SETORRA_MULTIAGENT_CAPTURE=1` (or pass `capture_multiagent=True` to `SetorraCollector`).

Core APIs (on SetorraCollector)
- `start_conversation(conversation_id=None, participants=None, edges=None)`
- `add_participant(agent_id, version=None, role=None)` / `remove_participant(...)`
- `send_message(to=[...], content, channel="delegate", metadata=None, from_agent=None, reply_to=None)`
- `record_inbound_message(from_agent, content, message_id=None, reply_to=None, metadata=None)`
- `handoff(to_agent, payload=None)` / `complete_handoff(child_run_id)` / `cancel_handoff(child_run_id, reason=None)`
- `close_conversation(final_status="success")`

Events Emitted
- `multiagent.topology`, `agent.joined`, `agent.left`
- `agent.message.outbound`, `agent.message.inbound`
- `agent.handoff.started`, `agent.handoff.completed`, `agent.handoff.cancelled`
- `conversation.summary`, `conversation.closed`

Deterministic IDs
- `conversation_id` (provided or generated)
- `message_id` (derived from conversation, sender, local sequence, content hash)
- `correlation_id` (per hop: conversation, message, from, to)

Privacy & Policy
- Content is redacted before persistence; only previews + hashes are stored.
- `send_message` evaluates the `agent.message` policy (ALLOW/DENY/REQUIRE_APPROVAL) and records the outcome per recipient.

Minimal Example (orchestrator → calendar → mail)
```python
from setorra import setorra_collector

collector = setorra_collector("scheduler-agent", "0.2.0")

collector.start_session(system_prompt="...", user_prompt="Schedule with [email]")
collector.start_conversation(
    conversation_id=None,
    participants=[
        {"agent_id": "scheduler-agent", "version": "0.2.0", "role": "orchestrator"},
        {"agent_id": "calendar-agent", "version": "2.0.0", "role": "executor"},
        {"agent_id": "mail-agent", "version": "0.3.0", "role": "executor"},
    ],
)

# Outbound to calendar
out1 = collector.send_message(to=["calendar-agent"], content={
    "summary": "Intro meeting",
    "start": {"dateTime": "2025-11-04T10:00:00+05:30", "timeZone": "Asia/Kolkata"},
    "end":   {"dateTime": "2025-11-04T10:30:00+05:30", "timeZone": "Asia/Kolkata"},
    "attendees": ["[email]"]
})

# Inbound at calendar
collector.record_inbound_message(from_agent="scheduler-agent", content={"...": "..."}, message_id=out1["message_id"])  # re-redacts locally

# Outbound to mail
out2 = collector.send_message(to=["mail-agent"], content={
    "to": ["[email]"],
    "subject": "Meeting: Tue 10:00–10:30",
    "ics": "[ICS_ATTACHMENT]",
})
collector.record_inbound_message(from_agent="scheduler-agent", content={"...": "..."}, message_id=out2["message_id"])  # at mail agent

collector.close_conversation(final_status="success")
collector.end_session(final_output={"output": "✅ Scheduled + invited"}, model_id="gpt-4o-mini")
```

Adapters (optional)
- LangGraph: `setorra.adapters.langgraph.SetorraLangGraphCallback` (call from your graph callbacks)
- LangChain: use existing `SetorraLangChainCallback` and optionally call `send_message`/`record_inbound_message` from routing code
- OpenAI Assistants: `setorra.adapters.openai_assistants.SetorraOpenAIAssistants`

Notes
- When the feature flag is off, the APIs are no-ops and your artifacts remain unchanged.
- These APIs do not perform delivery; they only record inter-agent intent/outcomes.

