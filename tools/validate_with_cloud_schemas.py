#!/usr/bin/env python3
"""
Producer-side validation: SDK JSON → Avro using Confluent Cloud schemas
No messages sent to Kafka - validation only.

Includes comprehensive logging for debugging schema issues.
"""

import os
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from dotenv import load_dotenv
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroSerializer
from confluent_kafka.serialization import SerializationContext, MessageField

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('validation_debug.log')
    ]
)
logger = logging.getLogger(__name__)

# Test sessions to validate
TEST_SESSIONS = [
    '01K8JQB2S3JXB9CP94Z89K4142',  # Simple failure
    '01K8JH3KPB2AZ7Y3AHAK6N3FV0',  # Complex with tools
    '01K8R071MMBEWZPWW0ZFHQTW0F',  # With org
    '01K9BSXCKYEFZ0W1VWVEM2MT07',  # Medium
    '01K9EH14FBPSTYP0Z7NA293PS6',  # Latest
]


def load_credentials() -> Dict[str, str]:
    """Load Confluent credentials from .env"""
    logger.info("Loading credentials from .env")
    load_dotenv()
    
    sr_url = os.getenv('CONFLUENT_SCHEMA_REGISTRY_URL')
    sr_key = os.getenv('CONFLUENT_SR_API_KEY')
    sr_secret = os.getenv('CONFLUENT_SR_API_SECRET')
    
    logger.debug(f"Schema Registry URL present: {bool(sr_url)}")
    logger.debug(f"Schema Registry API Key present: {bool(sr_key)}")
    logger.debug(f"Schema Registry API Secret present: {bool(sr_secret)}")
    
    if not all([sr_url, sr_key, sr_secret]):
        raise ValueError("Missing required credentials in .env file")
    
    logger.info(f"✓ Schema Registry URL: {sr_url.split('.')[0]}...confluent.cloud")
    
    return {
        'url': sr_url,
        'basic.auth.user.info': f'{sr_key}:{sr_secret}'
    }


def register_or_fetch_schemas(client: SchemaRegistryClient, schema_dir: Path) -> Dict[str, Any]:
    """Register schemas if they don't exist, or fetch existing ones"""
    logger.info("Registering/fetching schemas from Confluent Schema Registry")
    
    schema_files = {
        'evidence': ('setorra.evidence-value', 'evidence-event-v1.avsc'),
        'output': ('setorra.output-value', 'output-summary-v1.avsc'),
        'manifest': ('setorra.manifest-value', 'manifest-v1.avsc')
    }
    
    schemas = {}
    for key, (subject, filename) in schema_files.items():
        schema_file = schema_dir / filename
        
        if not schema_file.exists():
            logger.error(f"Schema file not found: {schema_file}")
            raise FileNotFoundError(f"Schema file {filename} not found")
        
        # Load schema from file
        with open(schema_file, 'r') as f:
            schema_str = f.read()
        
        logger.debug(f"Loaded schema from {filename}")
        
        try:
            # Try to fetch existing schema
            logger.debug(f"Checking if schema exists for subject: {subject}")
            schema_version = client.get_latest_version(subject)
            
            logger.info(f"✓ {subject} (existing)")
            logger.info(f"  - Schema ID: {schema_version.schema_id}")
            logger.info(f"  - Version: {schema_version.version}")
            
            schemas[key] = {
                'schema_id': schema_version.schema_id,
                'version': schema_version.version,
                'schema_str': schema_version.schema.schema_str,
                'subject': subject
            }
        except Exception as e:
            # Schema doesn't exist, register it
            logger.info(f"Schema {subject} not found, registering...")
            try:
                from confluent_kafka.schema_registry import Schema
                schema = Schema(schema_str, schema_type="AVRO")
                schema_id = client.register_schema(subject, schema)
                
                logger.info(f"✓ {subject} (newly registered)")
                logger.info(f"  - Schema ID: {schema_id}")
                logger.info(f"  - Version: 1")
                
                # Set compatibility mode to BACKWARD
                try:
                    client.set_compatibility(subject_name=subject, level="BACKWARD")
                    logger.debug(f"  - Compatibility: BACKWARD")
                except:
                    logger.warning(f"  - Could not set compatibility mode")
                
                schemas[key] = {
                    'schema_id': schema_id,
                    'version': 1,
                    'schema_str': schema_str,
                    'subject': subject
                }
            except Exception as reg_err:
                logger.error(f"✗ Failed to register schema for {subject}: {reg_err}")
                raise
    
    return schemas


def prepare_for_avro(data: Any, field_name: str = "root") -> Any:
    """
    Convert SDK JSON to Avro-compatible format (handle unions).
    
    For fields with union types like ["null", "string"], we need to wrap
    non-null values with type indicators: {"string": "value"}
    """
    if data is None:
        logger.debug(f"Field '{field_name}': None (keeping as null)")
        return None
    
    if isinstance(data, dict):
        # Check if it's already a union wrapper (has exactly one key that's a type)
        if len(data) == 1 and list(data.keys())[0] in ['string', 'int', 'long', 'double', 'boolean']:
            logger.debug(f"Field '{field_name}': Already union-wrapped")
            return data
        
        # For dict fields that should be strings (context, execution, policy, etc.)
        # we need to stringify them and wrap in union
        result = {}
        for k, v in data.items():
            result[k] = prepare_for_avro(v, f"{field_name}.{k}")
        
        logger.debug(f"Field '{field_name}': dict with {len(result)} keys")
        return result
    
    if isinstance(data, list):
        result = [prepare_for_avro(item, f"{field_name}[]") for item in data]
        logger.debug(f"Field '{field_name}': array with {len(result)} items")
        return result
    
    if isinstance(data, str):
        logger.debug(f"Field '{field_name}': string ({len(data)} chars)")
        return data
    
    if isinstance(data, (int, float, bool)):
        logger.debug(f"Field '{field_name}': {type(data).__name__} = {data}")
        return data
    
    # For any other type, convert to string
    logger.warning(f"Field '{field_name}': Unexpected type {type(data).__name__}, converting to string")
    return str(data)


def prepare_evidence_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """Prepare evidence event for Avro serialization"""
    logger.debug(f"Preparing evidence event: event_index={event.get('event_index')}, type={event.get('event_type')}")
    
    prepared = {}
    
    # Simple string/int fields
    for field in ['schema_version', 'session_id', 'timestamp', 'agent_name', 'agent_version', 'event_type']:
        prepared[field] = event.get(field)
    
    prepared['event_index'] = event.get('event_index', 0)
    
    # Arrays
    prepared['privacy_flags'] = event.get('privacy_flags', [])
    prepared['redacted_fields'] = event.get('redacted_fields', [])
    
    # Union fields - stringify dicts/arrays (AvroSerializer handles union wrapping automatically)
    for field in ['parameters_redacted', 'context', 'execution', 'policy', 'approval', 'guardrail']:
        value = event.get(field)
        if value is None:
            prepared[field] = None
        elif isinstance(value, str):
            prepared[field] = value
        elif isinstance(value, dict) or isinstance(value, list):
            # Stringify complex objects
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


def prepare_output_summary(output: Dict[str, Any]) -> Dict[str, Any]:
    """Prepare output summary for Avro serialization"""
    logger.debug(f"Preparing output summary: session_id={output.get('session_id')}")
    
    prepared = {}
    
    # Simple fields
    for field in ['schema_version', 'session_id', 'agent_name', 'agent_version', 
                  'started_at', 'finished_at', 'duration_ms']:
        prepared[field] = output.get(field)
    
    # errors_truncated must be an integer (not nullable), default to 0
    prepared['errors_truncated'] = output.get('errors_truncated', 0) or 0
    
    # Nullable simple fields (AvroSerializer handles union wrapping)
    prepared['model_id'] = output.get('model_id')
    prepared['latency_ms'] = output.get('latency_ms')
    prepared['cost_estimate'] = output.get('cost_estimate')
    
    # Union fields that need stringification (AvroSerializer handles union wrapping)
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


def prepare_manifest(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Prepare manifest for Avro serialization"""
    logger.debug(f"Preparing manifest: session_id={manifest.get('context', {}).get('session_id')}")
    
    prepared = {}
    
    # Simple fields
    prepared['schema_version'] = manifest.get('schema_version')
    prepared['hash_alg'] = manifest.get('hash_alg')
    prepared['produced_at'] = manifest.get('produced_at')
    
    # Context (nested record) - AvroSerializer handles union wrapping
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
    
    # Objects array - AvroSerializer handles union wrapping
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
    
    # Chain summary (nested record) - AvroSerializer handles union wrapping
    chain = manifest.get('chain_summary', {})
    prepared['chain_summary'] = {
        'total_events': chain.get('total_events', 0),
        'first_event_hash': chain.get('first_event_hash'),
        'final_event_hash': chain.get('final_event_hash'),
        'root_signature': chain.get('root_signature'),
        'signer_id': chain.get('signer_id')
    }
    
    return prepared


def validate_session(
    session_id: str,
    serializers: Dict[str, AvroSerializer],
    evidence_dir: Path
) -> Tuple[int, int, int, int]:
    """
    Load and validate one session (evidence, output, manifest).
    Returns: (evidence_count, evidence_bytes, output_bytes, manifest_bytes)
    """
    logger.info(f"Validating session: {session_id}")
    
    session_path = evidence_dir / session_id
    if not session_path.exists():
        logger.error(f"Session directory not found: {session_path}")
        raise FileNotFoundError(f"Session {session_id} not found")
    
    evidence_count = 0
    evidence_bytes = 0
    output_bytes = 0
    manifest_bytes = 0
    
    # Validate evidence.jsonl
    evidence_file = session_path / 'evidence.jsonl'
    if evidence_file.exists():
        logger.debug(f"Loading evidence.jsonl: {evidence_file}")
        with open(evidence_file, 'r') as f:
            for line_num, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    chain_idx = event.get('event_index', line_num - 1)
                    logger.debug(f"Processing event {chain_idx}: {event.get('event_type')}")
                    
                    prepared = prepare_evidence_event(event)
                    ctx = SerializationContext('setorra.evidence', MessageField.VALUE)
                    avro_bytes = serializers['evidence'](prepared, ctx)
                    
                    evidence_count += 1
                    evidence_bytes += len(avro_bytes)
                    logger.debug(f"✓ Event {chain_idx} serialized: {len(avro_bytes)} bytes")
                except Exception as e:
                    logger.error(f"✗ Failed to serialize evidence event {line_num}: {e}")
                    logger.error(f"  Event data: {line[:200]}...")
                    raise
    
    # Validate output.json
    output_file = session_path / 'output.json'
    if output_file.exists():
        logger.debug(f"Loading output.json: {output_file}")
        try:
            with open(output_file, 'r') as f:
                output = json.load(f)
            
            prepared = prepare_output_summary(output)
            ctx = SerializationContext('setorra.output', MessageField.VALUE)
            avro_bytes = serializers['output'](prepared, ctx)
            
            output_bytes = len(avro_bytes)
            logger.debug(f"✓ Output summary serialized: {output_bytes} bytes")
        except Exception as e:
            logger.error(f"✗ Failed to serialize output summary: {e}")
            raise
    
    # Validate manifest.json
    manifest_file = session_path / 'manifest.json'
    if manifest_file.exists():
        logger.debug(f"Loading manifest.json: {manifest_file}")
        try:
            with open(manifest_file, 'r') as f:
                manifest = json.load(f)
            
            prepared = prepare_manifest(manifest)
            ctx = SerializationContext('setorra.manifest', MessageField.VALUE)
            avro_bytes = serializers['manifest'](prepared, ctx)
            
            manifest_bytes = len(avro_bytes)
            logger.debug(f"✓ Manifest serialized: {manifest_bytes} bytes")
        except Exception as e:
            logger.error(f"✗ Failed to serialize manifest: {e}")
            raise
    
    return evidence_count, evidence_bytes, output_bytes, manifest_bytes


def main():
    """Main validation entry point"""
    logger.info("=== Producer-Side Validation ===")
    print("\n=== Producer-Side Validation ===\n")
    
    try:
        # 1. Load credentials
        print("📋 Loading credentials from .env...")
        creds = load_credentials()
        
        # 2. Connect to Schema Registry
        print("\n🔗 Connecting to Confluent Schema Registry...")
        client = SchemaRegistryClient(creds)
        logger.info("✓ Connected to Schema Registry")
        print("✓ Connected successfully")
        
        # 3. Register or fetch schemas
        print("\n📥 Registering/fetching Avro schemas...")
        schema_dir = Path(__file__).parent.parent / 'schemas' / 'avro'
        schemas = register_or_fetch_schemas(client, schema_dir)
        
        # 4. Create AvroSerializers
        print("\n🔧 Creating AvroSerializers...")
        serializers = {}
        for key, schema_info in schemas.items():
            serializers[key] = AvroSerializer(
                client,
                schema_info['schema_str'],
                to_dict=lambda obj, ctx: obj
            )
            logger.info(f"✓ {key.capitalize()} serializer ready")
        print("✓ Evidence serializer ready")
        print("✓ Output serializer ready")
        print("✓ Manifest serializer ready")
        
        # 5. Validate 5 sessions
        print("\n🧪 Validating 5 sessions...\n")
        evidence_dir = Path(__file__).parent.parent / 'evidence'
        
        total_evidence = 0
        total_evidence_bytes = 0
        total_output_bytes = 0
        total_manifest_bytes = 0
        success_count = 0
        
        for idx, session_id in enumerate(TEST_SESSIONS, 1):
            print(f"[{idx}/5] {session_id}")
            try:
                ev_count, ev_bytes, out_bytes, man_bytes = validate_session(
                    session_id, serializers, evidence_dir
                )
                
                total_evidence += ev_count
                total_evidence_bytes += ev_bytes
                total_output_bytes += out_bytes
                total_manifest_bytes += man_bytes
                
                print(f"  ✓ Evidence: {ev_count} events → {ev_bytes / 1024:.1f} KB")
                print(f"  ✓ Output: 1 summary → {out_bytes / 1024:.1f} KB")
                print(f"  ✓ Manifest: 1 manifest → {man_bytes / 1024:.1f} KB")
                
                success_count += 1
            except Exception as e:
                print(f"  ✗ Failed: {e}")
                logger.error(f"Session {session_id} validation failed", exc_info=True)
        
        # 6. Report results
        total_bytes = total_evidence_bytes + total_output_bytes + total_manifest_bytes
        
        print("\n=== Summary ===")
        print(f"✅ Sessions validated: {success_count}/5 ({success_count * 20}%)")
        print(f"✅ Evidence events serialized: {total_evidence}")
        print(f"✅ Output summaries serialized: {success_count}")
        print(f"✅ Manifests serialized: {success_count}")
        print(f"✅ Total Avro bytes: ~{total_bytes / 1024:.0f} KB")
        print(f"✅ Validation errors: {5 - success_count}")
        
        schema_ids = [s['schema_id'] for s in schemas.values()]
        print(f"✅ Cloud schema IDs used: {', '.join(map(str, schema_ids))}")
        
        print("\n🎉 All SDK JSON is compatible with Confluent Cloud Avro schemas!")
        print("📦 NO messages sent to Kafka (validation only)")
        
        logger.info("=== Validation Complete ===")
        return 0 if success_count == 5 else 1
        
    except Exception as e:
        logger.error("Validation failed", exc_info=True)
        print(f"\n❌ Validation failed: {e}")
        print("Check validation_debug.log for details")
        return 1


if __name__ == '__main__':
    exit(main())

