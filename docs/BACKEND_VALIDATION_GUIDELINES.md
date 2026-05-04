# Backend Validation Guidelines - Node.js

**Setorra SDK → Backend Endpoints → Schema Validation → Local Storage**

---

## Overview

The Setorra SDK sends **JSON data** to your backend endpoints. You must validate this JSON against **Avro schemas** stored in Confluent Cloud Schema Registry before storing locally.

**Key Point:** SDK sends JSON, but schemas are Avro. You validate JSON structure against Avro schema definitions.

---

## Architecture Flow

```
SDK (JSON) → Backend Endpoint → Fetch Avro Schema from Confluent → Validate JSON → Store Locally
```

**Three Endpoints:**
1. `POST /v1/ingest/evidence` - Evidence events (JSON)
2. `POST /v1/ingest/output` - Output summaries (JSON)
3. `POST /v1/ingest/manifest` - Manifests (JSON)

---

## Step 1: Setup Confluent Schema Registry Client

### Install Dependencies

```bash
npm install @kafkajs/confluent-schema-registry avsc
```

### Initialize Client

```javascript
const { SchemaRegistry } = require('@kafkajs/confluent-schema-registry');

const registry = new SchemaRegistry({
  host: 'https://psrc-XXXXX.confluent.cloud',
  auth: {
    username: process.env.CONFLUENT_SR_API_KEY,
    password: process.env.CONFLUENT_SR_API_SECRET
  }
});
```

**Schema IDs to Use:**
- Evidence: **100018** (`setorra.evidence-value`)
- Output: **100019** (`setorra.output-value`)
- Manifest: **100020** (`setorra.manifest-value`)

---

## Step 2: Fetch Schemas from Confluent Cloud

### Fetch by Schema ID (Recommended)

```javascript
// Fetch once at startup, cache in memory
const evidenceSchema = await registry.getSchema(100018);
const outputSchema = await registry.getSchema(100019);
const manifestSchema = await registry.getSchema(100020);
```

### Or Fetch by Subject Name

```javascript
const evidenceSchema = await registry.getLatestSchemaId('setorra.evidence-value');
const schema = await registry.getSchema(evidenceSchema.id);
```

**Important:** Fetch schemas at application startup and cache them. Don't fetch on every request (rate limits).

---

## Step 3: Understand Schema Structure

### Key Schema Characteristics

**Evidence Event Schema (ID: 100018):**
- 16 fields total
- Flexible fields stored as **JSON strings**: `parameters_redacted`, `context`, `execution`, `policy`, `approval`, `guardrail`
- Arrays: `privacy_flags`, `redacted_fields`
- Nested record: `integrity` (prev_hash, event_hash, signature)

**Output Summary Schema (ID: 100019):**
- 23 fields total
- Flexible fields as **JSON strings**: `prompts`, `final_output`, `run_result`, `tools_used`, `reasoning`, `consents`, `environment_info`, `policy_summary`, `guardrail_summary`, `errors`, `integrity`, `organization`
- Integers: `duration_ms`, `errors_truncated`
- Nullable: `model_id`, `latency_ms`, `cost_estimate`

**Manifest Schema (ID: 100020):**
- Nested records: `context`, `objects[]`, `chain_summary`
- Strongly typed structure (not JSON strings)

---

## Step 4: Validation Strategy

### Option A: Convert JSON → Avro → Validate (Recommended)

**Why:** Avro libraries can validate JSON structure against Avro schema.

```javascript
const avsc = require('avsc');

// Parse Avro schema from registry
const schema = avsc.parse(evidenceSchema.schema);

// Validate JSON against schema
try {
  const valid = schema.isValid(jsonData);
  if (!valid) {
    // Get detailed errors
    const errors = schema.validate(jsonData);
  }
} catch (error) {
  // Schema validation failed
}
```

### Option B: Manual Field-by-Field Validation

**Why:** More control, but more code.

```javascript
// Check required fields exist
const requiredFields = ['schema_version', 'session_id', 'event_index', ...];
for (const field of requiredFields) {
  if (!(field in jsonData)) {
    throw new ValidationError(`Missing required field: ${field}`);
  }
}

// Check field types
if (typeof jsonData.event_index !== 'number') {
  throw new ValidationError('event_index must be integer');
}

// Check union types (nullable fields)
if (jsonData.parameters_redacted !== null && typeof jsonData.parameters_redacted !== 'string') {
  throw new ValidationError('parameters_redacted must be null or string');
}
```

---

## Step 5: Handle JSON String Fields

### Critical: Parse Nested JSON Strings

Many fields are stored as **JSON strings** in the schema, but SDK sends them as **objects**. You need to:

**For Evidence Events:**
```javascript
// SDK sends: context: {environment: {...}, organization: {...}}
// Schema expects: context: string (JSON string)

// Option 1: SDK already stringifies (check your SDK)
// Option 2: You stringify before validation
const preparedData = {
  ...jsonData,
  context: typeof jsonData.context === 'string' 
    ? jsonData.context 
    : JSON.stringify(jsonData.context),
  execution: typeof jsonData.execution === 'string'
    ? jsonData.execution
    : JSON.stringify(jsonData.execution),
  // ... same for policy, approval, guardrail
};
```

**For Output Summaries:**
```javascript
// Similar handling for: prompts, final_output, run_result, tools_used,
// reasoning, consents, environment_info, policy_summary, guardrail_summary,
// errors, integrity, organization
```

**For Manifests:**
```javascript
// Manifests have nested records (not JSON strings)
// No stringification needed - validate structure directly
```

---

## Step 6: Validation Checklist

### Evidence Event Validation

- [ ] All 16 fields present (or nullable fields can be null)
- [ ] `event_index` is integer
- [ ] `timestamp` is ISO-8601 string
- [ ] `privacy_flags` and `redacted_fields` are arrays of strings
- [ ] Flexible fields (`context`, `execution`, etc.) are strings (or stringify objects)
- [ ] `integrity` object has `prev_hash` (nullable), `event_hash` (required), `signature` (nullable)
- [ ] `event_type` matches known types (agent.started, tool.start, etc.)

### Output Summary Validation

- [ ] All 23 fields present (or nullable fields can be null)
- [ ] `duration_ms` and `errors_truncated` are integers
- [ ] `latency_ms` is integer or null
- [ ] `cost_estimate` is number or null
- [ ] Flexible fields are strings (or stringify objects)
- [ ] `integrity` field is JSON string containing `{first_event_hash, last_event_hash, count, signature}`

### Manifest Validation

- [ ] `context` object has all required fields (session_id, env, started_at, etc.)
- [ ] `objects` array contains records with: name, bytes, sha256, content_type, lines
- [ ] `chain_summary` object has: total_events (int), first_event_hash, final_event_hash, root_signature, signer_id

---

## Step 7: Error Handling

### Validation Errors

```javascript
// Return clear error messages
if (!valid) {
  return res.status(400).json({
    error: 'Schema validation failed',
    schema_id: 100018,
    subject: 'setorra.evidence-value',
    details: validationErrors,
    received_data_preview: Object.keys(jsonData)
  });
}
```

### Schema Registry Errors

```javascript
try {
  const schema = await registry.getSchema(100018);
} catch (error) {
  if (error.statusCode === 404) {
    // Schema not found - check schema ID
  } else if (error.statusCode === 401) {
    // Authentication failed - check credentials
  } else {
    // Network/other error - retry with backoff
  }
}
```

---

## Step 8: Performance Optimization

### Cache Schemas

```javascript
// Fetch once at startup
let cachedSchemas = {};

async function initializeSchemas() {
  cachedSchemas.evidence = await registry.getSchema(100018);
  cachedSchemas.output = await registry.getSchema(100019);
  cachedSchemas.manifest = await registry.getSchema(100020);
}

// Use cached schemas in endpoints
app.post('/v1/ingest/evidence', async (req, res) => {
  const schema = cachedSchemas.evidence;
  // Validate against cached schema
});
```

### Batch Validation

```javascript
// If receiving multiple events in one request
const events = req.body.events; // Array
const results = events.map(event => validate(event, schema));
const allValid = results.every(r => r.valid);
```

---

## Step 9: Testing Strategy

### Test Cases Based on Validation Results

**We validated 5 sessions with 100% success. Use these patterns:**

1. **Simple Failure Case** (Session: `01K8JQB2S3JXB9CP94Z89K4142`)
   - 6 events
   - APIConnectionError
   - No tools, minimal data
   - **Test:** Basic validation, nullable fields

2. **Complex Success Case** (Session: `01K8JH3KPB2AZ7Y3AHAK6N3FV0`)
   - 62 events
   - Tools used (Google Calendar)
   - Guardrails applied
   - Organization data present
   - **Test:** Full feature set, nested JSON strings

3. **With Organization** (Session: `01K8R071MMBEWZPWW0ZFHQTW0F`)
   - Organization block in context
   - **Test:** Optional organization fields

4. **Medium Complexity** (Sessions: `01K9BSXCKYEFZ0W1VWVEM2MT07`, `01K9EH14FBPSTYP0Z7NA293PS6`)
   - Multiple tools
   - Policy decisions
   - **Test:** Policy and guardrail summaries

### Sample Test Data

```javascript
// Evidence event (minimal)
const minimalEvent = {
  schema_version: "1.2",
  session_id: "01K8JQB2S3JXB9CP94Z89K4142",
  event_index: 0,
  timestamp: "2025-10-27T11:37:31Z",
  agent_name: "cli-chat",
  agent_version: "0.1.0",
  event_type: "agent.started",
  parameters_redacted: "{}",
  privacy_flags: [],
  redacted_fields: [],
  context: "{}",
  execution: '{"status":"started"}',
  policy: '{"version":"0","decision":"not_evaluated"}',
  approval: null,
  guardrail: '{"status":"unconfigured"}',
  integrity: {
    prev_hash: null,
    event_hash: "4aa711ee88182e9d5867d53df4378c8d3cf8d76858114b72ce37f1e85aa5b5f0",
    signature: null
  }
};
```

---

## Step 10: Common Pitfalls & Solutions

### Pitfall 1: JSON String vs Object Mismatch

**Problem:** SDK sends objects, schema expects strings.

**Solution:** Check SDK output format. If objects, stringify before validation:
```javascript
if (typeof data.context === 'object') {
  data.context = JSON.stringify(data.context);
}
```

### Pitfall 2: Missing Nullable Fields

**Problem:** Optional fields not present in JSON.

**Solution:** Schema has defaults. Handle missing fields:
```javascript
const prepared = {
  ...jsonData,
  approval: jsonData.approval ?? null,
  model_id: jsonData.model_id ?? null
};
```

### Pitfall 3: Integer Type Mismatch

**Problem:** JavaScript numbers might be floats.

**Solution:** Ensure integers:
```javascript
if (typeof data.event_index === 'number') {
  data.event_index = Math.floor(data.event_index);
}
```

### Pitfall 4: Array Type Validation

**Problem:** Arrays might be null or undefined.

**Solution:** Normalize arrays:
```javascript
const privacy_flags = Array.isArray(data.privacy_flags) 
  ? data.privacy_flags 
  : [];
```

---

## Step 11: Validation Response Format

### Success Response

```javascript
res.status(200).json({
  success: true,
  session_id: jsonData.session_id,
  schema_id: 100018,
  validated_at: new Date().toISOString(),
  stored: true
});
```

### Error Response

```javascript
res.status(400).json({
  success: false,
  error: 'validation_failed',
  schema_id: 100018,
  subject: 'setorra.evidence-value',
  errors: [
    {
      field: 'event_index',
      expected: 'integer',
      received: typeof jsonData.event_index,
      value: jsonData.event_index
    }
  ],
  message: 'Schema validation failed. See errors array for details.'
});
```

---

## Step 12: Monitoring & Logging

### Log Validation Metrics

```javascript
// Track validation success/failure rates
logger.info('Evidence validation', {
  session_id: jsonData.session_id,
  schema_id: 100018,
  valid: isValid,
  validation_time_ms: Date.now() - startTime,
  fields_validated: Object.keys(jsonData).length
});
```

### Alert on Schema Mismatches

```javascript
if (!isValid) {
  // Alert team if validation fails
  alertService.send({
    type: 'schema_validation_failure',
    schema_id: 100018,
    session_id: jsonData.session_id,
    errors: validationErrors
  });
}
```

---

## Step 13: Schema Evolution Handling

### BACKWARD Compatibility Rules

**Current Schema Version:** 1  
**Compatibility Mode:** BACKWARD

**What This Means:**
- ✅ New fields may be added (with defaults)
- ✅ Fields may be removed (you'll see null/defaults)
- ❌ Field types cannot change
- ❌ Required fields cannot be added

**Your Code Should:**
```javascript
// Handle missing fields gracefully
const modelId = jsonData.model_id ?? null; // New field might not exist

// Don't break on extra fields (ignore them)
const validated = schema.validate(jsonData, { noUnknownFields: false });
```

---

## Step 14: Endpoint Implementation Checklist

### Evidence Endpoint (`POST /v1/ingest/evidence`)

- [ ] Fetch schema ID 100018 (cached)
- [ ] Parse JSON body
- [ ] Stringify nested objects if needed (context, execution, policy, etc.)
- [ ] Validate against schema
- [ ] Check integrity.prev_hash links correctly (if not first event)
- [ ] Store locally (JSON or Avro format)
- [ ] Return success/error response

### Output Endpoint (`POST /v1/ingest/output`)

- [ ] Fetch schema ID 100019 (cached)
- [ ] Parse JSON body
- [ ] Stringify nested objects if needed (prompts, tools_used, etc.)
- [ ] Validate against schema
- [ ] Ensure errors_truncated is integer (default 0)
- [ ] Store locally
- [ ] Return success/error response

### Manifest Endpoint (`POST /v1/ingest/manifest`)

- [ ] Fetch schema ID 100020 (cached)
- [ ] Parse JSON body
- [ ] Validate nested records (context, objects[], chain_summary)
- [ ] Verify chain_summary.total_events matches evidence count
- [ ] Store locally
- [ ] Return success/error response

---

## Step 15: Integration Testing

### Test Against Real SDK Data

Use the validated sessions from our tests:

1. **Request SDK to send test data** to your endpoints
2. **Validate against schemas** (should pass 100%)
3. **Check stored data** matches input
4. **Verify error handling** with invalid data

### Test Scenarios

| Scenario | Expected Result |
|----------|----------------|
| Valid evidence event | ✅ 200 OK, stored |
| Missing required field | ❌ 400 Bad Request, error details |
| Wrong field type | ❌ 400 Bad Request, type mismatch |
| Invalid JSON string field | ❌ 400 Bad Request, parse error |
| Schema registry down | ❌ 503 Service Unavailable, retry |
| Valid output summary | ✅ 200 OK, stored |
| Valid manifest | ✅ 200 OK, stored |

---

## Quick Reference

### Schema Registry Connection

```javascript
Host: https://psrc-XXXXX.confluent.cloud
Auth: Basic Auth (API Key + Secret)
Schema IDs: 100018, 100019, 100020
Subjects: setorra.evidence-value, setorra.output-value, setorra.manifest-value
```

### Validation Libraries

- **@kafkajs/confluent-schema-registry** - Fetch schemas
- **avsc** - Validate JSON against Avro schema
- **ajv** (alternative) - JSON Schema validation (if converting Avro → JSON Schema)

### Key Fields to Validate

**Evidence:** event_index (int), event_type (string), integrity (object)  
**Output:** duration_ms (int), errors_truncated (int), integrity (string)  
**Manifest:** context (object), objects[] (array), chain_summary (object)

---

## Support & Questions

**If validation fails:**
1. Check schema ID matches (100018, 100019, 100020)
2. Verify JSON string fields are actually strings
3. Ensure integer fields are numbers (not strings)
4. Check nullable fields can be null
5. Review validation error details

**If schema registry errors:**
1. Verify credentials (API key + secret)
2. Check network connectivity
3. Verify schema IDs exist in your Confluent account
4. Check rate limits (cache schemas!)

---

**Remember:** SDK sends JSON, but schemas are Avro. Your job is to validate JSON structure matches Avro schema definition. The validation libraries handle the complexity - you just need to ensure data format matches!

