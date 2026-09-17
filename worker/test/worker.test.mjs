import test from "node:test";
import assert from "node:assert/strict";
import worker, { parseCallbackData, handleUpdate, decisionKey } from "../src/index.js";

// --- fakes --------------------------------------------------------------------
class FakeKV {
  constructor(entries = {}) {
    this.map = new Map(Object.entries(entries));
  }
  async put(key, value) {
    this.map.set(key, value);
  }
  async get(key) {
    return this.map.has(key) ? this.map.get(key) : null;
  }
  async list({ prefix = "", cursor } = {}) {
    const names = [...this.map.keys()].filter((n) => n.startsWith(prefix)).sort();
    if (cursor === undefined) {
      return { keys: names.map((name) => ({ name })), list_complete: true };
    }
    return { keys: [], list_complete: true };
  }
}

function fakeTelegram(failMethod = null) {
  const calls = [];
  return {
    calls,
    async telegram(method, payload) {
      calls.push([method, payload]);
      if (method === failMethod) throw new Error(`${method} failed`);
      return {};
    },
  };
}

const press = (data, { chatId = 7, messageId = 42, queryId = "cq1" } = {}) => ({
  update_id: 1,
  callback_query: {
    id: queryId,
    data,
    message: { chat: { id: chatId }, message_id: messageId },
  },
});

// --- parseCallbackData: the same contract as kya.notify.parse_callback_data ----
test("parseCallbackData accepts ours, including ids that contain colons", () => {
  assert.deepEqual(parseCallbackData("kya:done:abc"), { action: "done", settlementId: "abc" });
  assert.deepEqual(parseCallbackData("kya:skip:x:y:z"), { action: "skip", settlementId: "x:y:z" });
});

test("parseCallbackData rejects foreign, malformed, empty and oversized payloads", () => {
  assert.equal(parseCallbackData("other:done:x"), null);
  assert.equal(parseCallbackData("kya:withdraw:x"), null);
  assert.equal(parseCallbackData("kya:done:"), null);
  assert.equal(parseCallbackData("just-one-part"), null);
  assert.equal(parseCallbackData(""), null);
  assert.equal(parseCallbackData(null), null);
  assert.equal(parseCallbackData(42), null);
  assert.equal(parseCallbackData(`kya:done:${"x".repeat(70)}`), null, "over the 64-byte limit");
});

// --- handleUpdate: a press becomes a KV record + toast + cleared keyboard ------
test("a Done press is recorded, acknowledged and its keyboard cleared", async () => {
  const kv = new FakeKV();
  const { calls, telegram } = fakeTelegram();
  const result = await handleUpdate(press("kya:done:case-9"), {}, { kv, telegram });
  assert.equal(result.handled, true);
  assert.deepEqual(result, { handled: true, action: "done", settlementId: "case-9" });
  const stored = JSON.parse(await kv.get(decisionKey("case-9")));
  assert.equal(stored.action, "done");
  assert.equal(stored.chat_id, 7);
  assert.equal(stored.message_id, 42);
  assert.equal(typeof stored.ts, "number");
  assert.deepEqual(calls[0], [
    "answerCallbackQuery",
    { callback_query_id: "cq1", text: "Noted — done ✓" },
  ]);
  assert.deepEqual(calls[1], [
    "editMessageReplyMarkup",
    { chat_id: 7, message_id: 42, reply_markup: { inline_keyboard: [] } },
  ]);
});

test("a Not-mine press records the skip action", async () => {
  const kv = new FakeKV();
  const { calls, telegram } = fakeTelegram();
  await handleUpdate(press("kya:skip:s1"), {}, { kv, telegram });
  assert.equal(JSON.parse(await kv.get(decisionKey("s1"))).action, "skip");
  assert.equal(calls[0][1].text, "Noted — not mine");
});

test("a press without message context still records, but skips the cosmetic edit", async () => {
  const kv = new FakeKV();
  const { calls, telegram } = fakeTelegram();
  const update = { callback_query: { id: "cq2", data: "kya:done:s2" } };
  await handleUpdate(update, {}, { kv, telegram });
  assert.equal(JSON.parse(await kv.get(decisionKey("s2"))).action, "done");
  assert.equal(calls.length, 1, "no editMessageReplyMarkup without a message id");
});

test("malformed callback data is answered politely and recorded nowhere", async () => {
  const kv = new FakeKV();
  const { calls, telegram } = fakeTelegram();
  const result = await handleUpdate(press("other:done:x"), {}, { kv, telegram });
  assert.equal(result.handled, false);
  assert.equal(calls[0][1].text, "Unrecognised button");
  assert.equal(calls.length, 1, "no edit for a rejected press");
  assert.equal(kv.map.size, 0);
});

test("non-callback updates are ignored without any Telegram call", async () => {
  const kv = new FakeKV();
  const { calls, telegram } = fakeTelegram();
  assert.deepEqual(await handleUpdate({ update_id: 2, message: {} }, {}, { kv, telegram }), {
    handled: false,
  });
  assert.equal(calls.length, 0);
});

test("a failed keyboard edit does not lose the decision", async () => {
  const kv = new FakeKV();
  const { telegram } = fakeTelegram("editMessageReplyMarkup");
  const result = await handleUpdate(press("kya:done:s3"), {}, { kv, telegram });
  assert.equal(result.handled, true);
  assert.equal(JSON.parse(await kv.get(decisionKey("s3"))).action, "done");
});

// --- the HTTP surface -----------------------------------------------------------
const SECRET = "s3cret";
const env = { WEBHOOK_SECRET: SECRET, TELEGRAM_BOT_TOKEN: "t", DECISIONS: new FakeKV() };
const post = (path, headers, body) =>
  worker.fetch(new Request(`https://worker.test${path}`, { method: "POST", headers, body }), env);
const get = (path, headers = {}) =>
  worker.fetch(new Request(`https://worker.test${path}`, { method: "GET", headers }), env);

test("the webhook refuses posts without the Telegram secret header", async () => {
  const response = await post("/telegram", { "content-type": "application/json" }, "{}");
  assert.equal(response.status, 403);
});

test("the webhook accepts a non-callback update end-to-end", async () => {
  const response = await post(
    "/telegram",
    {
      "content-type": "application/json",
      "x-telegram-bot-api-secret-token": SECRET,
    },
    JSON.stringify({ update_id: 3 }),
  );
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ok: true, handled: false });
});

test("garbage bodies get a 400, unknown routes a 404", async () => {
  assert.equal(
    (await post("/telegram", { "x-telegram-bot-api-secret-token": SECRET }, "not json")).status,
    400,
  );
  assert.equal((await get("/nope")).status, 404);
});

test("/decisions requires the bearer secret and returns the KV state", async () => {
  const kv = new FakeKV({
    "d:case-a": JSON.stringify({ action: "done", ts: 1, chat_id: 7, message_id: 8 }),
    "d:case-b": JSON.stringify({ action: "skip", ts: 2, chat_id: 7, message_id: 9 }),
    "other:thing": JSON.stringify({ action: "done" }), // different prefix: never served
  });
  const guarded = { ...env, DECISIONS: kv };
  assert.equal((await get("/decisions")).status, 403);
  assert.equal((await get("/decisions", { Authorization: "Bearer wrong" })).status, 403);

  const response = await worker.fetch(
    new Request("https://worker.test/decisions", {
      headers: { Authorization: `Bearer ${SECRET}` },
    }),
    guarded,
  );
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.ok, true);
  assert.deepEqual(Object.keys(body.decisions).sort(), ["case-a", "case-b"]);
  assert.equal(body.decisions["case-a"].action, "done");
});

test("/healthz is open and honest", async () => {
  assert.deepEqual(await (await get("/healthz")).json(), { ok: true });
});

