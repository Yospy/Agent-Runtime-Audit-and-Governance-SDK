"""LangChain callback adapter for Setorra.

This handler plugs into LangChain's callback system and forwards notable LLM
and tool events into the Setorra collector. It stays compliant with LangChain's
`BaseCallbackHandler` contract so that it can be attached safely across
versions and in combination with other callbacks.

Privacy posture:
- No raw prompts or tool payloads are stored; only counts and redacted previews
  are passed to the collector (which performs masking again before persistence).
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, Optional

try:  # LangChain is an optional dependency at runtime.
    from langchain_core.callbacks import BaseCallbackHandler
except Exception:  # pragma: no cover - fallback for environments without LangChain.
    class BaseCallbackHandler:  # type: ignore
        """Minimal shim providing the attributes LangChain expects."""

        # Whether exceptions inside the handler should be re-raised.
        raise_error: bool = False
        # Flags controlling which event types to ignore.
        ignore_llm: bool = False
        ignore_chain: bool = False
        ignore_agent: bool = False
        ignore_chat_model: bool = False
        ignore_retriever: bool = True

        # Default event hooks (no-op).
        def __getattr__(self, name: str) -> Any:
            def _noop(*_args: Any, **_kwargs: Any) -> None:
                return None

            return _noop

from ..collector import SetorraCollector
from ..integrity import canonical_json
from ..redact import redact


class SetorraLangChainCallback(BaseCallbackHandler):
    """LangChain callback handler that forwards events to Setorra."""

    raise_error: bool = False
    ignore_llm: bool = False
    ignore_chain: bool = False
    ignore_agent: bool = False
    ignore_chat_model: bool = False
    ignore_retriever: bool = True

    def __init__(self, collector: SetorraCollector, *, model_id: Optional[str] = None) -> None:
        self.collector = collector
        self.model_id = model_id
        self._llm_t0: Optional[float] = None
        self._tool_t0: Optional[float] = None
        self._tool_name: Optional[str] = None
        self._chain_t0: Optional[float] = None
        self._tool_input_hash: Optional[str] = None

    # LLM lifecycle -----------------------------------------------------
    def on_llm_start(self, serialized: Any, prompts: Any, **kwargs: Any) -> None:  # noqa: D401 - external API
        self._llm_t0 = time.monotonic()
        try:
            self.collector.record_action(
                "llm.start",
                parameters={"prompt_count": len(prompts or [])},
                status="start",
                model_id=self.model_id,
                prompt_hashes=self._prompt_hashes(),
            )
        except Exception:
            pass

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:  # noqa: D401 - external API
        try:
            ms = int((time.monotonic() - (self._llm_t0 or time.monotonic())) * 1000)
            self.collector.record_action(
                "llm.end",
                parameters={},
                status="success",
                model_id=self.model_id,
                prompt_hashes=self._prompt_hashes(),
                output_preview=_preview_text(response),
                output_hash=_hash_payload(response),
                latency_ms=ms,
            )
        except Exception:
            pass

    def on_llm_error(self, error: Any, **kwargs: Any) -> None:  # noqa: D401 - external API
        try:
            ms = int((time.monotonic() - (self._llm_t0 or time.monotonic())) * 1000)
            self.collector.record_action(
                "llm.error",
                parameters={},
                status="error",
                model_id=self.model_id,
                prompt_hashes=self._prompt_hashes(),
                error_type=type(error).__name__,
                error_preview=_preview_text(error),
                latency_ms=ms,
            )
        except Exception:
            pass

    # Tool lifecycle ----------------------------------------------------
    def on_tool_start(self, serialized: Any, input_str: Any, **kwargs: Any) -> None:  # noqa: D401 - external API
        self._tool_t0 = time.monotonic()
        name = None
        try:
            name = (serialized or {}).get("name")
        except Exception:
            name = None
        self._tool_name = name or "<tool>"
        self._tool_input_hash = _hash_payload(input_str)
        try:
            self.collector.record_action(
                "tool.start",
                parameters={"name": self._tool_name, "args_preview": _preview_text(input_str)},
                status="start",
                input_hash=self._tool_input_hash,
                tool_name=self._tool_name,
            )
        except Exception:
            pass

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:  # noqa: D401 - external API
        try:
            ms = int((time.monotonic() - (self._tool_t0 or time.monotonic())) * 1000)
            self.collector.record_action(
                "tool.end",
                parameters={"name": self._tool_name or "<tool>"},
                status="success",
                output_preview=_preview_text(output),
                output_hash=_hash_payload(output),
                input_hash=self._tool_input_hash,
                latency_ms=ms,
                tool_name=self._tool_name,
            )
        except Exception:
            pass
        finally:
            self._tool_input_hash = None

    def on_tool_error(self, error: Any, **kwargs: Any) -> None:  # noqa: D401 - external API
        try:
            ms = int((time.monotonic() - (self._tool_t0 or time.monotonic())) * 1000)
            self.collector.record_action(
                "tool.error",
                parameters={"name": self._tool_name or "<tool>"},
                status="error",
                error_type=type(error).__name__,
                error_preview=_preview_text(error),
                input_hash=self._tool_input_hash,
                latency_ms=ms,
                tool_name=self._tool_name,
            )
        except Exception:
            pass
        finally:
            self._tool_input_hash = None

    # Agent high-level --------------------------------------------------
    def on_agent_action(self, action: Any, **kwargs: Any) -> None:  # noqa: D401 - external API
        try:
            log = getattr(action, "log", "") or ""
            tool = getattr(action, "tool", None)
            reasoning_preview = _preview_text(log)
            reasoning_hash = _hash_payload(log) if log else None
            self.collector.record_action(
                "reasoning",
                parameters={"stage": "agent.action", "tool": tool or "<unknown>"},
                status="observed",
                reasoning_hash=reasoning_hash,
                reasoning_preview=reasoning_preview,
            )
        except Exception:
            pass

    def on_agent_finish(self, finish: Any, **kwargs: Any) -> None:  # noqa: D401 - external API
        try:
            log = getattr(finish, "log", "") or ""
            reasoning_preview = _preview_text(log)
            reasoning_hash = _hash_payload(log) if log else None
            self.collector.record_action(
                "agent.finish_detail",
                parameters={},
                status="finished",
                reasoning_hash=reasoning_hash,
                reasoning_preview=reasoning_preview,
            )
        except Exception:
            pass

    # Chain lifecycle ---------------------------------------------------
    def on_chain_start(self, serialized: Any, inputs: Any, **kwargs: Any) -> None:  # noqa: D401 - external API
        self._chain_t0 = time.monotonic()
        try:
            params_redacted, _, _ = redact(inputs)
            self.collector.record_action(
                "chain.start",
                parameters={"type": (serialized or {}).get("name") if isinstance(serialized, dict) else None},
                status="start",
                execution={"input_preview": params_redacted},
            )
        except Exception:
            pass

    def on_chain_end(self, outputs: Any, **kwargs: Any) -> None:  # noqa: D401 - external API
        try:
            ms = int((time.monotonic() - (self._chain_t0 or time.monotonic())) * 1000)
            out_prev, _, _ = redact(outputs)
            self.collector.record_action(
                "chain.end",
                parameters={},
                status="success",
                output_preview=out_prev,
                output_hash=_hash_payload(outputs),
                latency_ms=ms,
            )
        except Exception:
            pass

    def on_chain_error(self, error: Any, **kwargs: Any) -> None:  # noqa: D401 - external API
        try:
            ms = int((time.monotonic() - (self._chain_t0 or time.monotonic())) * 1000)
            err_prev, _, _ = redact(error)
            self.collector.record_action(
                "chain.error",
                parameters={},
                status="error",
                error_type=type(error).__name__,
                error_preview=err_prev,
                latency_ms=ms,
            )
        except Exception:
            pass

    def _prompt_hashes(self) -> Dict[str, Optional[str]]:
        try:
            return self.collector.prompt_hashes()
        except Exception:
            return {}


def _preview_text(value: Any, limit: int = 200) -> str:
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:
        text = repr(value)
    return text.replace("\n", " ")[:limit]


def _hash_payload(payload: Any) -> str:
    try:
        canonical = canonical_json(payload)
    except Exception:
        canonical = str(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

# Backward-compatible alias for older imports.
TisacLangChainCallback = SetorraLangChainCallback
