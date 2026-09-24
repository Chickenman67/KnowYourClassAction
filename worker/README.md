# kya-webhook (Milestone C)

A Cloudflare Worker that records the *Done / Not mine* button presses from the
Telegram digest into Workers KV, and serves that state back to the Python
pipeline so decided cases stop re-appearing in digests.

## Deploy

```bash
cd worker
npm install -g wrangler          # or use npx wrangler throughout
npx wrangler login
npx wrangler kv namespace create DECISIONS
#   -> paste the printed namespace id into wrangler.toml
npx wrangler deploy
npx wrangler secret put TELEGRAM_BOT_TOKEN   # same value as .env's KYA_TELEGRAM_BOT_TOKEN
npx wrangler secret put WEBHOOK_SECRET       # same value as .env's KYA_TELEGRAM_WEBHOOK_SECRET
```

Then, from the repo root:

```bash
kya --set-webhook https://kya-webhook.<your-subdomain>.workers.dev
# and add to .env:
#   KYA_DECISIONS_URL=https://kya-webhook.<your-subdomain>.workers.dev/decisions
```

One secret, two doors: `WEBHOOK_SECRET` is both the Telegram
`secret_token` (verified on every webhook POST via
`X-Telegram-Bot-Api-Secret-Token`) and the Bearer token for `GET /decisions`.

## Endpoints

| Route            | Auth                                   | Purpose                                  |
|------------------|----------------------------------------|------------------------------------------|
| `POST /telegram` | `X-Telegram-Bot-Api-Secret-Token`      | webhook updates; records button presses  |
| `GET /decisions` | `Authorization: Bearer <secret>`       | `{"ok":true,"decisions":{"<id>":{...}}}` |
| `GET /healthz`   | none                                   | liveness probe                           |

Callback payloads are validated with the same rule as
`kya.notify.parse_callback_data` (`kya:<action>:<id>`, action `done|skip`,
≤ 64 bytes); anything foreign is answered "Unrecognised button" and recorded
nowhere.

An id too long for that 64-byte budget arrives as a 16-hex-character alias
derived by `kya.notify.decision_key`. The worker stores and returns it
verbatim - no worker change is needed - and the Python side resolves it with
the same function, so a press still names exactly one case.

## Offline tests

```bash
cd worker
npm test    # node --test — real handler logic against Map-backed KV fakes
```