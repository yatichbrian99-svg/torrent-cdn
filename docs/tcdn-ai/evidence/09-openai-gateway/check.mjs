import fs from "node:fs";
import OpenAI from "openai";

const [apiKey, model] = fs.readFileSync("/tmp/openai-js-check/key.txt", "utf8").trim().split("\n");

const client = new OpenAI({
  apiKey,
  baseURL: "http://127.0.0.1:8099/v1",
  maxRetries: 0,
});

const out = {};

const models = await client.models.list();
out.models = {
  object: models.object,
  first: { id: models.data[0].id, object: models.data[0].object, owned_by: models.data[0].owned_by },
};

const reply = await client.chat.completions.create({
  model,
  messages: [{ role: "user", content: "hello from the official js client" }],
  max_tokens: 8,
});
out.non_streaming = {
  object: reply.object,
  id_prefix: reply.id.slice(0, 9),
  role: reply.choices[0].message.role,
  content: reply.choices[0].message.content,
  finish_reason: reply.choices[0].finish_reason,
  usage: reply.usage,
  tcdn: reply.tcdn,
};

const stream = await client.chat.completions.create({
  model,
  messages: [{ role: "user", content: "stream js" }],
  max_tokens: 8,
  stream: true,
  stream_options: { include_usage: true },
});
let text = "";
let usage = null;
let finish = null;
for await (const chunk of stream) {
  const choice = chunk.choices?.[0];
  if (choice?.delta?.content) text += choice.delta.content;
  if (choice?.finish_reason) finish = choice.finish_reason;
  if (chunk.usage) usage = chunk.usage;
}
out.streaming = { text, finish, usage };

try {
  await client.chat.completions.create({
    model,
    messages: [{ role: "user", content: "tools?" }],
    tools: [{ type: "function", function: { name: "noop", parameters: {} } }],
  });
} catch (error) {
  out.tools_error = { status: error.status, body: error.error ?? error.message };
}

console.log(JSON.stringify(out, null, 2));
