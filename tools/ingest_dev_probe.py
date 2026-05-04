#!/usr/bin/env python3
"""Dev probe: handshake + ingest validation against local backend.

What it does
- Uses the SDK handshake via setorra_collector to obtain a session token.
- Generates a fresh SDK run to create REAL production artifacts:
  evidence/<session_id>/{evidence.jsonl, output.json, manifest.json}.
- Verifies integrity chain locally using SDK's compute_event_hash().
- Validates against Confluent Cloud Avro schemas (optional).
- Posts all three artifacts to the backend in order (evidence → output → manifest).
- Provides diagnostic logging to identify SDK vs backend issues.

Usage examples
  python3 tools/ingest_dev_probe.py
  python3 tools/ingest_dev_probe.py --session 01K8...
  python3 tools/ingest_dev_probe.py --base-url http://127.0.0.1:5001

Env expectations (same as sample agents)
- SETORRA_BACKEND (e.g., http://127.0.0.1:5001)
- SETORRA_API_KEY (for handshake)
- SETORRA_ENVIRONMENT (defaults to 'local')

Notes
- ONLY uses real SDK-generated artifacts (no synthetic mode).
- Minimal and development-only. No retries/backoff; never prints secrets.
- Uses stdlib urllib; optional confluent-kafka for schema validation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Schema validation imports (optional - only if credentials available)
try:
    from confluent_kafka.schema_registry import SchemaRegistryClient
    from confluent_kafka.schema_registry.avro import AvroSerializer
    from confluent_kafka.serialization import SerializationContext, MessageField
    SCHEMA_VALIDATION_AVAILABLE = True
except ImportError:
    SCHEMA_VALIDATION_AVAILABLE = False

# Ensure repo root is importable when running from tools/
try:
    _THIS = Path(__file__).resolve()
    _ROOT = _THIS.parents[1]
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
except Exception:
    pass

# Load project .env for local probes (best-effort)
try:
    from dotenv import load_dotenv  # type: ignore
    # Prefer project root .env for consistent local behavior.
    _DOTENV = (_ROOT / ".env") if '_ROOT' in globals() else None
    if _DOTENV and _DOTENV.exists():
        load_dotenv(dotenv_path=str(_DOTENV))
    else:
        load_dotenv()
except Exception:
    pass


def _print(msg: str) -> None:
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def _detect_base_url(cli_base: Optional[str]) -> Optional[str]:
    if cli_base:
        return cli_base.rstrip("/")
    env_base = (os.getenv("SETORRA_BACKEND", "") or os.getenv("SETORRA_CONTROL_URL", "")).strip()
    if env_base:
        return env_base.rstrip("/")
    # Prefer IPv4 loopback for local backend probes.
    if (os.getenv("SETORRA_ENVIRONMENT", "local").strip().lower() == "local"):
        return "http://127.0.0.1:5001"
    return None


def _handshake_via_collector(agent_name: str, agent_version: str) -> Tuple[Optional[str], Dict[str, str]]:
    """Instantiate the SDK collector to trigger the same handshake path.

    Returns (session_token, organization_info_minimal).
    """
    try:
        from setorra import setorra_collector  # type: ignore
    except Exception as exc:  # pragma: no cover
        _print(f"[probe] error importing SDK collector: {exc}")
        return None, {}

    collector = setorra_collector(agent_name, agent_version)
    # Read the SDK handshake fields.
    token = getattr(collector, "_remote_session_token", None)
    org_info = getattr(collector, "_organization_info", {}) or {}
    minimal = {}
    for k in ("org_id", "org_name", "key_id_prefix"):
        if k in org_info and org_info[k]:
            minimal[k] = org_info[k]
    return token, minimal


def _start_and_finish_run(agent_name: str, agent_version: str) -> Tuple[str, Path]:
    """Generate a minimal run so artifacts exist locally."""
    from setorra import setorra_collector  # type: ignore

    coll = setorra_collector(agent_name, agent_version)
    session_id = coll.start_session(system_prompt="dev-probe", user_prompt="handshake+ingest test")
    # Emit a minimal successful outcome and finalize
    coll.end_session(final_output={"ok": True}, model_id="dev-probe")
    run_dir = Path("evidence") / session_id
    return session_id, run_dir


def _load_bytes(path: Path) -> bytes:
    with path.open("rb") as f:
        return f.read()


def _verify_local_integrity(evidence_bytes: bytes) -> Dict[str, Any]:
    """Verify integrity chain locally using SDK's compute_event_hash().
    
    Returns:
        {
            'total_events': int,
            'valid_hashes': int,
            'mismatches': [{'event_index': int, 'event_type': str, 'computed': str, 'stored': str}],
            'chain_valid': bool,
            'sequence_valid': bool
        }
    """
    from setorra.integrity import compute_event_hash  # type: ignore
    
    result = {
        'total_events': 0,
        'valid_hashes': 0,
        'mismatches': [],
        'chain_valid': True,
        'sequence_valid': False
    }
    
    try:
        lines = evidence_bytes.decode('utf-8').strip().split('\n')
        events = []
        
        for line in lines:
            if not line.strip():
                continue
            event = json.loads(line)
            events.append(event)
        
        result['total_events'] = len(events)
        
        # Check sequence: first=agent.started, last=agent.finished
        if events:
            first_type = events[0].get('event_type', '')
            last_type = events[-1].get('event_type', '')
            result['sequence_valid'] = (first_type == 'agent.started' and last_type == 'agent.finished')
        
        # Verify hashes and chain continuity
        prev_hash = None
        for i, event in enumerate(events):
            event_index = event.get('event_index', i)
            event_type = event.get('event_type', 'unknown')
            
            # Verify hash
            stored_hash = event.get('integrity', {}).get('event_hash', '')
            computed_hash = compute_event_hash(event)
            
            if stored_hash == computed_hash:
                result['valid_hashes'] += 1
            else:
                result['mismatches'].append({
                    'event_index': event_index,
                    'event_type': event_type,
                    'computed': computed_hash,
                    'stored': stored_hash
                })
            
            # Verify chain continuity
            expected_prev = event.get('integrity', {}).get('prev_hash')
            if i == 0:
                # First event should have null prev_hash
                if expected_prev is not None:
                    result['chain_valid'] = False
            else:
                # Subsequent events should link to previous event_hash
                if expected_prev != prev_hash:
                    result['chain_valid'] = False
            
            prev_hash = stored_hash
        
    except Exception as e:
        _print(f"[integrity] Error verifying integrity: {e}")
        result['chain_valid'] = False
    
    return result


def _inspect_evidence_structure(evidence_bytes: bytes) -> str:
    """Parse evidence and return summary string."""
    try:
        lines = evidence_bytes.decode('utf-8').strip().split('\n')
        events = [json.loads(line) for line in lines if line.strip()]
        
        if not events:
            return "No events found"
        
        total = len(events)
        first_type = events[0].get('event_type', 'unknown')
        last_type = events[-1].get('event_type', 'unknown')
        
        return f"{total} events, sequence: {first_type} → ... → {last_type}"
    except Exception as e:
        return f"Error parsing evidence: {e}"


def _post(base_url: str, token: str, org_id: Optional[str], agent_label: str, idempotency_key: str, *, content_type: str, body: bytes, endpoint: str, timeout_s: float = 3.0) -> Tuple[int, float, int, Optional[str]]:
    url = f"{base_url}{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": content_type,
        "X-Setorra-Agent": agent_label,
        # Idempotency-Key must be the bare session_id (no suffixes). Manifest uses context.session_id.
        "Idempotency-Key": idempotency_key,
        "Accept": "application/json",
        "User-Agent": "SetorraSDK-DevProbe/1",
    }
    if org_id:
        headers["X-Setorra-Org"] = org_id
    req = urllib.request.Request(url=url, headers=headers, data=body, method="POST")
    t0 = time.perf_counter()
    code = 0
    error_msg = None
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # nosec - relies on system CA
            # Drain minimally
            try:
                _ = resp.read(0)
            except Exception:
                pass
            code = int(getattr(resp, "status", 200))
    except urllib.error.HTTPError as e:
        code = int(getattr(e, "code", 0) or 0)
        try:
            error_body = e.read().decode('utf-8')
            error_data = json.loads(error_body)
            error_msg = error_data.get('error', '') + ': ' + error_data.get('detail', error_body[:100])
        except Exception:
            error_msg = None
    except Exception:
        code = 0
    ms = (time.perf_counter() - t0) * 1000.0
    return code, ms, len(body), error_msg


def _load_schema_credentials() -> Optional[Dict[str, str]]:
    """Load Confluent Schema Registry credentials from .env"""
    sr_url = os.getenv('CONFLUENT_SCHEMA_REGISTRY_URL')
    sr_key = os.getenv('CONFLUENT_SR_API_KEY')
    sr_secret = os.getenv('CONFLUENT_SR_API_SECRET')
    
    if not all([sr_url, sr_key, sr_secret]):
        return None
    
    return {
        'url': sr_url,
        'basic.auth.user.info': f'{sr_key}:{sr_secret}'
    }


def _fetch_schemas(client: Any, schema_dir: Path) -> Dict[str, Any]:
    """Fetch schemas from Confluent Schema Registry"""
    schema_files = {
        'evidence': ('setorra.evidence-value', 'evidence-event-v1.avsc'),
        'output': ('setorra.output-value', 'output-summary-v1.avsc'),
        'manifest': ('setorra.manifest-value', 'manifest-v1.avsc')
    }
    
    schemas = {}
    for key, (subject, filename) in schema_files.items():
        schema_file = schema_dir / filename
        if not schema_file.exists():
            raise FileNotFoundError(f"Schema file {filename} not found")
        
        try:
            schema_version = client.get_latest_version(subject)
            schemas[key] = {
                'schema_id': schema_version.schema_id,
                'version': schema_version.version,
                'schema_str': schema_version.schema.schema_str,
                'subject': subject
            }
        except Exception as e:
            raise RuntimeError(f"Failed to fetch schema {subject}: {e}")
    
    return schemas


def _prepare_evidence_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """Prepare evidence event for Avro serialization"""
    prepared = {}
    
    # Simple string/int fields
    for field in ['schema_version', 'session_id', 'timestamp', 'agent_name', 'agent_version', 'event_type']:
        prepared[field] = event.get(field)
    
    prepared['event_index'] = event.get('event_index', 0)
    
    # Arrays
    prepared['privacy_flags'] = event.get('privacy_flags', [])
    prepared['redacted_fields'] = event.get('redacted_fields', [])
    
    # Union fields - stringify dicts/arrays
    for field in ['parameters_redacted', 'context', 'execution', 'policy', 'approval', 'guardrail']:
        value = event.get(field)
        if value is None:
            prepared[field] = None
        elif isinstance(value, str):
            prepared[field] = value
        elif isinstance(value, dict) or isinstance(value, list):
            prepared[field] = json.dumps(value, separators=(',', ':'))
        else:
            prepared[field] = str(value)
    
    # Integrity (nested record)
    integrity = event.get('integrity', {})
    prepared['integrity'] = {
        'prev_hash': integrity.get('prev_hash'),
        'event_hash': integrity.get('event_hash', ''),
        'signature': integrity.get('signature')
    }
    
    return prepared


def _prepare_output_summary(output: Dict[str, Any]) -> Dict[str, Any]:
    """Prepare output summary for Avro serialization"""
    prepared = {}
    
    # Simple fields
    for field in ['schema_version', 'session_id', 'agent_name', 'agent_version', 
                  'started_at', 'finished_at', 'duration_ms']:
        prepared[field] = output.get(field)
    
    # errors_truncated must be an integer (not nullable), default to 0
    prepared['errors_truncated'] = output.get('errors_truncated', 0) or 0
    
    # Nullable simple fields
    prepared['model_id'] = output.get('model_id')
    prepared['latency_ms'] = output.get('latency_ms')
    prepared['cost_estimate'] = output.get('cost_estimate')
    
    # Union fields that need stringification
    for field in ['prompts', 'final_output', 'run_result', 'tools_used', 'reasoning', 
                  'consents', 'environment_info', 'policy_summary', 'guardrail_summary', 
                  'errors', 'integrity', 'organization']:
        value = output.get(field)
        if value is None or (isinstance(value, dict) and not value):
            prepared[field] = None
        elif isinstance(value, str):
            prepared[field] = value
        else:
            prepared[field] = json.dumps(value, separators=(',', ':'))
    
    return prepared


def _prepare_manifest(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Prepare manifest for Avro serialization"""
    prepared = {}
    
    # Simple fields
    prepared['schema_version'] = manifest.get('schema_version')
    prepared['hash_alg'] = manifest.get('hash_alg')
    prepared['produced_at'] = manifest.get('produced_at')
    
    # Context (nested record)
    ctx = manifest.get('context', {})
    prepared['context'] = {
        'session_id': ctx.get('session_id', ''),
        'env': ctx.get('env', ''),
        'started_at': ctx.get('started_at', ''),
        'finished_at': ctx.get('finished_at', ''),
        'toolchain_version': ctx.get('toolchain_version', ''),
        'evidence_schema_version': ctx.get('evidence_schema_version', ''),
        'org_id': ctx.get('org_id'),
        'org_name': ctx.get('org_name')
    }
    
    # Objects array
    objects = manifest.get('objects', [])
    prepared['objects'] = []
    for obj in objects:
        prepared['objects'].append({
            'name': obj.get('name', ''),
            'bytes': obj.get('bytes', 0),
            'sha256': obj.get('sha256', ''),
            'content_type': obj.get('content_type', ''),
            'lines': obj.get('lines')
        })
    
    # Chain summary (nested record)
    chain = manifest.get('chain_summary', {})
    prepared['chain_summary'] = {
        'total_events': chain.get('total_events', 0),
        'first_event_hash': chain.get('first_event_hash'),
        'final_event_hash': chain.get('final_event_hash'),
        'root_signature': chain.get('root_signature'),
        'signer_id': chain.get('signer_id')
    }
    
    return prepared


def _validate_payloads_against_cloud_schemas(
    evidence_bytes: bytes,
    output_bytes: bytes,
    manifest_bytes: bytes,
    verbose: bool = False
) -> Tuple[bool, Dict[str, Any]]:
    """Validate payloads against Confluent Cloud Avro schemas.
    
    Returns (success, results_dict) where results_dict contains:
    - schemas: dict with schema_id, version, subject for each
    - evidence_count: number of events validated
    - evidence_bytes: total bytes serialized
    - output_bytes: bytes serialized
    - manifest_bytes: bytes serialized
    - errors: list of error messages
    """
    if not SCHEMA_VALIDATION_AVAILABLE:
        return False, {'error': 'Schema validation libraries not available (confluent-kafka[avro] not installed)'}
    
    creds = _load_schema_credentials()
    if not creds:
        return False, {'error': 'Schema Registry credentials not found in .env'}
    
    errors = []
    results = {
        'schemas': {},
        'evidence_count': 0,
        'evidence_bytes': 0,
        'output_bytes': 0,
        'manifest_bytes': 0,
        'errors': errors
    }
    
    try:
        # Connect to Schema Registry
        if verbose:
            _print("[validate] Connecting to Confluent Schema Registry...")
        client = SchemaRegistryClient(creds)
        
        # Fetch schemas
        schema_dir = Path(__file__).parent.parent / 'schemas' / 'avro'
        schemas = _fetch_schemas(client, schema_dir)
        
        for key, schema_info in schemas.items():
            results['schemas'][key] = {
                'schema_id': schema_info['schema_id'],
                'version': schema_info['version'],
                'subject': schema_info['subject']
            }
            if verbose:
                _print(f"[validate] {schema_info['subject']}: schema_id={schema_info['schema_id']} version={schema_info['version']}")
        
        # Create serializers
        serializers = {}
        for key, schema_info in schemas.items():
            serializers[key] = AvroSerializer(
                client,
                schema_info['schema_str'],
                to_dict=lambda obj, ctx: obj
            )
        
        # Validate evidence events
        if verbose:
            _print("[validate] Validating evidence events...")
        evidence_lines = evidence_bytes.decode('utf-8').strip().split('\n')
        evidence_errors = 0
        for line_num, line in enumerate(evidence_lines, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                prepared = _prepare_evidence_event(event)
                ctx = SerializationContext('setorra.evidence', MessageField.VALUE)
                avro_bytes = serializers['evidence'](prepared, ctx)
                results['evidence_count'] += 1
                results['evidence_bytes'] += len(avro_bytes)
            except Exception as e:
                error_msg = f"Evidence event {line_num}: {e}"
                errors.append(error_msg)
                evidence_errors += 1
                if verbose:
                    _print(f"[validate] ✗ {error_msg}")
        
        if evidence_errors == 0 and results['evidence_count'] > 0:
            _print(f"[validate] ✓ Evidence schema validation complete ({results['evidence_count']} events)")
        
        # Validate output summary
        if verbose:
            _print("[validate] Validating output summary...")
        output_validated = False
        try:
            output_data = json.loads(output_bytes.decode('utf-8'))
            prepared = _prepare_output_summary(output_data)
            ctx = SerializationContext('setorra.output', MessageField.VALUE)
            avro_bytes = serializers['output'](prepared, ctx)
            results['output_bytes'] = len(avro_bytes)
            output_validated = True
        except Exception as e:
            error_msg = f"Output summary: {e}"
            errors.append(error_msg)
            if verbose:
                _print(f"[validate] ✗ {error_msg}")
        
        if output_validated:
            _print("[validate] ✓ Output schema validation complete")
        
        # Validate manifest
        if verbose:
            _print("[validate] Validating manifest...")
        manifest_validated = False
        try:
            manifest_data = json.loads(manifest_bytes.decode('utf-8'))
            prepared = _prepare_manifest(manifest_data)
            ctx = SerializationContext('setorra.manifest', MessageField.VALUE)
            avro_bytes = serializers['manifest'](prepared, ctx)
            results['manifest_bytes'] = len(avro_bytes)
            manifest_validated = True
        except Exception as e:
            error_msg = f"Manifest: {e}"
            errors.append(error_msg)
            if verbose:
                _print(f"[validate] ✗ {error_msg}")
        
        if manifest_validated:
            _print("[validate] ✓ Manifest schema validation complete")
        
        success = len(errors) == 0
        return success, results
        
    except Exception as e:
        errors.append(f"Validation failed: {e}")
        results['errors'] = errors
        return False, results


def main() -> int:
    ap = argparse.ArgumentParser(description="Setorra dev ingest probe (handshake + ingest + schema validation)")
    ap.add_argument("--session", help="Reuse existing session id (skips new run)")
    ap.add_argument("--base-url", help="Backend base URL (default from SETORRA_BACKEND or http://127.0.0.1:5001 in local)")
    ap.add_argument("--agent-name", default="dev-probe", help="Agent name label for handshake/logging")
    ap.add_argument("--agent-version", default="0.1.0", help="Agent version label for handshake/logging")
    ap.add_argument("--timeout-ms", type=int, default=3000, help="HTTP timeout per request")
    ap.add_argument("--skip-validation", action="store_true", help="Skip schema validation against Confluent Cloud (default: always validate)")
    ap.add_argument("--verbose-validation", action="store_true", help="Show detailed validation output")
    args = ap.parse_args()

    base_url = _detect_base_url(args.base_url)
    if not base_url:
        _print("[probe] SETORRA_BACKEND not set and no default for non-local environment; aborting")
        return 2

    # Handshake via the SDK collector factory path.
    _print("[handshake] Connecting to backend...")
    token, org = _handshake_via_collector(args.agent_name, args.agent_version)
    if not token:
        _print("[handshake] ✗ no org link (offline or missing env)")
        return 3
    org_id = org.get("org_id")
    org_name = org.get("org_name")
    _print(f"[handshake] ✓ org_id={org_id or 'n/a'} org_name={org_name or 'n/a'} token_present=1")

    # Load real SDK artifacts (always)
    _print("[prepare] Loading payloads...")
    session_id: Optional[str] = args.session
    run_dir: Optional[Path] = None
    
    if session_id:
        run_dir = Path("evidence") / session_id
        if not run_dir.exists():
            _print(f"[prepare] ✗ provided session not found: {run_dir}")
            return 4
        _print(f"[prepare] Loading artifacts from {run_dir}")
    else:
        _print("[prepare] Generating new SDK run...")
        session_id, run_dir = _start_and_finish_run(args.agent_name, args.agent_version)
        _print(f"[prepare] ✓ session={session_id} dir={run_dir}")

    assert session_id and run_dir
    ev_p = run_dir / "evidence.jsonl"
    out_p = run_dir / "output.json"
    man_p = run_dir / "manifest.json"
    for p in (ev_p, out_p, man_p):
        if not p.exists():
            _print(f"[prepare] ✗ missing artifact: {p}")
            return 5
    ndjson_b = _load_bytes(ev_p)
    out_b = _load_bytes(out_p)
    man_b = _load_bytes(man_p)
    _print(f"[prepare] ✓ Loaded evidence={len(ndjson_b)} bytes output={len(out_b)} bytes manifest={len(man_b)} bytes")
    
    # Inspect evidence structure
    _print("[inspect] Analyzing SDK artifacts...")
    inspection = _inspect_evidence_structure(ndjson_b)
    _print(f"[inspect] Evidence: {inspection}")
    
    # Verify local integrity using SDK's compute_event_hash()
    _print("[integrity] Verifying hashes locally using SDK's compute_event_hash()...")
    integrity_check = _verify_local_integrity(ndjson_b)
    
    if integrity_check['mismatches']:
        _print(f"[integrity] ✗ Found {len(integrity_check['mismatches'])} hash mismatch(es):")
        for m in integrity_check['mismatches']:
            _print(f"[integrity]   Event {m['event_index']} ({m['event_type']}): stored={m['stored'][:16]}... computed={m['computed'][:16]}...")
    else:
        _print(f"[integrity] ✓ All {integrity_check['total_events']} event hashes valid")
    
    _print(f"[integrity] Chain continuity: {'✓' if integrity_check['chain_valid'] else '✗'}")
    _print(f"[integrity] Sequence (first=agent.started, last=agent.finished): {'✓' if integrity_check['sequence_valid'] else '✗'}")
    
    # Validate against Confluent Cloud schemas (unless skipped)
    validation_success = True
    validation_results = {}
    if not args.skip_validation:
        _print("[validate] Validating payloads against Confluent Cloud Avro schemas...")
        validation_success, validation_results = _validate_payloads_against_cloud_schemas(
            ndjson_b, out_b, man_b, verbose=args.verbose_validation
        )
        
        if validation_success:
            schemas = validation_results.get('schemas', {})
            ev_count = validation_results.get('evidence_count', 0)
            ev_bytes = validation_results.get('evidence_bytes', 0)
            out_bytes = validation_results.get('output_bytes', 0)
            man_bytes = validation_results.get('manifest_bytes', 0)
            
            _print(f"[validate] ✓ Evidence: {ev_count} events → {ev_bytes / 1024:.1f} KB")
            _print(f"[validate] ✓ Output: 1 summary → {out_bytes / 1024:.1f} KB")
            _print(f"[validate] ✓ Manifest: 1 manifest → {man_bytes / 1024:.1f} KB")
            
            for key, schema_info in schemas.items():
                _print(f"[validate]   {schema_info['subject']}: schema_id={schema_info['schema_id']} version={schema_info['version']}")
        else:
            error_msg = validation_results.get('error', 'Unknown validation error')
            errors = validation_results.get('errors', [])
            _print(f"[validate] ✗ Validation failed: {error_msg}")
            for err in errors:
                _print(f"[validate]   ✗ {err}")
            _print("[validate] Continuing to send to backend (to test backend error handling)...")
    else:
        _print("[validate] Skipped (--skip-validation)")

    # Post in order
    _print("[post] Sending artifacts to backend...")
    timeout_s = max(0.5, args.timeout_ms / 1000.0)
    agent_label = f"{args.agent_name}/{args.agent_version}"

    code_e, ms_e, bytes_e, err_e = _post(
        base_url,
        token,
        org_id,
        agent_label,
        session_id,
        content_type="application/x-ndjson",
        body=ndjson_b,
        endpoint="/v1/ingest/evidence",
        timeout_s=timeout_s,
    )
    status_e = "✓" if 200 <= code_e < 300 else "✗"
    _print(f"[post] evidence {status_e} status={code_e} ms={ms_e:.2f} bytes={bytes_e}")
    if 200 <= code_e < 300:
        _print(f"[post] ✓ Evidence successfully transferred to backend (session={session_id}, endpoint=/v1/ingest/evidence)")
    elif err_e:
        _print(f"[post]   Backend error: {err_e}")
    
    code_o, ms_o, bytes_o, err_o = _post(
        base_url,
        token,
        org_id,
        agent_label,
        session_id,
        content_type="application/json",
        body=out_b,
        endpoint="/v1/ingest/output",
        timeout_s=timeout_s,
    )
    status_o = "✓" if 200 <= code_o < 300 else "✗"
    _print(f"[post] output   {status_o} status={code_o} ms={ms_o:.2f} bytes={bytes_o}")
    if 200 <= code_o < 300:
        _print(f"[post] ✓ Output successfully transferred to backend (session={session_id}, endpoint=/v1/ingest/output)")
    elif err_o:
        _print(f"[post]   Backend error: {err_o}")
    
    code_m, ms_m, bytes_m, err_m = _post(
        base_url,
        token,
        org_id,
        agent_label,
        session_id,
        content_type="application/json",
        body=man_b,
        endpoint="/v1/ingest/manifest",
        timeout_s=timeout_s,
    )
    status_m = "✓" if 200 <= code_m < 300 else "✗"
    _print(f"[post] manifest {status_m} status={code_m} ms={ms_m:.2f} bytes={bytes_m}")
    if 200 <= code_m < 300:
        _print(f"[post] ✓ Manifest successfully transferred to backend (session={session_id}, endpoint=/v1/ingest/manifest)")
    elif err_m:
        _print(f"[post]   Backend error: {err_m}")

    ingest_ok = (200 <= code_e < 300) and (200 <= code_o < 300) and (200 <= code_m < 300)
    
    # Diagnostic analysis
    _print("\n[diagnosis] === Evidence Failure Analysis ===")
    if code_e >= 400:
        _print(f"[diagnosis] Backend rejected evidence with status={code_e}")
        if err_e and 'event_hash_mismatch' in err_e:
            _print(f"[diagnosis] Backend error indicates: event_hash_mismatch")
            if integrity_check['mismatches']:
                _print(f"[diagnosis] ⚠️  LOCAL CHECK ALSO FAILED")
                _print(f"[diagnosis] The SDK is generating incorrect event_hash values")
                _print(f"[diagnosis] This is an SDK CORE ISSUE (affects production)")
                _print(f"[diagnosis] Next step: Fix setorra/integrity.py or setorra/collector.py")
            else:
                _print(f"[diagnosis] ✓ LOCAL CHECK PASSED")
                _print(f"[diagnosis] SDK generates correct hashes, backend verification has an issue")
        elif integrity_check['mismatches']:
            _print(f"[diagnosis] ⚠️  Local integrity check failed but backend error is different")
            _print(f"[diagnosis] This suggests SDK CORE ISSUE")
    elif not integrity_check['mismatches']:
        _print(f"[diagnosis] ✓ SUCCESS - All integrity checks passed, backend accepted data")
    
    # Final summary
    _print("\n[summary] === Ingest Probe Summary ===")
    _print(f"[summary] Session ID: {session_id}")
    _print(f"[summary] Handshake: ✓ org_id={org_id or 'n/a'}")
    
    if not args.skip_validation:
        if validation_success:
            schemas = validation_results.get('schemas', {})
            schema_ids = [s['schema_id'] for s in schemas.values()]
            _print(f"[summary] Validation: ✓ schemas={', '.join(map(str, schema_ids))}")
        else:
            _print(f"[summary] Validation: ✗ {len(validation_results.get('errors', []))} error(s)")
    
    _print(f"[summary] Integrity: {'✓ valid' if not integrity_check['mismatches'] else '✗ ' + str(len(integrity_check['mismatches'])) + ' mismatch(es)'}")
    _print(f"[summary] Ingest: evidence={code_e} output={code_o} manifest={code_m}")
    _print(f"[summary] Latency: evidence={ms_e:.1f}ms output={ms_o:.1f}ms manifest={ms_m:.1f}ms")
    _print(f"[summary] Overall: {'✓ SUCCESS' if ingest_ok else '✗ FAILED'}")
    
    return 0 if ingest_ok else 6


if __name__ == "__main__":
    raise SystemExit(main())
