/**
 * KnowYourClassAction webhook worker (Milestone C).
 *
 * Two doors, one secret (env.WEBHOOK_SECRET):
 * - POST /telegram: updates Telegram delivers via setWebhook. The secret
 *   arrives as the X-Telegram-Bot-Api-Secret-Token header, which Telegram
 *   sets from the secret_token that `kya --set-webhook <base-url>` registers.
 *   A button press carries callback data `kya:<action>:<id>`; it is parsed
 *   with the exact same split rule as kya.notify.parse_callback_data, stored
 *   in KV, acknowledged with a toast, and the message's keyboard is cleared.
 * - GET /decisions: the recorded state for the Python side (`kya --decisions`
 *   and the digest's already-decided filter), guarded by a Bearer token.
 *
 * The pure parts (parseCallbackData, handleUpdate) take injectable KV and
 * Telegram dependencies, so `worker/test/` exercises the real logic offline.
 */

const NAMESPACE = "kya";
const ACTIONS = new Set(["done", "skip"]);
const CALLBACK_LIMIT_BYTES = 64; // the same hard limit kya.notify enforces

const CONFIRM_TEXT = { done: "Noted — done ✓", skip: "Noted — not mine" };

export function parseCallbackData(data) {
  if (typeof data !== "string" || data.length === 0) return null;
  const first = data.indexOf(":");
  if (first === -1) return null;
  const second = data.indexOf(":", first + 1);
  if (second === -1) return null;
  const namespace = data.slice(0, first);
  const action = data.slice(first + 1, second);
  const settlementId = data.slice(second + 1);
  if (namespace !== NAMESPACE || !ACTIONS.has(action) || settlementId.length === 0) {
    return null;
  }
  if (new TextEncoder().encode(data).length > CALLBACK_LIMIT_BYTES) return null;
  return { action, settlementId };
}

export const decisionKey = (settlementId) => `d:${settlementId}`;

async function callTelegram(token, method, payload) {
  const response = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await response.json().catch(() => null);
  if (!body || body.ok !== true) throw new Error(`${method} failed`);
  return body.result;
}

/**
 * Handle one Telegram update. `deps` is injectable for the offline test
 * suite: `{ kv, telegram }` replace the Workers KV binding and the network
 * Telegram client.
 */
export async function handleUpdate(update, env, deps = {}) {
  const kv = deps.kv ?? env.DECISIONS;
  const telegram =
    deps.telegram ?? ((method, payload) => callTelegram(env.TELEGRAM_BOT_TOKEN, method, payload));

  const callback = update?.callback_query;
  if (!callback) return { handled: false };

  const parsed = parseCallbackData(callback.data);
  if (!parsed) {
    // Foreign or malformed press: acknowledge politely, record nothing.
    await telegram("answerCallbackQuery", {
      callback_query_id: callback.id,
      text: "Unrecognised button",
    });
    return { handled: false, reason: "unrecognised-callback-data" };
  }

  const message = callback.message ?? {};
  const record = {
    action: parsed.action,
    ts: Math.floor(Date.now() / 1000),
    chat_id: message.chat?.id ?? null,
    message_id: message.message_id ?? null,
  };
  await kv.put(decisionKey(parsed.settlementId), JSON.stringify(record));
  await telegram("answerCallbackQuery", {
    callback_query_id: callback.id,
    text: CONFIRM_TEXT[parsed.action],
  });
  if (record.chat_id !== null && record.message_id !== null) {
    try {
      await telegram("editMessageReplyMarkup", {
        chat_id: record.chat_id,
        message_id: record.message_id,
        reply_markup: { inline_keyboard: [] },
      });
    } catch {
      // Clearing the keyboard is cosmetic; the decision is already stored.
    }
  }
  return { handled: true, ...parsed };
}

async function readAllDecisions(kv) {
  const decisions = {};
  let cursor;
  do {
    const page = await kv.list({ prefix: "d:", cursor });
    await Promise.all(
      page.keys.map(async (key) => {
        const raw = await kv.get(key.name);
        try {
          decisions[key.name.slice(2)] = JSON.parse(raw);
        } catch {
          decisions[key.name.slice(2)] = { action: "unknown" };
        }
      }),
    );
    cursor = page.list_complete ? undefined : page.cursor;
  } while (cursor !== undefined);
  return decisions;
}

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

const asMessage = (err) => String(err && err.message ? err.message : err);

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/healthz") {
      return json({ ok: true });
    }

    if (request.method === "POST" && url.pathname === "/telegram") {
      if (!env.WEBHOOK_SECRET
          || request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
        return json({ ok: false, error: "forbidden" }, 403);
      }
      let update;
      try {
        update = await request.json();
      } catch {
        return json({ ok: false, error: "invalid JSON" }, 400);
      }
      try {
        return json({ ok: true, ...(await handleUpdate(update, env)) });
      } catch (err) {
        return json({ ok: false, error: asMessage(err) }, 500);
      }
    }

    if (request.method === "GET" && url.pathname === "/decisions") {
      if (!env.WEBHOOK_SECRET
          || request.headers.get("Authorization") !== `Bearer ${env.WEBHOOK_SECRET}`) {
        return json({ ok: false, error: "forbidden" }, 403);
      }
      try {
        return json({ ok: true, decisions: await readAllDecisions(env.DECISIONS) });
      } catch (err) {
        return json({ ok: false, error: asMessage(err) }, 500);
      }
    }

    return json({ ok: false, error: "not found" }, 404);
  },
};
