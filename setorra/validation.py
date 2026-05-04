import os
import json
import logging
from typing import Any, Dict, Optional, Tuple

# Configure logging
logger = logging.getLogger("setorra.validation")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

VALIDATION_VERBOSE = os.getenv("SETORRA_VALIDATION_VERBOSE", "").lower() in ("1", "true", "yes", "debug")
VALIDATION_ENABLED = os.getenv("SETORRA_VALIDATE_PAYLOADS", "").lower() in ("1", "true", "yes", "on")

try:
    from confluent_kafka.schema_registry import SchemaRegistryClient
    import fastavro
    from fastavro.validation import validate
    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False
    logger.warning("Validation dependencies (confluent-kafka, fastavro) not found. Validation will be skipped.")

def get_schema_client() -> Optional[Any]:
    if not HAS_DEPS:
        return None
        
    url = os.getenv('CONFLUENT_SCHEMA_REGISTRY_URL')
    api_key = os.getenv('CONFLUENT_SR_API_KEY')
    api_secret = os.getenv('CONFLUENT_SR_API_SECRET')
    
    if not url or not api_key or not api_secret:
        logger.warning("Missing Schema Registry configuration in .env. Validation skipped.")
        return None
        
    sr_conf = {
        'url': url,
        'basic.auth.user.info': f"{api_key}:{api_secret}"
    }
    return SchemaRegistryClient(sr_conf)

def fetch_schema(client: Any, subject_name: str) -> Optional[str]:
    try:
        # Get latest schema
        schema = client.get_latest_version(subject_name)
        if VALIDATION_VERBOSE:
            logger.info(f"Fetched schema for subject: {subject_name} (ID: {schema.schema_id})")
        return schema.schema.schema_str
    except Exception as e:
        logger.error(f"Error fetching schema for {subject_name}: {e}")
        return None

def validate_data(data: Any, schema_str: str, subject: str) -> bool:
    if not HAS_DEPS:
        return True

    try:
        parsed_schema = fastavro.parse_schema(json.loads(schema_str))
        
        # fastavro.validate returns True if valid, raises ValidationError if raise_errors=True is passed (in some versions)
        # or we can just check the return value.
        # However, to get detailed errors, it's better to use validation.validate which might raise.
        # Let's stick to the pattern that worked in the script:
        # validate(data, parsed_schema, raise_errors=True)
        
        try:
            validate(data, parsed_schema, raise_errors=True)
            if VALIDATION_VERBOSE:
                logger.info(f"✅ Validation SUCCESS for {subject}")
            return True
        except Exception as e:
            logger.error(f"❌ Validation FAILED for {subject}: {e}")
            return False
            
    except Exception as e:
        logger.error(f"❌ Validation ERROR for {subject}: {e}")
        return False

def validate_payloads(
    manifest_data: Dict[str, Any], 
    output_data: Dict[str, Any],
    evidence_events: Optional[list] = None
) -> None:
    """
    Validates manifest, output data, and evidence events against Schema Registry.
    This function is intended to be called before persisting data.
    """
    if not HAS_DEPS:
        return

    if not VALIDATION_ENABLED:
        if VALIDATION_VERBOSE:
            logger.info("Validation skipped. Set SETORRA_VALIDATE_PAYLOADS=true to enable Schema Registry validation.")
        return

    # Feature flag to easily disable/remove
    if os.getenv("SETORRA_DISABLE_VALIDATION", "").lower() in ("true", "1", "yes"):
        logger.info("Validation disabled via SETORRA_DISABLE_VALIDATION.")
        return

    if VALIDATION_VERBOSE:
        logger.info("Starting payload validation against Schema Registry...")

    client = get_schema_client()
    if not client:
        return

    subjects = {
        "manifest": "setorra.manifest-value",
        "output": "setorra.output-value",
        "evidence": "setorra.evidence-value"
    }
    
    schemas = {}
    for key, subject in subjects.items():
        s = fetch_schema(client, subject)
        if s:
            schemas[key] = s

    summary = {
        "manifest": None,
        "output": None,
        "evidence": None,
    }

    # Validate Manifest
    if "manifest" in schemas and manifest_data:
        summary["manifest"] = validate_data(manifest_data, schemas["manifest"], "Manifest")

    # Validate Output
    if "output" in schemas and output_data:
        summary["output"] = validate_data(output_data, schemas["output"], "Output")

    # Validate Evidence Events
    if "evidence" in schemas and evidence_events:
        ev_count = len(evidence_events)
        all_valid = True
        failed_index = None
        if VALIDATION_VERBOSE:
            logger.info(f"Validating {ev_count} evidence events...")
        for i, event in enumerate(evidence_events):
            if not validate_data(event, schemas["evidence"], f"Evidence Event {i}"):
                all_valid = False
                failed_index = i
                break
        summary["evidence"] = all_valid
        if VALIDATION_VERBOSE and all_valid and evidence_events:
            logger.info("✅ All evidence events validated successfully.")
        if failed_index is not None and not VALIDATION_VERBOSE:
            logger.error(f"❌ Evidence validation failed at index {failed_index}")

    # Single summary line for non-verbose mode
    if not VALIDATION_VERBOSE:
        manifest_status = "ok" if summary["manifest"] is not False else "fail"
        output_status = "ok" if summary["output"] is not False else "fail"
        ev_status = (
            "skip" if summary["evidence"] is None else ("ok" if summary["evidence"] else "fail")
        )
        logger.info(f"[validation] manifest={manifest_status} output={output_status} evidence={ev_status}")
    else:
        logger.info("Validation sequence completed.")
