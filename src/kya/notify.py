"""Telegram delivery for the diff engine's events.

Design decisions worth knowing before changing anything here:

* **Messages are built from typed :class:`~kya.diff.DiffEvent`s, never from raw
  source prose.** A settlement is only worth a notification because something
  discrete happened - it is new, the money changed, the deadline moved or is
  about to close. Reworded descriptions must never wake anyone up.
* **Everything from a scraped page is escaped.** Titles and details come from
  a third party's HTML, and we send with ``parse_mode=HTML``; an unescaped
  ``<`` would either break the message or let a hostile page inject markup.
* **The transport is injected.** Every test in this module runs offline against
  a fake; no test in this repo touches the network.
* **The token never touches config.yaml.** ``config.yaml`` is committed to a
  public repository, so credentials are read from the environment only.
* **Buttons carry a decision, not state.** "Done" / "Not mine" send the case id
  and the action back as callback data; recording that decision is the webhook
  worker's job (Milestone C). :func:`parse_callback_data` is the contract
  between the two.
* **An id too long for a button is aliased, never truncated.** Telegram caps
  ``callback_data`` at 64 bytes, and this project's descriptive slugs grow
  past that - one reached 60 characters, and the ``kya:done:`` prefix spends 9
  more. :func:`decision_key` substitutes a 16-hex-character digest of the id:
  stable, collision-safe at this scale, derived identically on both sides of
  the contract, so a press still resolves to exactly one case. Truncating would
  map a press onto whichever case shares the prefix, which is why that was
  never an option.
"""

from __future__ import annotations

import hashlib
import html
import json
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Sequence

from kya.diff import DiffEvent, EventKind
from kya.models import Settlement
from kya.site_build import money_sticker

ENV_TOKEN = "KYA_TELEGRAM_BOT_TOKEN"
ENV_CHAT_ID = "KYA_TELEGRAM_CHAT_ID"
ENV_WEBHOOK_SECRET = "KYA_TELEGRAM_WEBHOOK_SECRET"
ENV_DECISIONS_URL = "KYA_DECISIONS_URL"

API_ROOT = "https://api.telegram.org"

# Telegram's own hard limits. Exceeding either is a silent failure at best.
CALLBACK_LIMIT_BYTES = 64
MESSAGE_LIMIT_CHARS = 4096

CALLBACK_NAMESPACE = "kya"
ACTIONS: dict[str, str] = {"done": "Done", "skip": "Not mine"}

# Groups are rendered in this order: what is about to expire outranks what is
# merely new, because the urgent item is the one with a cost to ignoring it.
_HEADINGS: list[tuple[EventKind, str]] = [
    (EventKind.DEADLINE_SOON, "Closing soon"),
    (EventKind.PAYOUT_CHANGED, "Money changed"),
    (EventKind.DEADLINE_EXTENDED, "Deadline extended"),
    (EventKind.DEADLINE_MOVED_UP, "Deadline moved up"),
    (EventKind.DEADLINE_PASSED, "Closed"),
    (EventKind.LANE_CHANGED, "Moved lane"),
    (EventKind.NEW, "New to the ledger"),
    (EventKind.REMOVED, "Delisted"),
]

PROOF_SHORT = {
    "L0": "automatic",
    "L1": "no proof",
    "L2": "notice ID",
    "L3": "documents",
    "L4": "dual-tier",
}

FOOTER = (
    "Not legal advice. Always check the official claim portal before relying "
    "on an amount or a date."
)


class TelegramError(RuntimeError):
    """The Telegram API refused a call, or could not be reached."""


def escape(text: object) -> str:
    """Escape scraped text for ``parse_mode=HTML``."""
    return html.escape("" if text is None else str(text), quote=False)


# What a button payload may spend on the id, i.e. everything left of it in
# "kya:<action>:<id>". The longest action name is measured so that adding a
# longer one can never quietly eat an id's room.
CALLBACK_ID_BUDGET = CALLBACK_LIMIT_BYTES - len(
    f"{CALLBACK_NAMESPACE}:{max(ACTIONS, key=len)}:"
)

# Characters in an aliased id. 16 hex characters is 64 bits: across the ~400
# ids in this dataset a collision is a 1-in-2^58 event, and because both sides
# of the contract derive the alias from the same function, nothing has to be
# stored in order to resolve one.
ALIAS_CHARS = 16


def decision_key(settlement_id: str) -> str:
    '''The id a decision is shipped and recorded under.

    An id that fits the callback budget is shipped whole, so every decision the
    webhook has already recorded keeps resolving. A longer one is replaced by a
    stable digest of itself - an alias, not a truncation: it names exactly one
    case, where a prefix would name whichever case happens to share it.
    '''
    if len(settlement_id.encode("utf-8")) <= CALLBACK_ID_BUDGET:
        return settlement_id
    return hashlib.sha256(settlement_id.encode("utf-8")).hexdigest()[:ALIAS_CHARS]


def callback_data(action: str, settlement_id: str) -> str:
    '''The button payload for one decision.

    The raise is the last resort: unreachable while :func:`decision_key` keeps
    the id inside the budget, and the guard that makes any future change to
    that arithmetic fail here at build time rather than at Telegram.
    '''
    data = f"{CALLBACK_NAMESPACE}:{action}:{decision_key(settlement_id)}"
    if len(data.encode("utf-8")) > CALLBACK_LIMIT_BYTES:
        raise ValueError(
            f"callback_data would be {len(data.encode('utf-8'))} bytes "
            f"(limit {CALLBACK_LIMIT_BYTES}): {settlement_id!r}"
        )
    return data


def parse_callback_data(data: str | None) -> tuple[str, str] | None:
    """Split button data back into ``(action, settlement_id)``.

    Returns ``None`` for anything that is not ours, so the webhook can ignore
    stray or malformed presses instead of raising at the user.
    """
    if not data:
        return None
    parts = data.split(":", 2)
    if len(parts) != 3:
        return None
    namespace, action, settlement_id = parts
    if namespace != CALLBACK_NAMESPACE or action not in ACTIONS or not settlement_id:
        return None
    return action, settlement_id


# --------------------------------------------------------------------------
# Rendering: DiffEvents -> Telegram HTML, chunked to the message limit
# --------------------------------------------------------------------------

_EVENT_ICONS: dict[EventKind, str] = {
    EventKind.DEADLINE_SOON: "⏳",
    EventKind.PAYOUT_CHANGED: "💰",
    EventKind.DEADLINE_EXTENDED: "📅",
    EventKind.DEADLINE_MOVED_UP: "⚠️",
    EventKind.DEADLINE_PASSED: "⛔",
    EventKind.LANE_CHANGED: "🔀",
    EventKind.NEW: "🆕",
    EventKind.REMOVED: "🗑",
}


def format_event(event: DiffEvent) -> str:
    """One bullet for one event. Everything scraped is escaped."""
    icon = _EVENT_ICONS[event.kind]
    title = escape(event.title)
    if event.detail:
        return f"{icon} {title} — <i>{escape(event.detail)}</i>"
    return f"{icon} {title}"


def render_digest(events: Sequence[DiffEvent], *, count: int | None = None) -> list[str]:
    """Group actionable events into one or more HTML messages.

    Removals are dropped here (they are bookkeeping, per the diff engine's own
    contract). Messages are split at ``MESSAGE_LIMIT_CHARS`` so a busy poll
    day degrades into several messages instead of a silent Telegram 400.
    """
    actionable = [e for e in events if e.is_actionable]
    if not actionable:
        return []

    heading = (
        f"📣 <b>KnowYourClassAction</b> — "
        f"{len(actionable) if count is None else count} update"
        f"{'s' if (len(actionable) if count is None else count) != 1 else ''}\n"
    )

    by_kind: dict[EventKind, list[DiffEvent]] = {}
    for event in actionable:
        by_kind.setdefault(event.kind, []).append(event)

    blocks: list[str] = []
    for kind, heading_text in _HEADINGS:
        group = by_kind.get(kind)
        if not group:
            continue
        lines = "\n".join(format_event(e) for e in group)
        blocks.append(f"<b>{heading_text}</b>\n{lines}")

    body = "\n\n".join(blocks)
    footer = f"\n\n<i>{FOOTER}</i>"

    # Chunk on block boundaries where possible; a single over-long block is
    # hard-split as a last resort so nothing is silently dropped.
    messages: list[str] = []
    current = heading
    for block in blocks:
        candidate = (current + "\n\n" + block) if current != heading else heading + block
        if len(candidate) + len(footer) <= MESSAGE_LIMIT_CHARS:
            current = candidate
            continue
        if current != heading:
            messages.append(current + footer)
            current = heading + block
        else:
            # One block alone exceeds the limit: split it line-wise.
            for line in block.split("\n"):
                candidate = current + ("\n" if current != heading else "") + line
                if len(candidate) + len(footer) <= MESSAGE_LIMIT_CHARS:
                    current = candidate
                else:
                    messages.append(current + footer)
                    current = heading + line
    if current != heading:
        messages.append(current + footer)
    return messages


def render_event_message(event: DiffEvent) -> str:
    """A standalone message for one event, with footer - used for small digests."""
    return f"{format_event(event)}\n\n<i>{FOOTER}</i>"


# Below this many actionable events each case gets its own message with
# Done / Not mine buttons; above it, a grouped digest (buttons per case would
# be unreadable at scale, and the decisions are recorded by the Milestone C
# webhook either way).
INDIVIDUAL_MESSAGE_THRESHOLD = 5


def digest_count(events: Sequence[DiffEvent]) -> int:
    """How many actionable events a digest covers (for the heading)."""
    return sum(1 for e in events if e.is_actionable)


# --------------------------------------------------------------------------
# Transport: the only place that talks to api.telegram.org
# --------------------------------------------------------------------------


@dataclass
class SentMessage:
    message_id: int
    chat_id: int | str


Transport = Callable[[str, dict], dict]
"""``(method, payload) -> full decoded Telegram response body`` (with ``ok``).

Injected in tests; refusals are handled identically no matter who provides it.
"""


def _requests_transport(token: str) -> Transport:
    """The real transport. Only this function knows about ``requests``."""
    import requests

    def call(method: str, payload: dict) -> dict:
        url = f"{API_ROOT}/bot{token}/{method}"
        try:
            response = requests.post(url, json=payload, timeout=15)
        except requests.RequestException as exc:
            name = exc.__class__.__name__
            raise TelegramError(f"{method}: could not reach Telegram ({name})") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise TelegramError(
                f"{method}: non-JSON response (HTTP {response.status_code})"
            ) from exc
        return body

    return call


class TelegramBot:
    """Thin API client. The transport is injectable so tests never touch net."""

    def __init__(self, token: str, transport: Transport | None = None):
        if not token:
            raise TelegramError("no bot token configured")
        self._call = transport or _requests_transport(token)

    def _execute(self, method: str, payload: dict) -> dict:
        """Send one call and unwrap ``result``, refusing API errors loudly."""
        body = self._call(method, payload)
        if not body.get("ok"):
            description = body.get("description", "unknown error")
            raise TelegramError(f"{method} failed: {description}")
        return body["result"]

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        buttons: list[tuple[str, str]] | None = None,
        disable_preview: bool = True,
    ) -> SentMessage:
        payload: dict = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": ACTIONS[action], "callback_data": callback_data(action, sid)}]
                    for action, sid in buttons
                ]
            }
        result = self._execute("sendMessage", payload)
        return SentMessage(message_id=result["message_id"], chat_id=result["chat"]["id"])

    def get_me(self) -> dict:
        return self._execute("getMe", {})

    def recent_updates(self) -> list[dict]:
        """Updates for --whoami chat discovery (no offset bookkeeping needed)."""
        return self._execute("getUpdates", {"limit": 100})

    def set_webhook(self, url: str, *, secret_token: str | None = None) -> None:
        """Point Telegram's webhook at the worker (Milestone C)."""
        payload: dict = {"url": url}
        if secret_token:
            payload["secret_token"] = secret_token
        self._execute("setWebhook", payload)

    def answer_callback_query(self, callback_query_id: str, text: str) -> None:
        """Stop the button's spinner and show the user a toast."""
        self._execute(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text},
        )

    def clear_message_buttons(self, chat_id, message_id: int) -> None:
        """Strip the inline keyboard once a decision is recorded."""
        self._execute(
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": []},
            },
        )


# --------------------------------------------------------------------------
# Credentials: env first, .env file as fallback. Never config.yaml.
# --------------------------------------------------------------------------


def load_credentials(environ: Mapping[str, str] | None = None) -> tuple[str, str | None]:
    """Read the bot token (required) and chat id (optional) from the environment.

    Raises with an actionable message instead of a bare KeyError: the common
    failure is a missing ``.env`` export, not a typo'd variable.
    """
    import os

    env = environ if environ is not None else os.environ
    token = env.get(ENV_TOKEN, "").strip()
    if not token:
        raise TelegramError(f"{ENV_TOKEN} is not set — put it in .env (see .env.example)")
    chat_id = env.get(ENV_CHAT_ID, "").strip() or None
    return token, chat_id


def _parse_env_file(path) -> dict[str, str]:
    """Minimal .env loader: KEY=VALUE lines, # comments, optional quotes."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def merge_environ(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Process environment overlaid with the repo .env when the token is absent.

    Every credential lookup goes through this, so a command body only ever sees
    one merged mapping and never has to know where a value came from.
    """
    import os

    from kya.config import find_repo_root

    env = dict(environ if environ is not None else os.environ)
    if ENV_TOKEN not in env or not str(env.get(ENV_TOKEN, "")).strip():
        env_file = find_repo_root() / ".env"
        if env_file.is_file():
            env.update(_parse_env_file(env_file))
    return env


def bootstrap(environ: Mapping[str, str] | None = None) -> tuple[TelegramBot, str | None]:
    """Token + chat id from the process env, falling back to the repo .env."""
    token, chat_id = load_credentials(merge_environ(environ))
    return TelegramBot(token), chat_id


# --------------------------------------------------------------------------
# CLI command bodies (wired into kya.run)
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Decisions (Milestone C): read the state the webhook worker records
# --------------------------------------------------------------------------


def fetch_decisions(
    environ: Mapping[str, str] | None = None, *, getter=None
) -> dict[str, dict]:
    """Decisions recorded by the webhook worker, keyed by settlement id.

    Returns ``{}`` when ``KYA_DECISIONS_URL`` is unset, so a build without the
    worker still runs. The bearer token is the same webhook secret the worker
    already guards Telegram updates with - one secret, two doors.
    """
    import os

    env = environ if environ is not None else os.environ
    url = (env.get(ENV_DECISIONS_URL) or "").strip()
    if not url:
        return {}
    token = (env.get(ENV_WEBHOOK_SECRET) or "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if getter is None:
        import requests

        def getter(target: str, request_headers: dict) -> tuple[int, str]:
            response = requests.get(target, headers=request_headers, timeout=15)
            return response.status_code, response.text

    status, text = getter(url, headers)
    if status != 200:
        raise TelegramError(f"decisions fetch failed: HTTP {status} from {url}")
    try:
        body = json.loads(text)
    except ValueError as exc:
        raise TelegramError(f"decisions fetch: non-JSON response from {url}") from exc
    decisions = body.get("decisions") if isinstance(body, dict) else None
    return decisions if isinstance(decisions, dict) else {}


def cmd_whoami(bot: TelegramBot, out=print) -> int:
    """Print the bot identity and any chat that has messaged it recently."""
    me = bot.get_me()
    out(f"bot: @{me.get('username')} (id {me.get('id')})")
    updates = bot.recent_updates()
    chats: dict = {}
    for update in updates:
        message = update.get("message") or update.get("edited_message") or {}
        callback = update.get("callback_query") or {}
        chat = message.get("chat") or (callback.get("message") or {}).get("chat")
        if chat and "id" in chat:
            label = chat.get("title") or " ".join(
                filter(None, [chat.get("first_name"), chat.get("last_name")])
            )
            chats[chat["id"]] = f"  {chat['id']}  ({chat.get('type')}) {label}".rstrip()
    if chats:
        out("chats that have messaged this bot (use the id as KYA_TELEGRAM_CHAT_ID):")
        for line in chats.values():
            out(line)
    else:
        out("no chats seen yet - open the bot in Telegram and press Start, then rerun.")
    return 0


def cmd_send_test_message(bot: TelegramBot, chat_id, out=print) -> int:
    buttons = [("done", "example-settlement"), ("skip", "example-settlement")]
    sent = bot.send_message(chat_id, "✅ <b>KnowYourClassAction</b> test message", buttons=buttons)
    out(f"delivered: message id {sent.message_id}")
    return 0


def cmd_set_webhook(bot: TelegramBot, base_url: str, *, environ=None, out=print) -> int:
    """Register the worker as Telegram's webhook, with the shared secret."""
    env = merge_environ(environ)
    secret = (env.get(ENV_WEBHOOK_SECRET) or "").strip()
    if not secret:
        raise TelegramError(
            f"{ENV_WEBHOOK_SECRET} is not set - generate one (python -c "
            '"import secrets; print(secrets.token_urlsafe(32))") and put it in .env'
        )
    url = base_url.rstrip("/") + "/telegram"
    bot.set_webhook(url, secret_token=secret)
    out(f"webhook set: {url}")
    return 0


def cmd_decisions(*, environ=None, getter=None, out=print) -> int:
    """Print the decisions recorded by the webhook worker."""
    env = merge_environ(environ)
    url = (env.get(ENV_DECISIONS_URL) or "").strip()
    decisions = fetch_decisions(env, getter=getter)
    if not decisions:
        if url:
            out(f"decisions: none recorded yet ({url})")
        else:
            out("decisions: KYA_DECISIONS_URL not set - nothing to read")
        return 0
    for settlement_id, record in sorted(decisions.items()):
        action = record.get("action", "?") if isinstance(record, dict) else record
        out(f"  {settlement_id}: {action}")
    out(f"decisions: {len(decisions)} recorded")
    return 0


def cmd_notify_digest(
    bot: TelegramBot,
    chat_id,
    settlements,
    *,
    previous: dict | None = None,
    out=print,
    db_path=None,
    decisions: Mapping[str, dict] | None = None,
) -> int:
    """Diff the stored snapshot against this build and deliver the news.

    ``previous`` is the snapshot as it existed *before* this run's upsert;
    passing it is essential when the caller has already saved, or every diff
    comes back empty. When omitted it is loaded from the store.

    ``decisions`` (the webhook worker's recorded Done / Not mine presses)
    filters cases the user has already ruled on. When not supplied it is
    fetched if ``KYA_DECISIONS_URL`` is configured; a worker outage degrades
    to an unfiltered digest rather than silence.
    """
    from kya.diff import diff_snapshots
    from kya.store import connect as store_connect, load_snapshot

    if previous is None:
        if db_path is None:
            from kya.config import find_repo_root

            db_path = find_repo_root() / ".state" / "kya.sqlite3"
        previous = load_snapshot(store_connect(db_path))
    if not previous:
        # A fresh store is a baseline, not news: without this guard the first
        # scheduled run would report every case in the index as NEW.
        out(
            f"digest: baseline established ({len(settlements)} cases); "
            "the first run stores a baseline instead of reporting every case as new"
        )
        return 0
    current = [s.model_dump(mode="json") for s in settlements]
    from kya.config import load_config

    soon_days = load_config().deadlines.soon_days
    events = diff_snapshots(previous, current, soon_days=soon_days)
    if decisions is None:
        try:
            decisions = fetch_decisions()
        except TelegramError as exc:
            out(f"digest: decisions unavailable ({exc}); sending unfiltered")
            decisions = {}
    # Looked up through decision_key, not the raw id: the worker stores whatever
    # id the button shipped, which is an alias for an over-long one.
    decided = [e for e in events if decision_key(e.settlement_id) in decisions]
    if decided:
        out(f"digest: skipping {len(decided)} case(s) already decided")
        events = [e for e in events if decision_key(e.settlement_id) not in decisions]
    return cmd_notify_digest_events(bot, chat_id, events, out=out)


def cmd_notify_digest_events(
    bot: TelegramBot,
    chat_id,
    events: Sequence[DiffEvent],
    *,
    out=print,
) -> int:
    """Deliver already-diffed events.

    Below :data:`INDIVIDUAL_MESSAGE_THRESHOLD` each case gets its own message
    with Done / Not mine buttons; above it, one grouped digest — buttons per
    case would be unreadable at scale, and the decisions are recorded by the
    Milestone C webhook either way.
    """
    actionable = [e for e in events if e.is_actionable]
    if not actionable:
        out("digest: nothing actionable to report")
        return 0
    if len(actionable) <= INDIVIDUAL_MESSAGE_THRESHOLD:
        for item in actionable:
            bot.send_message(
                chat_id,
                render_event_message(item),
                buttons=[("done", item.settlement_id), ("skip", item.settlement_id)],
            )
        out(f"digest: sent {len(actionable)} message(s) with buttons")
        return 0
    messages = render_digest(events, count=len(actionable))
    for message in messages:
        bot.send_message(chat_id, message)
    out(f"digest: sent {len(messages)} message(s) covering {len(actionable)} events")
    return 0
