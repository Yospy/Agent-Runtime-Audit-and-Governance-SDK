"""LangGraph callback shim for Setorra multi-agent capture (Python).

This is a minimal adapter. It does not import LangGraph to avoid a hard
dependency. Applications can call these methods from their own callbacks
or wiring code to emit multi-agent events via the collector.

Usage (pseudo):
    cb = SetorraLangGraphCallback(collector)
    cb.on_graph_start(graph_run_id, participants, edges)
    # on node enter/edge send
    cb.on_edge_routed(from_node, to_nodes, content)
    # on node receive
    cb.on_node_receive(node_id, from_node, content, message_id)
    # at end
    cb.on_graph_end(status="success")
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional


class SetorraLangGraphCallback:
    def __init__(self, collector: Any) -> None:
        self.collector = collector

    def on_graph_start(
        self,
        *,
        graph_run_id: Optional[str] = None,
        participants: Optional[List[Dict[str, Any]]] = None,
        edges: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.collector.start_conversation(
            conversation_id=graph_run_id,
            participants=participants,
            edges=edges,
        )

    def on_edge_routed(
        self,
        *,
        from_node: str,
        to_nodes: Iterable[str],
        content: Any,
        channel: str = "delegate",
        reply_to: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.collector.send_message(
            to=list(to_nodes),
            content=content,
            channel=channel,
            from_agent=from_node,
            reply_to=reply_to,
        )

    def on_node_receive(
        self,
        *,
        node_id: str,
        from_node: str,
        content: Any,
        message_id: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> None:
        # Inbound is recorded from the perspective of the receiving node's collector.
        self.collector.record_inbound_message(
            from_agent=from_node,
            content=content,
            message_id=message_id,
            reply_to=reply_to,
        )

    def on_graph_end(self, *, status: str = "success") -> None:
        self.collector.close_conversation(final_status=status)

