# KnowYourClassAction

A free, self-updating tracker for US class-action settlements, split by **how
big the money is**, **what it takes to claim it**, and **when the window
closes**. Not legal advice, not a law firm, not a settlement administrator.

Current state: **Milestones A and B complete** — the pipeline publishes a
static site, and the Telegram bot delivers diffs as messages with inline
*Done / Not mine* buttons (recording those decisions is the Milestone C
webhook). The Cloudflare worker and scheduled GitHub Actions runs are on the
roadmap below.

## Why this exists

Class-action deadlines are easy to miss and hard to compare. An aggregator
announces *"up to $5,000!"* — but that figure is usually a documents-required
ceiling, while the realistic no-receipts claim is $50. The difference decides
whether a claim is worth ten minutes of your time, so this project treats
**payout size** and **proof burden** as first-class, separately-modelled
dimensions instead of one headline number.

## The four lanes

Lane assignment is a deterministic classification rule (`src/kya/classify.py`),
not a judgement call. Predicates are checked in priority order and the first
match wins; every row records a `lane_reason` explaining why it sits there.

| Lane | Rule | Meaning |
|---|---|---|
| 3 · Investigation | `/lawsuits/` URL, or "free case review" / "no settlement or claim form" | Nothing claimable yet; an invitation to join |
| 4 · Pending | "pending final approval" / not yet verified | A settlement exists but no confirmed claim window |
| 2 · Automatic | proof level L0 (automatic payment, no claim form) | No action needed — but watch opt-out/election dates |
| 1 · Claimable | everything else with an open window | File a claim |

## The proof ladder (L0–L4)

`src/kya/normalize.py` models the burden per **benefit tier**, not per
settlement, because many settlements have two paths to the money:

| Level | Meaning | Example |
|---|---|---|
| L0 | Automatic — no claim at all | *automatic payment — no claim form* |
| L1 | Self-report, no documents | *up to $5 without proof* |
| L2 | ID/PIN from the notice | *Login ID and PIN from the mailed notice* |
| L3 | Documents required | *receipts or proof of purchase* |
| L4 | Dual tier — an easy path **and** a larger documented one | *$2.00 per unit capped $6.00 without proof, uncapped with proof* |

### Documented inferences

Where the sources under-specify, the pipeline infers — and every inference is
listed here rather than hidden in code:

- **L4 dual-tier reporting.** When a page's proof detail gives an ID/PIN for
  filing *and* scopes documents to a higher tier (`"receipts required for the
  up-to-$2,500 losses option"`), the entry bar is the ID, not the receipts.
  Reading the receipts as mandatory would send people digging for paperwork
  they do not need — the single most damaging misread, and the one with the
  most parser machinery behind it.
- **Negated documents are not a bar.** `"no receipts"` or `"without proof of
  purchase"` is the opposite of a documents requirement.
- **The administrator's records are not your paperwork.** `"eligibility is
  determined from the bank's own records"` describes their process, not a
  document you must produce.
- **Uncapped sibling rates.** The Earth Rated shape — *"$2.00 per unit, capped
  $6.00 without proof, uncapped with proof"* — states the rate once; the
  uncapped tier borrows it while keeping its own `cap = null`.
- **Fund sizes are never payouts.** `"Pro Rata Cash from a $3M Fund"` yields a
  `pro_rata_fund` tier with no amount, because the fund is not what you receive.

### Sources disagree? Flag, never merge

The page's own `Proof Required` fact is authoritative over the index label —
but a *systematic* disagreement between them usually means the parser, not the
world, is wrong. So conflicts become recorded warnings, and the repo ships an
audit tool that groups them by shape. On the last full build, 259 enriched
settlements carried exactly **3** flagged proof disagreements (Toyota airbags,
FCA valve train, VSL#3) — all genuine two-path ambiguities a human should
review, down from 103 systematic misreads that the audit exposed and the
parser fixes above eliminated.

## Sources and authority

- **openclassactions.com** (`llms.txt` index + per-case pages) is the
  discovery layer, fetched politely: identifying User-Agent, ≥ 1 s between
  requests, robots respected, 6 h disk cache. Their `llms.txt` asks for
  attribution — this is it.
- The **official claim portal** linked on every card is the authority. Figures
  here exist because a human read a settlement agreement; they carry a
  `verified_as_of` stamp and expire visibly.
- CourtListener and topclassactions RSS are planned secondary sources.

## Running it

```bash
pip install -e .[dev]
python -m pytest                    # 221 tests, all offline (fixtures captured live)
kya --pages --site                  # full build: dataset + docs/ site
python tools/build_dataset.py --limit 5   # smoke run without installing
python tools/audit_warnings.py      # group the build's warnings by shape
```

### Telegram setup (Milestone B)

Credentials live in `.env` (gitignored — this repo is public); see
`.env.example`. Nothing is ever read from `config.yaml`, which is committed.

```bash
# 1. @BotFather -> /newbot, copy the token into .env as KYA_TELEGRAM_BOT_TOKEN
# 2. open the bot in Telegram and press Start, then:
kya --whoami             # bot identity + the chat id that messaged it
# 3. put that id in .env as KYA_TELEGRAM_CHAT_ID, then:
kya --send-test-message  # one message with Done / Not mine buttons
kya --notify-digest      # diff vs the stored snapshot, send what's new
```

Small digests send one message per case with the two buttons; a busy day
degrades gracefully into a grouped digest (chunked to Telegram's 4096-char
limit). Titles and details are HTML-escaped — scraped prose is untrusted
input, and an unescaped `<` must never inject markup.

The same pipeline runs locally or in CI: it needs no credentials, writes
`data/settlements.json` plus the static site into `docs/` for GitHub Pages,
and keeps its disposable SQLite query store under `.state/` (gitignored).
The dataset is deterministic — rebuilding unchanged sources moves at most the
`generated_at` line, so a scheduled run shows up as a reviewable diff rather
than a rewrite of every record (`tests/test_store.py` holds that line).

## Repository layout

```
src/kya/
  http.py          polite, cached, robots-aware client
  sources/         openclassactions index + page parsers
  normalize.py     payout tiers + the L0-L4 proof ladder
  classify.py      lane and kind classification
  score.py         payout tiers S-F, expected value, confidence
  build.py         index line + page facts -> Settlement (page wins, conflicts flagged)
  deadlines.py     deadline parsing, soon/urgent windows
  diff.py          change events between runs (new / payout changed / deadline soon)
  store.py         SQLite store + data/settlements.json export
  site_build.py    Jinja2 -> docs/ static site
  run.py           the kya console entry point
  templates/       index.html.j2 plus static assets (style.css, app.js, favicon)
tests/             221 offline tests over captured live fixtures
tools/             build, fixture capture, and warning-audit CLIs
```

## Roadmap

- [x] Milestone A - pipeline, dataset, static site
- [x] Milestone B - Telegram notifications with inline *Done / Not mine* buttons
  (delivery + CLI; recording button presses lands with the Milestone C webhook)
- [ ] Milestone C - Cloudflare Worker webhook + KV state
- [ ] Scheduled GitHub Actions builds + GitHub Pages deploy
- [ ] Secondary sources (CourtListener, topclassactions RSS) and cross-checks

## Disclaimers

This project is **not legal advice**, and it is not affiliated with any court,
law firm, or settlement administrator. Deadlines and amounts can change; the
official claim portal linked on each row always governs. Scraped prose is
treated as untrusted input: links are stripped and text is sanitised before it
is rendered anywhere.

## License

[MIT](LICENSE)

## Payout tiers and expected value

Claims are stickered S–F by headline value (S ≥ $5,000 … F < $50) and ranked by
a conservative expected value (`src/kya/score.py`):

- unknown household quantities → **1 unit**
- pro-rata claims → **0.05 % of the common fund** (`config.yaml`)
- every estimate carries a confidence level and an `ev_note` explaining the
  derivation; a low-confidence estimate is labelled as one, never presented
  as a promise
