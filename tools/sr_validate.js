#!/usr/bin/env node
/**
 * Validate and encode hardcoded sample payloads against SR subjects.
 *
 * Usage:
 *   node tools/sr_validate.js
 *
 * Env vars required:
 *   CONFLUENT_SCHEMA_REGISTRY_URL
 *   CONFLUENT_SR_API_KEY
 *   CONFLUENT_SR_API_SECRET
 *
 * It fetches the latest schema for each subject below and attempts to encode
 * the sample payload. If encode succeeds, the payload matches the schema.
 */

const { SchemaRegistry } = require('@kafkajs/confluent-schema-registry');

const sessionId = '01KBT0F3Z8R8K0J2H0B6V4Q9X2';

const samples = [
  {
    subject: 'setorra.evidence.v1-value',
    payload: {
      schema_version: '1.3',
      session_id: sessionId,
      event_index: 0,
      event_type: 'agent.started',
      timestamp: '2025-11-23T10:45:00Z',
      agent: { name: 'sample-agent', version: '1.0.0' },
      privacy_flags: [],
      redacted_fields: [],
      parameters_redacted: null,
      context: null,
      execution: null,
      policy: null,
      approval: null,
      guardrail: null,
      integrity: {
        prev_hash: null,
        event_hash: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        signature: null,
      },
    },
  },
  {
    subject: 'setorra.output.v1-value',
    payload: {
      schema_version: '1.3',
      session_id: sessionId,
      timestamp: '2025-11-23T10:45:02Z',
      content: 'Sample final answer.',
      prompts: null,
      tools_used: [],
      errors: [],
      integrity: {
        first_event_hash: null,
        final_event_hash: null,
        chain_length: null,
      },
    },
  },
  {
    subject: 'setorra.manifest.v1-value',
    payload: {
      schema_version: 'manifest-v1',
      session_id: sessionId,
      context: {
        session_id: sessionId,
        env: null,
        toolchain_version: null,
        evidence_schema_version: null,
        started_at: null,
        finished_at: null,
      },
      produced_at: '2025-11-23T10:45:03Z',
      hash_alg: 'sha256-v1',
      objects: [
        {
          name: 'evidence.jsonl',
          content_type: 'application/x-ndjson',
          sha256: 'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
          bytes: 512,
          lines: null,
        },
        {
          name: 'output.json',
          content_type: 'application/json',
          sha256: 'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
          bytes: 256,
          lines: null,
        },
      ],
      chain_summary: {
        first_event_hash: null,
        final_event_hash: null,
        chain_length: null,
      },
    },
  },
];

async function main() {
  const srUrl = process.env.CONFLUENT_SCHEMA_REGISTRY_URL;
  const srUser = process.env.CONFLUENT_SR_API_KEY;
  const srPass = process.env.CONFLUENT_SR_API_SECRET;
  if (!srUrl || !srUser || !srPass) {
    console.error('Missing SR env vars: CONFLUENT_SCHEMA_REGISTRY_URL, CONFLUENT_SR_API_KEY, CONFLUENT_SR_API_SECRET');
    process.exit(2);
  }

  const registry = new SchemaRegistry({
    host: srUrl,
    auth: { username: srUser, password: srPass },
  });

  let failures = 0;
  for (const { subject, payload } of samples) {
    try {
      const { id } = await registry.getLatestSchema(subject);
      const buf = await registry.encode(id, payload);
      console.log(`OK subject=${subject} schemaId=${id} bytes=${buf.length}`);
    } catch (err) {
      failures += 1;
      console.error(`FAIL subject=${subject}: ${err.message}`);
    }
  }
  if (failures > 0) {
    process.exit(1);
  }
}

main();
