"""OpenAI Assistants/Threads shim for Setorra multi-agent capture (Python).

This wrapper does not depend on the OpenAI client. Applications can call the
exposed methods from their own glue code to emit multi-agent events around
assistant/thread message exchanges.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional


class SetorraOpenAIAssistants:
    def __init__(self, collector: Any) -> None:
        self.collector = collector

    def on_thread_start(
        self,
        *,
        thread_id: Optional[str],
        participants: Iterable[Dict[str, Any]],
    ) -> None:
        self.collector.start_conversation(
            conversation_id=thread_id,
            participants=list(participants),
        )

    def post_to_assistant(
        self,
        *,
        from_agent: str,
        to_assistant: str,
        content: Any,
        reply_to: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.collector.send_message(
            to=[to_assistant],
            content=content,
            from_agent=from_agent,
            reply_to=reply_to,
            channel="delegate",
        )

    def assistant_reply(
        self,
        *,
        assistant_id: str,
        to_agent: Optional[str] = None,
        content: Any,
        message_id: Optional[str] = None,
    ) -> None:
        # Record inbound at the orchestrator/receiver side
        self.collector.record_inbound_message(
            from_agent=assistant_id,
            content=content,
            message_id=message_id,
        )

    def on_thread_end(self, *, status: str = "success") -> None:
        self.collector.close_conversation(final_status=status)

