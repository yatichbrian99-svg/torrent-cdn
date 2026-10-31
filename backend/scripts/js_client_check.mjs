#!/usr/bin/env node
// Row 2.1: the *official* OpenAI JavaScript client against a live gateway.
//
// tests/test_ai_openai_compat.py already drives the official Python client
// over ASGI, which is fast but cannot be reached from npm. OpenAI's own JS SDK
// speaks HTTP only, so this script talks to scripts/gateway_harness.py on a
// socket — the same code path a Cursor/Continue user would take.
//
// Usage (server already started, see the harness docstring):
//   node scripts/js_client_check.mjs /tmp/gateway-harness.json
//
// Exits non-zero and prints the failed assertion if the gateway stops behaving
// like an OpenAI endpoint; prints a JSON summary on success so the CI log
// records what was actually received.

import { readFileSync } from "node:fs";
import OpenAI from "openai";

const infoPath = process.argv[2] ?? "/tmp/gateway-harness.json";
const info = JSON.parse(readFileSync(infoPath, "utf8"));

const failures = [];
let checks = 0;
function check(name, condition, detail) {
  checks += 1;
  if (!condition) failures.push({ name, detail });
}

const client = new OpenAI({ apiKey: info.api_key, baseURL: info.base_url, maxRetries: 0 });

// ── 1. /v1/models is an OpenAI list, and says it is gateway mode ────────────
const models = await client.models.list();
check("models.object", models.object === "list", models.object);
const entry = models.data.find((model) => model.id === info.model);
check("models.entry", Boolean(entry), models.data.map((m) => m.id).join(","));
check("models.gateway_mode", entry?.tcdn?.gateway_mode === true, entry?.tcdn);

// ── 2. Non-streaming chat completion ──────────────────────────────────
const reply = await client.chat.completions.create({
  model: info.model,
  messages: [{ role: "user", content: "hello from the official JS client" }],
  max_tokens: 8,
});
check("completion.object", reply.object === "chat.completion", reply.object);
check("completion.id", reply.id.startsWith("chatcmpl-"), reply.id);
check(
  "completion.content",
  reply.choices[0]?.message?.content?.startsWith(`echo[${info.model}]`),
  reply.choices[0]?.message?.content,
);
check("completion.finish_reason", reply.choices[0]?.finish_reason === "stop", reply.choices[0]?.finish_reason);
check(
  "completion.usage",
  reply.usage?.total_tokens === reply.usage?.prompt_tokens + reply.usage?.completion_tokens,
  reply.usage,
);
// The gateway must disclose what it can see: standard tier, no E2E encryption.
check("completion.mode", reply.tcdn?.mode === "gateway", reply.tcdn);
check("completion.prompt_visibility", reply.tcdn?.prompt_visibility === "gateway", reply.tcdn);
check("completion.confidentiality", reply.tcdn?.confidentiality === "none", reply.tcdn);
check("completion.job_id", Boolean(reply.tcdn?.job_id), reply.tcdn);

// ── 3. Streaming, including the final usage chunk ───────────────────────────
const stream = await client.chat.completions.create({
  model: info.model,
  messages: [{ role: "user", content: "stream through the official JS client" }],
  max_tokens: 8,
  stream: true,
  stream_options: { include_usage: true },
});
let content = "";
let sawUsage = false;
for await (const chunk of stream) {
  content += chunk.choices?.[0]?.delta?.content ?? "";
  if (chunk.usage) sawUsage = true;
}
check("stream.content", content.startsWith(`echo[${info.model}]`), content);
check("stream.usage", sawUsage, sawUsage);

// ── 4. Unsupported parameters are rejected, not silently ignored ────────────
let toolsError = null;
try {
  await client.chat.completions.create({
    model: info.model,
    messages: [{ role: "user", content: "use a tool" }],
    tools: [{ type: "function", function: { name: "noop", parameters: {} } }],
  });
} catch (error) {
  toolsError = error;
}
check("tools.rejected", Boolean(toolsError), "request unexpectedly succeeded");
check("tools.status", toolsError?.status === 400, toolsError?.status);
check(
  "tools.code",
  toolsError?.error?.code === "unsupported_parameter",
  toolsError?.error,
);

// ── 5. Confidential tier is pointed at the SDK, not silently downgraded ─────
let confidentialError = null;
try {
  await client.chat.completions.create({
    model: info.model,
    messages: [{ role: "user", content: "keep this private" }],
    // `confidential` is a TCDN extension. The JS SDK sends unknown keys in the
    // body verbatim (only its TypeScript type is narrower), so no extra_body
    // wrapper as in the Python client.
    confidential: true,
  });
} catch (error) {
  confidentialError = error;
}
check("confidential.rejected", Boolean(confidentialError), "request unexpectedly succeeded");
check(
  "confidential.sdk_hint",
  /SDK/i.test(JSON.stringify(confidentialError?.error ?? {})),
  confidentialError?.error,
);

if (failures.length > 0) {
  console.error(JSON.stringify({ ok: false, failures }, null, 2));
  process.exit(1);
}
console.log(
  JSON.stringify(
    {
      ok: true,
      client: "openai (official, npm)",
      node: process.version,
      checks,
      model: info.model,
      completion_id: reply.id,
      streamed_chars: content.length,
    },
    null,
    2,
  ),
);
