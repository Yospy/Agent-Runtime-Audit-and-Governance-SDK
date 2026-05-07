# Setorra SDK Reference Documentation

**Version:** 1.3  
**Last Updated:** 2025-01-XX

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Installation](#installation)
4. [Quick Start](#quick-start)
5. [Core API Reference](#core-api-reference)
6. [Integration Patterns](#integration-patterns)
7. [Policy System](#policy-system)
8. [Guardrails](#guardrails)
9. [Privacy & Redaction](#privacy--redaction)
10. [Evidence & Storage](#evidence--storage)
11. [Multi-Agent Support](#multi-agent-support)
12. [Configuration](#configuration)
13. [Framework Adapters](#framework-adapters)
14. [Error Handling](#error-handling)
15. [Advanced Usage](#advanced-usage)

---

## Overview

Setorra is a security and governance SDK for AI agents that produces compliance-grade, tamper-evident audit evidence. It integrates into agent frameworks to capture runs, enforce policies and guardrails, redact PII/secrets, and generate evidence artifacts suitable for SOC2/ISO audits and vendor due diligence.

### Key Features

- **Policy Enforcement**: Threshold-based and attribute-based decisions (ALLOW, DENY, REQUIRE_APPROVAL, SANDBOX)
- **Runtime Guardrails**: Developer-defined rules that execute before policy evaluation
- **Privacy-First Redaction**: Automatic PII and secret detection before persistence
- **Tamper-Evident Chains**: Hash-linked, signed event sequences with integrity verification
- **Compliance-Ready Evidence**: Exportable evidence packs mapping to SOC2/ISO controls
- **Multi-Agent Support**: Capture inter-agent communication and handoffs
- **Framework Integration**: Drop-in wrappers for LangChain, LangGraph, OpenAI Assistants, and custom agents

### Core Concepts

- **Run**: A single agent execution session with a stable session ID
- **Step**: A discrete unit of agent reasoning or action decision within a run
- **Action**: A concrete operation an agent attempts (e.g., `payment.transfer`, `email.send`)
- **Policy**: A versioned set of rules determining action outcomes
- **Decision**: Policy outcome - ALLOW, DENY, REQUIRE_APPROVAL, or SANDBOX
- **Evidence Event**: A structured, redacted, signed record capturing who, what, when, under which policy, and with what result
- **Integrity Chain**: Hash-linked sequence of signed events enabling tamper evidence

---

## Architecture

### Event Flow

```
agent.started
  ↓
session.manifest
  ↓
prompt.capture
  ↓
[guardrail evaluation] → [policy evaluation]
  ↓
tool.start / llm.start
  ↓
tool.end / llm.end
  ↓
agent.outcome
  ↓
agent.finished
```

### Artifact Structure

Each run produces a directory `evidence/<session_id>/` containing:

- **`evidence.jsonl`**: Newline-delimited JSON stream of all events with integrity hashes
- **`output.json`**: Single JSON summary with prompts, tools, outcomes, policy/guardrail summaries
- **`manifest.json`**: Commit marker with sizes, hashes, chain summary, and integrity root

### Component Organization

```
setorra/
├── __init__.py          # Public API exports
├── collector.py         # Core SetorraCollector (session lifecycle, events)
├── integration.py       # invoke() wrapper and Runner facade
├── policy.py            # PolicyLoader, PolicyDecisionPoint
├── guardrails.py        # GuardrailLoader, GuardrailEnforcer
├── redact.py            # PII/secret detection and redaction
├── schema.py            # EvidenceEvent, OutputSummary dataclasses
├── storage.py           # Atomic file writers and utilities
├── integrity.py         # Hash computation and canonicalization
├── remote.py            # Backend handshake client
├── multiagent.py        # Multi-agent messaging helpers
├── context.py           # Thread-local session/plan context
├── id.py                # ULID generation
└── adapters/
    ├── langchain.py     # LangChain callback handler
    ├── langgraph.py     # LangGraph callback handler
    └── openai_assistants.py  # OpenAI Assistants integration
```

---

## Installation

### Requirements

- Python 3.8+
- No required external dependencies for core functionality
- Optional: `yaml` for YAML guardrail bundles

### Installation Methods

**Via Poetry (development):**
```bash
poetry install
```

**Via pip:**
```bash
pip install setorra
```

**From source:**
```bash
git clone <repository>
cd setorra
pip install -e .
```

---

## Quick Start

### Runner Pattern (Recommended)

```python
from setorra import runner

# Initialize once
run = runner(
    agent_name="my-agent",
    agent_version="1.0.0",
    policy="policy.json",          # Optional: path to policy bundle
    guardrails="guardrails.yaml",  # Optional: path to guardrail bundle
    environment="prod",            # Optional: environment label
)

# Invoke agent
result = run.invoke(
    agent_or_executor=my_agent,
    inputs={"query": "..."},
    system_prompt="You are a helpful assistant.",
    user_prompt="What is the weather?",
)

print(f"Session ID: {result.session_id}")
print(f"Output: {result.output}")
```

### Legacy Pattern (Still Supported)

```python
from setorra import setorra_collector, invoke

collector = setorra_collector(
    agent_name="my-agent",
    agent_version="1.0.0"
)

result = invoke(
    collector,
    agent_or_executor=my_agent,
    inputs={"query": "..."},
    system_prompt="You are a helpful assistant.",
    user_prompt="What is the weather?",
)

print(f"Session ID: {result.session_id}")
```

---

## Core API Reference

### `setorra_collector(agent_name: str, agent_version: str) -> SetorraCollector`

Factory function that creates a `SetorraCollector` instance with sensible defaults.

**Parameters:**
- `agent_name` (str): Identifier for the agent (e.g., "finance-agent")
- `agent_version` (str): Version string (e.g., "1.0.0")

**Returns:** `SetorraCollector` instance

**Environment Variables Used:**
- `SETORRA_ENVIRONMENT`: Deployment environment label (default: "local")
- `SETORRA_HOSTNAME`: Override hostname (default: socket.gethostname())
- `SETORRA_SIGNING_KEY`: HMAC signing key for integrity signatures
- `SETORRA_SIGNING_KEY_ID`: Identifier for the signing key
- `SETORRA_POLICY_PATH`: Path to policy bundle JSON file
- `SETORRA_GUARDRAIL_PATH`: Path to guardrail bundle (JSON or YAML)
- `SETORRA_POLICY_AUTO_WAIT`: Whether to wait for approvals (default: "true")
- `SETORRA_API_KEY`: API key for backend handshake
- `SETORRA_BACKEND`: Backend base URL for control plane

**Example:**
```python
import os
os.environ["SETORRA_POLICY_PATH"] = "/path/to/policy.json"
collector = setorra_collector("my-agent", "1.0.0")
```

---

### `SetorraCollector`

Core class orchestrating session lifecycle, event emission, policy/guardrail enforcement, and artifact generation.

#### Constructor

```python
SetorraCollector(
    agent_name: str,
    agent_version: str,
    storage_dir: Path | str = DEFAULT_DIR,
    max_buffer_bytes: int = 10 * 1024 * 1024,
    *,
    environment: str = "local",
    hostname: Optional[str] = None,
    sdk_version: Optional[str] = None,
    signing_key: Optional[bytes | str] = None,
    signing_key_id: Optional[str] = None,
    policy_loader: Optional[PolicyLoader] = None,
    policy_decider: Optional[PolicyDecisionPoint] = None,
    auto_wait_for_approval: bool = True,
    approval_notifier: Optional[ApprovalNotifier] = None,
    guardrail_loader: Optional[GuardrailLoader] = None,
    guardrail_enforcer: Optional[GuardrailEnforcer] = None,
    capture_plans: Optional[bool] = None,
    capture_multiagent: Optional[bool] = None,
)
```

**Parameters:**
- `agent_name` (str): Agent identifier
- `agent_version` (str): Agent version string
- `storage_dir` (Path | str): Directory for evidence artifacts (default: "evidence")
- `max_buffer_bytes` (int): Maximum in-memory buffer size before flush (default: 10MB)
- `environment` (str): Environment label (default: "local")
- `hostname` (str, optional): Override hostname
- `signing_key` (bytes | str, optional): HMAC signing key
- `signing_key_id` (str, optional): Signing key identifier
- `policy_loader` (PolicyLoader, optional): Policy bundle loader
- `policy_decider` (PolicyDecisionPoint, optional): Policy decision evaluator
- `auto_wait_for_approval` (bool): Block on approval requirements (default: True)
- `approval_notifier` (ApprovalNotifier, optional): Callback for approval notifications
- `guardrail_loader` (GuardrailLoader, optional): Guardrail bundle loader
- `guardrail_enforcer` (GuardrailEnforcer, optional): Guardrail enforcer instance
- `capture_plans` (bool, optional): Enable planning capture (default: from env)
- `capture_multiagent` (bool, optional): Enable multi-agent capture (default: from env)

#### Session Lifecycle Methods

##### `start_session(...) -> str`

Start a new agent run session. Must be called before recording actions.

```python
session_id = collector.start_session(
    meta: Optional[Dict[str, Any]] = None,
    *,
    policy_version: str = "0",
    capability_scope: Optional[Any] = None,
    environment: Optional[Dict[str, Any]] = None,
    system_prompt: Optional[str] = None,
    user_prompt: Optional[str] = None,
    model_id: Optional[str] = None,
) -> str
```

**Returns:** Session ID (ULID string)

**Emits Events:**
- `agent.started`
- `session.manifest`
- `prompt.capture`

**Example:**
```python
session_id = collector.start_session(
    meta={"user_id": "user123"},
    system_prompt="You are a helpful assistant.",
    user_prompt="What is the weather?",
    model_id="gpt-4",
)
```

##### `end_session(final_output: Any = None, model_id: Optional[str] = None) -> dict`

Finalize the current session, write artifacts to disk, and return artifact paths.

**Parameters:**
- `final_output` (Any): Final output value to include in summary
- `model_id` (str, optional): Model identifier used

**Emits Events:**
- `agent.outcome`
- `agent.finished`

**Artifacts Written:**
- `evidence/<session_id>/evidence.jsonl`
- `evidence/<session_id>/output.json`
- `evidence/<session_id>/manifest.json`

**Returns:**
- `session_id`
- `artifact_dir`
- `evidence_path`
- `output_path`
- `manifest_path`

**Example:**
```python
artifacts = collector.end_session(final_output={"result": "success"})
print(artifacts["evidence_path"])
```

#### Action Recording Methods

##### `record_action(...) -> str`

Record a discrete action or step within the session.

```python
event_hash = collector.record_action(
    action_type: str,
    parameters: Optional[Any] = None,
    *,
    status: str = "start",
    model_id: Optional[str] = None,
    prompt_hashes: Optional[Dict[str, Optional[str]]] = None,
    input_hash: Optional[str] = None,
    output_preview: Optional[str] = None,
    output_hash: Optional[str] = None,
    latency_ms: Optional[int] = None,
    error_type: Optional[str] = None,
    error_preview: Optional[str] = None,
    tool_name: Optional[str] = None,
    reasoning: Optional[str] = None,
    consent: Optional[Dict[str, Any]] = None,
) -> str
```

**Parameters:**
- `action_type` (str): Event type (e.g., "tool.start", "llm.end")
- `parameters` (Any, optional): Action parameters (will be redacted)
- `status` (str): Execution status ("start", "success", "error", "denied")
- `model_id` (str, optional): Model identifier
- `prompt_hashes` (Dict, optional): Prompt hash map
- `input_hash` (str, optional): Input content hash
- `output_preview` (str, optional): Redacted output preview
- `output_hash` (str, optional): Output content hash
- `latency_ms` (int, optional): Latency in milliseconds
- `error_type` (str, optional): Error type name
- `error_preview` (str, optional): Redacted error message
- `tool_name` (str, optional): Tool name for tool events
- `reasoning` (str, optional): Reasoning text for agent reasoning events
- `consent` (Dict, optional): Consent record

**Returns:** Event hash (SHA-256 hex digest)

**Example:**
```python
# Tool start
collector.record_action(
    "tool.start",
    parameters={"name": "weather_api", "location": "SF"},
    status="start",
    tool_name="weather_api",
)

# Tool end
collector.record_action(
    "tool.end",
    parameters={"name": "weather_api"},
    status="success",
    output_preview="Sunny, 72°F",
    output_hash=hashlib.sha256(b"Sunny").hexdigest(),
    latency_ms=150,
    tool_name="weather_api",
)
```

##### `record_outcome(...) -> None`

Record the final outcome of the session.

```python
collector.record_outcome(
    status: str,
    *,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None
```

**Parameters:**
- `status` (str): Final status ("success", "failure")
- `error_type` (str, optional): Error type if failed
- `error_message` (str, optional): Error message (will be redacted)

#### Policy & Guardrail Methods

##### `enforce_policy(...) -> PolicyDecision`

Evaluate policy for an action and return the decision.

```python
decision = collector.enforce_policy(
    action_type: str,
    actor: Dict[str, Any],
    parameters: Any,
    context: Optional[Dict[str, Any]] = None,
    *,
    wait_for_approval: Optional[bool] = None,
) -> PolicyDecision
```

**Parameters:**
- `action_type` (str): Action name (e.g., "payment.transfer")
- `actor` (Dict): Actor attributes (type, name, version, etc.)
- `parameters` (Any): Action parameters
- `context` (Dict, optional): Additional context
- `wait_for_approval` (bool, optional): Override auto_wait_for_approval

**Returns:** `PolicyDecision` with `decision` field ("allow", "deny", "require_approval")

**Raises:**
- `PolicyDeniedError`: When action is denied
- `PolicyApprovalTimeout`: When approval times out
- `PolicyApprovalCancelled`: When approval is cancelled

**Example:**
```python
try:
    decision = collector.enforce_policy(
        "payment.transfer",
        actor={"type": "agent", "name": collector.agent_name},
        parameters={"amount": 1000, "recipient": "user@example.com"},
        context={"environment": "prod"},
    )
    if decision.decision == "require_approval":
        # Handle approval requirement
        pass
except PolicyDeniedError:
    # Handle denial
    pass
```

##### `apply_guardrails(...) -> GuardrailEvaluation`

Apply runtime guardrails to an action.

```python
result = collector.apply_guardrails(
    action_type: str,
    actor: Dict[str, Any],
    parameters: Any,
    context: Optional[Dict[str, Any]] = None,
) -> GuardrailEvaluation
```

**Parameters:**
- `action_type` (str): Action name
- `actor` (Dict): Actor attributes
- `parameters` (Any): Action parameters
- `context` (Dict, optional): Additional context

**Returns:** `GuardrailEvaluation` with `decision` and `modifications` fields

**Raises:**
- `GuardrailViolationError`: When action is denied by guardrails

**Example:**
```python
try:
    result = collector.apply_guardrails(
        "agent.invoke",
        actor={"type": "agent", "name": collector.agent_name},
        parameters={"inputs": user_inputs},
    )
    if result.modifications:
        # Apply modifications to parameters
        for mod in result.modifications:
            # Update parameters based on mod["path"] and mod["value"]
            pass
except GuardrailViolationError as e:
    # Handle guardrail denial
    pass
```

#### Multi-Agent Methods

##### `start_conversation(...) -> Optional[str]`

Start or attach to a multi-agent conversation (feature-gated).

```python
conversation_id = collector.start_conversation(
    *,
    conversation_id: Optional[str] = None,
    participants: Optional[List[Dict[str, Any]]] = None,
    edges: Optional[List[Dict[str, Any]]] = None,
) -> Optional[str]
```

**Parameters:**
- `conversation_id` (str, optional): Existing conversation ID
- `participants` (List, optional): List of participant dicts with `agent_id`, `version`, `role`
- `edges` (List, optional): List of edge dicts describing communication topology

**Returns:** Conversation ID or None if multi-agent capture is disabled

**Feature Gate:** Requires `SETORRA_MULTIAGENT_CAPTURE=1` or `capture_multiagent=True`

##### `send_message(...) -> Dict[str, Any]`

Record an outbound message attempt (policy-gated).

```python
result = collector.send_message(
    *,
    to: List[str],
    content: Any,
    channel: str = "delegate",
    metadata: Optional[Dict[str, Any]] = None,
    from_agent: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> Dict[str, Any]
```

**Returns:** Dict with `message_id`, `result`, and `recipients` list

**Emits Event:** `agent.message.outbound`

##### `record_inbound_message(...) -> Optional[str]`

Record receipt of an inbound message.

```python
message_id = collector.record_inbound_message(
    *,
    from_agent: str,
    content: Any,
    message_id: Optional[str] = None,
    reply_to: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[str]
```

**Returns:** Message ID or None if multi-agent capture is disabled

**Emits Event:** `agent.message.inbound`

---

### `Runner` Class

Standardized integration facade that simplifies SDK usage.

#### Constructor

```python
Runner(
    agent_name: str,
    agent_version: str,
    *,
    policy: Optional[str] = None,
    guardrails: Optional[str] = None,
    environment: Optional[str] = None,
    errors_as_output: bool = True,
)
```

**Parameters:**
- `agent_name` (str): Agent identifier
- `agent_version` (str): Agent version string
- `policy` (str, optional): Path to policy bundle
- `guardrails` (str, optional): Path to guardrail bundle
- `environment` (str, optional): Environment label
- `errors_as_output` (bool): Return errors as output instead of raising (default: True)

#### Methods

##### `invoke(...) -> InvokeResult`

Run an agent/executor with Setorra capture.

```python
result = runner.invoke(
    agent_or_executor: Any,
    inputs: Any = None,
    *,
    model_id: Optional[str] = None,
    system_prompt: Optional[str] = None,
    user_prompt: Optional[str] = None,
    policy_version: str = "0",
    capability_scope: Optional[Any] = None,
    environment: Optional[Dict[str, Any]] = None,
    errors_as_output: Optional[bool] = None,
    **invoke_kwargs: Any,
) -> InvokeResult
```

**Returns:** `InvokeResult` with `session_id` and `output` fields

**Example:**
```python
run = runner("my-agent", "1.0.0", policy="policy.json")
result = run.invoke(
    my_agent,
    inputs={"query": "..."},
    system_prompt="You are helpful.",
)
print(result.session_id, result.output)
```

##### `invoker() -> Callable`

Return a callable that mirrors `.invoke()` for convenience.

```python
invoke_fn = runner.invoker()
result = invoke_fn(my_agent, inputs={"query": "..."})
```

##### `collector: SetorraCollector` (Property)

Access the underlying collector for advanced use cases.

```python
run = runner("my-agent", "1.0.0")
collector = run.collector
# Use collector for manual event recording, etc.
```

---

### `runner()` Factory Function

```python
run = runner(
    agent_name: str,
    agent_version: str,
    *,
    policy: Optional[str] = None,
    guardrails: Optional[str] = None,
    environment: Optional[str] = None,
    errors_as_output: bool = True,
) -> Runner
```

Convenience factory that creates a `Runner` instance. See `Runner` class above for details.

---

### `invoke()` Function

Legacy wrapper function that runs an agent with Setorra capture.

```python
result = invoke(
    collector: SetorraCollector,
    agent_or_executor: Any,
    inputs: Any = None,
    meta: Optional[Dict[str, Any]] = None,
    *,
    model_id: Optional[str] = None,
    system_prompt: Optional[str] = None,
    user_prompt: Optional[str] = None,
    policy_version: str = "0",
    capability_scope: Optional[Any] = None,
    environment: Optional[Dict[str, Any]] = None,
    errors_as_output: Optional[bool] = None,
    **invoke_kwargs: Any,
) -> InvokeResult
```

**Parameters:**
- `collector` (SetorraCollector): Collector instance
- `agent_or_executor` (Any): Agent callable (supports `.invoke()`, `.run()`, or callable)
- `inputs` (Any): Input to pass to agent
- `meta` (Dict, optional): Metadata for session
- `model_id` (str, optional): Model identifier
- `system_prompt` (str, optional): System prompt
- `user_prompt` (str, optional): User prompt
- `policy_version` (str): Policy version string (default: "0")
- `capability_scope` (Any, optional): Capability scope for policy
- `environment` (Dict, optional): Environment metadata
- `errors_as_output` (bool, optional): Return errors as output (default: from env or False)
- `**invoke_kwargs`: Additional kwargs passed to agent

**Returns:** `InvokeResult` with `session_id` and `output`

**Supported Agent Patterns:**
- Objects with `.invoke(inputs, **kwargs)` method (LangChain/LangGraph)
- Objects with `.run(inputs, **kwargs)` method
- Plain callables `fn(inputs)` or `fn()`

**Example:**
```python
from setorra import setorra_collector, invoke

collector = setorra_collector("my-agent", "1.0.0")
result = invoke(
    collector,
    my_agent,
    inputs={"query": "..."},
    system_prompt="You are helpful.",
)
```

---

## Integration Patterns

### LangChain Integration

```python
from setorra import runner, SetorraLangChainCallback
from langchain.chains import LLMChain

run = runner("my-agent", "1.0.0", policy="policy.json")

# Create callback
callback = SetorraLangChainCallback(
    run.collector,
    model_id="gpt-4"
)

# Use with LangChain chain
chain = LLMChain(llm=llm, prompt=prompt)
result = chain.invoke(
    {"input": "..."},
    config={"callbacks": [callback]}
)
```

### LangGraph Integration

```python
from setorra import runner, SetorraLangGraphCallback

run = runner("my-agent", "1.0.0")

callback = SetorraLangGraphCallback(run.collector)

# Use with LangGraph app
app = create_agent()
result = app.invoke(
    {"messages": [("user", "...")]},
    config={"callbacks": [callback]}
)
```

### OpenAI Assistants Integration

```python
from setorra import runner, SetorraOpenAIAssistants

run = runner("my-agent", "1.0.0")

# Wrap OpenAI client
wrapper = SetorraOpenAIAssistants(
    run.collector,
    openai_client=openai_client
)

# Use wrapper methods
thread = wrapper.create_thread()
run = wrapper.create_run(assistant_id, thread_id)
```

### Custom Agent Integration

```python
from setorra import runner

run = runner("my-agent", "1.0.0", policy="policy.json")

# Define your agent
class MyAgent:
    def invoke(self, inputs):
        # Your agent logic
        return {"result": "..."}

agent = MyAgent()

# Run with Setorra
result = run.invoke(agent, inputs={"query": "..."})
```

### Manual Event Recording

```python
from setorra import setorra_collector

collector = setorra_collector("my-agent", "1.0.0")
session_id = collector.start_session(
    user_prompt="What is the weather?",
    model_id="gpt-4"
)

# Record tool call
collector.record_action(
    "tool.start",
    parameters={"name": "weather_api", "location": "SF"},
    tool_name="weather_api",
    status="start"
)

# ... execute tool ...

collector.record_action(
    "tool.end",
    parameters={"name": "weather_api"},
    tool_name="weather_api",
    status="success",
    output_preview="Sunny, 72°F",
    latency_ms=150
)

collector.end_session(final_output={"weather": "Sunny"})
```

---

## Policy System

### Policy Bundle Format

Policies are defined in JSON bundles with the following structure:

```json
{
  "version": "1.0.0",
  "metadata": {
    "name": "Finance Policy",
    "description": "Policy for finance operations"
  },
  "defaults": {
    "decision": "deny",
    "fail_mode": "fail-closed"
  },
  "sdk_defaults": {
    "decision": "deny",
    "fail_mode": "fail-closed"
  },
  "actions": {
    "payment.transfer": {
      "default_decision": "require_approval",
      "fail_mode": "require-approval",
      "approval": {
        "requires_token": true,
        "expires_in": 900,
        "approver_roles": ["finance_manager"]
      },
      "rules": [
        {
          "rule_id": "high-amount",
          "decision": "require_approval",
          "match": {
            "parameters.amount": {"$gt": 10000}
          },
          "rationale": "High-value transfers require approval"
        },
        {
          "rule_id": "unauthorized-recipient",
          "decision": "deny",
          "match": {
            "parameters.recipient": {"$in": ["blocked@example.com"]}
          },
          "rationale": "Recipient is blocked"
        }
      ]
    }
  }
}
```

### PolicyLoader

```python
from setorra import PolicyLoader

loader = PolicyLoader(path=Path("policy.json"))
bundle = loader.get_bundle()

# Access policy version, actions, etc.
print(bundle.version)
print(bundle.actions.keys())
```

### PolicyDecisionPoint

```python
from setorra import PolicyLoader, PolicyDecisionPoint

loader = PolicyLoader(path=Path("policy.json"))
decider = PolicyDecisionPoint(loader)

decision = decider.evaluate(
    action_type="payment.transfer",
    actor={"type": "agent", "name": "finance-agent"},
    parameters={"amount": 5000, "recipient": "user@example.com"},
    context={}
)

print(decision.decision)  # "allow", "deny", "require_approval"
```

### Approval Flow

```python
from setorra import PolicyApprovalRequirement, PolicyApprovalReceipt

# When decision is "require_approval"
requirement = decision.requirement  # PolicyApprovalRequirement

# Request approval (implement your own flow)
approval_token = request_approval_from_user(requirement)

# Provide approval receipt
receipt = PolicyApprovalReceipt(
    intent_hash=requirement.intent_hash,
    token=approval_token,
    approver_id="user123",
    approver_email="approver@example.com"
)

# Attach approval to collector
collector.attach_approval(receipt)
```

---

## Guardrails

### Guardrail Bundle Format

Guardrails are defined in JSON or YAML bundles:

```json
{
  "version": "1.0.0",
  "metadata": {
    "name": "Input Sanitization",
    "description": "Sanitize inputs before processing"
  },
  "rules": [
    {
      "rule_id": "clamp-amount",
      "effect": "modify",
      "match": {
        "action_type": "payment.transfer",
        "parameters.amount": {"$gt": 10000}
      },
      "modify": {
        "parameters.amount": 10000
      },
      "rationale": "Clamp amounts above threshold"
    },
    {
      "rule_id": "deny-invalid-email",
      "effect": "deny",
      "match": {
        "parameters.recipient": {"$regex": ".*@blocked\\.com"}
      },
      "rationale": "Blocked domain"
    }
  ]
}
```

### GuardrailLoader

```python
from setorra import GuardrailLoader

loader = GuardrailLoader(path=Path("guardrails.yaml"))
bundle = loader.get_bundle()

# Access guardrail version, rules, etc.
print(bundle.version)
print(bundle.rules)
```

### GuardrailEnforcer

```python
from setorra import GuardrailLoader, GuardrailEnforcer

loader = GuardrailLoader(path=Path("guardrails.yaml"))
enforcer = GuardrailEnforcer(loader)

result = enforcer.evaluate(
    action_type="payment.transfer",
    actor={"type": "agent", "name": "finance-agent"},
    parameters={"amount": 15000, "recipient": "user@example.com"},
    context={}
)

print(result.decision)  # "allow", "deny", "modify"
if result.modifications:
    # Apply modifications to parameters
    for mod in result.modifications:
        # Update based on mod["path"] and mod["value"]
        pass
```

---

## Privacy & Redaction

### Redaction Function

```python
from setorra import redact

data = {
    "email": "user@example.com",
    "phone": "555-1234",
    "message": "Call me at 555-1234"
}

redacted, flags, fields = redact(data)

print(redacted)
# {
#   "email": "[email]",
#   "phone": "[phone]",
#   "message": "Call me at [phone]"
# }

print(flags)  # {"email", "phone"}
print(fields)  # ["email", "phone", "message"]
```

### Detected PII Types

- **Email**: `[email]`
- **Phone**: `[phone]` (US formats and E.164)
- **Secrets**: `[secret]` (API keys, tokens)
- **Bearer tokens**: `[bearer]`
- **JWT**: `[jwt]`
- **PEM keys**: `[pem]`
- **Cards**: `[card]` (PAN with Luhn validation)
- **IBAN**: `[iban]` (with mod-97 validation)
- **Routing**: `[routing]` (ABA routing with checksum)
- **SSN**: `[ssn]`
- **CVV**: `[cvv]` (context-gated)
- **Account numbers**: `[acct]` (context-gated)
- **IP addresses**: `[ip]` (IPv4/IPv6)
- **DOB**: `[dob]` (context-gated, strict mode)
- **Addresses**: `[address]` (context-gated, strict mode)

### Configuration

**Environment Variables:**
- `SETORRA_REDACTION_LEVEL`: `standard` (default) or `strict`
- `SETORRA_REDACTION_DISABLE`: Comma-separated list of flags to disable (e.g., `email,phone`)
- `SETORRA_REDACTION_ENABLE`: Comma-separated list of flags to enable

**Example:**
```bash
export SETORRA_REDACTION_LEVEL=strict
export SETORRA_REDACTION_DISABLE=ip
```

---

## Evidence & Storage

### Evidence Schema

Each event in `evidence.jsonl` follows this structure:

```json
{
  "schema_version": "1.3",
  "session_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
  "event_index": 0,
  "timestamp": "2025-01-15T10:30:00Z",
  "agent_name": "my-agent",
  "agent_version": "1.0.0",
  "event_type": "agent.started",
  "parameters_redacted": {},
  "privacy_flags": [],
  "redacted_fields": [],
  "context": {
    "environment": {
      "sdk_version": "1.0.0",
      "environment": "prod",
      "hostname": "server-01"
    }
  },
  "execution": {
    "status": "started"
  },
  "policy": {},
  "approval": null,
  "guardrail": {},
  "integrity": {
    "event_hash": "abc123...",
    "prior_hash": null,
    "signature": null,
    "event_index": 0
  }
}
```

### Output Summary Schema

The `output.json` file contains:

```json
{
  "schema_version": "1.3",
  "session_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
  "agent_name": "my-agent",
  "agent_version": "1.0.0",
  "started_at": "2025-01-15T10:30:00Z",
  "finished_at": "2025-01-15T10:30:05Z",
  "duration_ms": 5000,
  "model_id": "gpt-4",
  "prompts": {
    "system": {
      "hash": "sha256:...",
      "preview": "You are a helpful assistant."
    },
    "user": {
      "hash": "sha256:...",
      "preview": "What is the weather?"
    }
  },
  "final_output": {"result": "..."},
  "run_result": {},
  "tools_used": [
    {
      "name": "weather_api",
      "count": 1,
      "latency_ms": 150
    }
  ],
  "reasoning": [],
  "consents": [],
  "latency_ms": 5000,
  "cost_estimate": null,
  "environment_info": {},
  "policy_summary": {
    "version": "1.0.0",
    "decision": "allow",
    "bundle_hash": "sha256:..."
  },
  "guardrail_summary": {
    "version": "1.0.0",
    "decision": "allow",
    "applied_rules": []
  },
  "errors": [],
  "errors_truncated": 0,
  "integrity": {
    "first_hash": "abc123...",
    "last_hash": "def456...",
    "event_count": 10
  },
  "organization": {
    "org_id": "org_123",
    "org_name": "Acme Corp"
  }
}
```

### Manifest Schema

The `manifest.json` file contains:

```json
{
  "schema_version": "1.3",
  "session_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
  "agent_name": "my-agent",
  "agent_version": "1.0.0",
  "created_at": "2025-01-15T10:30:00Z",
  "artifacts": {
    "evidence.jsonl": {
      "size_bytes": 10240,
      "sha256": "abc123...",
      "line_count": 10
    },
    "output.json": {
      "size_bytes": 2048,
      "sha256": "def456..."
    }
  },
  "integrity": {
    "first_hash": "abc123...",
    "last_hash": "def456...",
    "event_count": 10,
    "chain_valid": true
  },
  "policy": {
    "version": "1.0.0",
    "bundle_hash": "sha256:..."
  },
  "guardrail": {
    "version": "1.0.0",
    "bundle_hash": "sha256:..."
  }
}
```

### Storage Configuration

**Default Directory:** `evidence/` (relative to current working directory)

By default, Setorra stores evidence artifacts on local storage only. The OSS SDK does not upload logs, evidence, prompts, payloads, or summaries anywhere unless you explicitly configure a backend/control-plane integration.

**Environment Variables:**
- `SETORRA_STORAGE_DIR`: Override storage directory path

**Custom Storage:**
```python
from setorra import SetorraCollector
from pathlib import Path

collector = SetorraCollector(
    agent_name="my-agent",
    agent_version="1.0.0",
    storage_dir=Path("/custom/path/evidence")
)
```

---

## Multi-Agent Support

Multi-agent capture is feature-gated and must be enabled explicitly.

### Enable Multi-Agent Capture

**Environment Variable:**
```bash
export SETORRA_MULTIAGENT_CAPTURE=1
```

**Or in code:**
```python
collector = SetorraCollector(
    agent_name="my-agent",
    agent_version="1.0.0",
    capture_multiagent=True
)
```

### Usage

```python
from setorra import setorra_collector, MessageBus

collector = setorra_collector("scheduler-agent", "1.0.0")
session_id = collector.start_session(user_prompt="Schedule meeting")

# Start conversation
conversation_id = collector.start_conversation(
    participants=[
        {"agent_id": "scheduler-agent", "version": "1.0.0", "role": "coordinator"},
        {"agent_id": "calendar-agent", "version": "1.0.0", "role": "executor"}
    ]
)

# Use MessageBus helper
bus = MessageBus(collector)

# Send message
result = bus.send(
    to=["calendar-agent"],
    content={"summary": "Meeting at 2pm"},
    channel="delegate",
    from_agent="scheduler-agent"
)

# In receiving agent process
collector.record_inbound_message(
    from_agent="scheduler-agent",
    content={"summary": "Meeting at 2pm"},
    message_id=result["message_id"]
)
```

### Multi-Agent Events

- `multiagent.topology`: Conversation topology (participants, edges)
- `agent.joined`: Agent joined conversation
- `agent.left`: Agent left conversation
- `agent.message.outbound`: Outbound message attempt
- `agent.message.inbound`: Inbound message receipt
- `agent.handoff.start`: Handoff initiated
- `agent.handoff.complete`: Handoff completed
- `agent.handoff.cancel`: Handoff cancelled

---

## Configuration

### Environment Variables Reference

#### Core Configuration

- `SETORRA_ENVIRONMENT`: Deployment environment label (default: `local`)
- `SETORRA_HOSTNAME`: Override hostname (default: socket.gethostname())
- `SETORRA_STORAGE_DIR`: Storage directory path (default: `evidence`)

#### Policy & Guardrails

- `SETORRA_POLICY_PATH`: Path to policy bundle JSON file
- `SETORRA_GUARDRAIL_PATH`: Path to guardrail bundle (JSON or YAML)
- `SETORRA_POLICY_AUTO_WAIT`: Wait for approvals (default: `true`)

#### Privacy & Redaction

- `SETORRA_REDACTION_LEVEL`: `standard` (default) or `strict`
- `SETORRA_REDACTION_DISABLE`: Comma-separated flags to disable
- `SETORRA_REDACTION_ENABLE`: Comma-separated flags to enable

#### Integrity & Signing

- `SETORRA_SIGNING_KEY`: HMAC signing key (bytes or string)
- `SETORRA_SIGNING_KEY_ID`: Signing key identifier

#### Backend Integration

- `SETORRA_API_KEY`: API key for backend handshake
- `SETORRA_BACKEND`: Backend base URL (e.g., `https://api.setorra.com`)

#### Error Handling

- `SETORRA_ERRORS_AS_OUTPUT`: Return errors as output (`true/1/yes/on`)

#### Feature Flags

- `SETORRA_CAPTURE_PLANS`: Enable planning capture (`true/1/yes/on`)
- `SETORRA_MULTIAGENT_CAPTURE`: Enable multi-agent capture (`true/1/yes/on`)


## Framework Adapters

### SetorraLangChainCallback

LangChain callback handler that forwards events to Setorra.

```python
from setorra import SetorraLangChainCallback

callback = SetorraLangChainCallback(
    collector,
    model_id="gpt-4"
)

# Use with LangChain
chain.invoke(
    inputs,
    config={"callbacks": [callback]}
)
```

**Captured Events:**
- `llm.start`, `llm.end`, `llm.error`
- `tool.start`, `tool.end`, `tool.error`
- `chain.start`, `chain.end`, `chain.error`

### SetorraLangGraphCallback

LangGraph callback handler.

```python
from setorra import SetorraLangGraphCallback

callback = SetorraLangGraphCallback(collector)

app.invoke(
    inputs,
    config={"callbacks": [callback]}
)
```

### SetorraOpenAIAssistants

Wrapper for OpenAI Assistants API.

```python
from setorra import SetorraOpenAIAssistants

wrapper = SetorraOpenAIAssistants(
    collector,
    openai_client=client
)

thread = wrapper.create_thread()
run = wrapper.create_run(assistant_id, thread_id)
```

---

## Error Handling

### Exceptions

#### Policy Errors

- `PolicyError`: Base class for policy-related errors
- `PolicyDeniedError`: Action denied by policy
- `PolicyApprovalTimeout`: Approval request timed out
- `PolicyApprovalCancelled`: Approval request cancelled

#### Guardrail Errors

- `GuardrailError`: Base class for guardrail errors
- `GuardrailViolationError`: Action denied by guardrails

#### Handshake Errors

- `HandshakeError`: Base class for handshake errors
- `InvalidApiKey`: API key not recognized (HTTP 401)
- `RevokedKey`: API key revoked (HTTP 403)
- `TransientNetworkError`: Temporary network error

### Errors-as-Output Mode

When `errors_as_output=True` (Runner default), blocked actions return a standardized payload instead of raising:

```python
result = run.invoke(agent, inputs={...})

if result.output.get("status") == "guardrail_denied":
    print(f"Blocked: {result.output['public_message']}")
elif result.output.get("status") == "policy_denied":
    print(f"Blocked: {result.output['public_message']}")
elif result.output.get("status") == "approval_timeout":
    print(f"Timeout: {result.output['public_message']}")
```

**Return Structure:**
```json
{
  "output": "Blocked by policy.",
  "status": "policy_denied",
  "public_message": "Blocked by policy.",
  "reason_code": "POLICY_DENY"
}
```

---

## Advanced Usage

### Custom Approval Notifier

```python
from setorra import ApprovalNotifier, SetorraCollector

class MyApprovalNotifier(ApprovalNotifier):
    def notify(self, requirement: PolicyApprovalRequirement) -> None:
        # Send notification to approver
        send_email(
            to=requirement.approver_roles,
            subject="Approval Required",
            body=f"Action requires approval: {requirement.intent_hash}"
        )

collector = SetorraCollector(
    agent_name="my-agent",
    agent_version="1.0.0",
    approval_notifier=MyApprovalNotifier()
)
```

### Custom Policy Loader

```python
from setorra import PolicyLoader, PolicyBundle, PolicyDefaults, PolicyAction
from pathlib import Path

class DatabasePolicyLoader(PolicyLoader):
    def get_bundle(self) -> PolicyBundle:
        # Load from database instead of file
        policy_data = load_from_database()
        return self._parse_bundle(policy_data)
```

### Planning Capture

Planning capture is feature-gated:

```bash
export SETORRA_CAPTURE_PLANS=1
```

When enabled, planning events are captured:

```python
collector.capture_plan(
    summary="Execute payment transfer",
    steps=["Validate recipient", "Check balance", "Transfer funds"]
)
```

### Integrity Verification

Verify integrity chain programmatically:

```python
import json
from pathlib import Path
from setorra.integrity import compute_event_hash

def verify_chain(evidence_path: Path) -> bool:
    events = []
    with open(evidence_path) as f:
        for line in f:
            events.append(json.loads(line))
    
    for i, event in enumerate(events):
        # Skip integrity hash when computing
        computed_hash = compute_event_hash(event)
        stored_hash = event.get("integrity", {}).get("event_hash")
        
        if computed_hash != stored_hash:
            return False
        
        # Verify chain link
        if i > 0:
            prior_hash = events[i-1].get("integrity", {}).get("event_hash")
            current_prior = event.get("integrity", {}).get("prior_hash")
            if prior_hash != current_prior:
                return False
    
    return True
```

### Backend Handshake

Link SDK to backend control plane:

This is optional. Without backend configuration, Setorra remains local-first and writes evidence only to local storage.

```python
import os

os.environ["SETORRA_API_KEY"] = "your-api-key"
os.environ["SETORRA_BACKEND"] = "https://api.setorra.com"

# Handshake happens automatically in factory
collector = setorra_collector("my-agent", "1.0.0")

# Events now include organization context
# context.organization.org_id
# context.organization.org_name
```

---

## Best Practices

1. **Initialize Once**: Create one `Runner` or `SetorraCollector` per process/service
2. **Session Lifecycle**: Always call `start_session()` before recording actions and `end_session()` after
3. **Error Handling**: Use `errors_as_output=True` for user-facing apps; use exceptions for internal control flow
4. **Policy Versioning**: Version your policy bundles and track versions in evidence
5. **Redaction**: Never log raw PII/secrets; rely on SDK redaction before persistence
6. **Integrity**: Verify evidence chains periodically to detect tampering
7. **Storage**: Use atomic writes and keep exported evidence artifacts immutable for compliance
8. **Multi-Agent**: Enable multi-agent capture only when needed to reduce overhead
9. **Guardrails First**: Apply guardrails before policy evaluation for faster denials
10. **Testing**: Test policy/guardrail bundles separately before production deployment

---

## Troubleshooting

### Session Already Active

**Error:** `RuntimeError: A session is already active`

**Solution:** Call `end_session()` before starting a new session, or create a new collector instance.

### Policy Bundle Not Found

**Error:** `RuntimeError: SETORRA_POLICY_PATH points to missing file`

**Solution:** Ensure the policy bundle path is correct and the file exists.

### Approval Timeout

**Error:** `PolicyApprovalTimeout`

**Solution:** Increase approval timeout in policy bundle or handle timeout gracefully with `errors_as_output=True`.

### Multi-Agent Events Not Captured

**Issue:** Multi-agent events missing from evidence

**Solution:** Ensure `SETORRA_MULTIAGENT_CAPTURE=1` or `capture_multiagent=True` is set.

---

## Appendix

### Event Type Reference

**Session Lifecycle:**
- `agent.started`
- `session.manifest`
- `prompt.capture`
- `agent.outcome`
- `agent.finished`

**LLM Events:**
- `llm.start`
- `llm.end`
- `llm.error`

**Tool Events:**
- `tool.start`
- `tool.end`
- `tool.error`

**Policy Events:**
- `policy.evaluated`
- `policy.denied`
- `policy.approval_required`
- `policy.approved`
- `policy.timeout`
- `policy.cancelled`

**Guardrail Events:**
- `guardrail.applied`
- `guardrail.modified`
- `guardrail.blocked`

**Multi-Agent Events:**
- `multiagent.topology`
- `agent.joined`
- `agent.left`
- `agent.message.outbound`
- `agent.message.inbound`
- `agent.handoff.start`
- `agent.handoff.complete`
- `agent.handoff.cancel`

**Planning Events:**
- `plan.created`
- `plan.updated`
- `plan.executed`

---

## License

[Your License Here]

---

## Support

For issues, questions, or contributions, please refer to the project repository.
