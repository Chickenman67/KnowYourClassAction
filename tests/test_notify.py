"""Telegram delivery - rendering, the callback contract, and the API client.

All tests run offline: the bot's transport is injected, so nothing here
touches the network. The digest is asserted against hand-written expectations
(grouping, escaping, chunking), not against a re-derivation of the renderer.
"""

from __future__ import annotations

import pytest

from kya.diff import DiffEvent, EventKind
from kya import notify


def event(kind=EventKind.NEW, sid="alpha", title="Alpha Settlement", detail=""):
    return DiffEvent(kind, sid, title, detail)


# --- the callback contract between notify.py and the future webhook ----------
def test_callback_data_round_trips_through_parse() -> None:
    data = notify.callback_data("done", "kia-window-regulator")
    assert notify.parse_callback_data(data) == ("done", "kia-window-regulator")


def test_parse_rejects_foreign_malformed_and_empty_payloads() -> None:
    assert notify.parse_callback_data("other:done:x") is None
    assert notify.parse_callback_data("kya:withdraw:x") is None
    assert notify.parse_callback_data("kya:done:") is None
    assert notify.parse_callback_data("just-one-part") is None
    assert notify.parse_callback_data(None) is None


def test_callback_data_raises_rather_than_truncating_an_id() -> None:
    with pytest.raises(ValueError):
        notify.callback_data("done", "x" * 100)


# --- rendering ----------------------------------------------------------------
def test_digest_groups_by_heading_and_escapes_scraped_titles() -> None:
    events = [
        event(EventKind.PAYOUT_CHANGED, "m", "Money & Sense <Ltd>", "100 -> 250"),
        event(EventKind.NEW, "n", "New Case"),
        event(EventKind.DEADLINE_SOON, "s", "Soon Case", "closes 2026-09-20"),
        event(EventKind.REMOVED, "g", "Gone Case"),
    ]
    messages = notify.render_digest(events)
    assert len(messages) == 1
    text = messages[0]
    # removals are bookkeeping, not news
    assert "Gone Case" not in text and "Delisted" not in text
    # grouping: one heading per kind, urgent first
    assert text.index("Closing soon") < text.index("Money changed") < text.index(
        "New to the ledger"
    )
    # everything from a scrape is escaped for parse_mode=HTML
    assert "Money &amp; Sense &lt;Ltd&gt;" in text
    # the not-legal-advice footer survives
    assert "Not legal advice" in text


def test_empty_events_produce_no_messages() -> None:
    assert notify.render_digest([]) == []
    assert notify.render_digest([event(EventKind.REMOVED, "g", "Gone")]) == []


def test_counts_in_the_heading_match_actionable_events() -> None:
    events = [event(EventKind.NEW, "a"), event(EventKind.NEW, "b"), event(EventKind.REMOVED, "c")]
    text = notify.render_digest(events)[0]
    assert "2 updates" in text


def test_a_very_busy_day_chunks_into_several_messages() -> None:
    events = [event(EventKind.NEW, f"case-{i:03d}", f"Case number {i:03d} " + "x" * 60)
              for i in range(80)]
    messages = notify.render_digest(events)
    assert len(messages) > 1
    for message in messages:
        assert len(message) <= notify.MESSAGE_LIMIT_CHARS
    # nothing is dropped across the chunk boundary
    joined = "".join(messages)
    for i in range(80):
        assert f"Case number {i:03d}" in joined


# --- the API client, against a fake transport ---------------------------------
class FakeTelegram:
    """Records calls and replays canned Telegram responses. No network."""

    def __init__(self, fail_method: str | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.fail_method = fail_method

    def __call__(self, method: str, payload: dict) -> dict:
        self.calls.append((method, payload))
        if method == self.fail_method:
            return {"ok": False, "description": f"{method} refused"}
        if method == "sendMessage":
            assert payload["parse_mode"] == "HTML"
            return {"ok": True, "result": {"message_id": 42, "chat": {"id": 123}}}
        if method == "getMe":
            return {"ok": True, "result": {"id": 1, "username": "Didndixjxhdujs_bot"}}
        if method == "getUpdates":
            return {
                "ok": True,
                "result": [
                    {"message": {"chat": {"id": 555, "type": "private", "first_name": "David"}}},
                    {"message": {"chat": {"id": 555, "type": "private", "first_name": "David"}}},
                ],
            }
        if method in ("setWebhook", "answerCallbackQuery", "editMessageReplyMarkup"):
            return {"ok": True, "result": {}}
        raise AssertionError(f"unexpected method {method}")


def test_send_message_sends_html_and_inline_buttons() -> None:
    fake = FakeTelegram()
    bot = notify.TelegramBot("token", transport=fake)
    sent = bot.send_message(
        123, "hello <b>world</b>", buttons=[("done", "abc"), ("skip", "abc")]
    )
    assert sent.message_id == 42 and sent.chat_id == 123
    method, payload = fake.calls[0]
    assert method == "sendMessage"
    keyboard = payload["reply_markup"]["inline_keyboard"]
    assert keyboard[0][0] == {"text": "Done", "callback_data": "kya:done:abc"}
    assert keyboard[1][0] == {"text": "Not mine", "callback_data": "kya:skip:abc"}


def test_api_refusal_raises_telegram_error_not_a_crash() -> None:
    bot = notify.TelegramBot("token", transport=FakeTelegram(fail_method="sendMessage"))
    with pytest.raises(notify.TelegramError):
        bot.send_message(123, "hi")


def test_a_missing_token_is_refused_before_any_network_call() -> None:
    with pytest.raises(notify.TelegramError):
        notify.TelegramBot("")


def test_whoami_lists_each_chat_once() -> None:
    fake = FakeTelegram()
    lines: list[str] = []
    assert notify.cmd_whoami(notify.TelegramBot("token", transport=fake), out=lines.append) == 0
    joined = "\n".join(lines)
    assert "@Didndixjxhdujs_bot" in joined
    assert joined.count("555") == 1, "duplicate messages must not duplicate the chat"
    assert "no chats" not in joined


def test_whoami_with_no_chats_says_so_instead_of_failing() -> None:
    fake = FakeTelegram()

    def no_updates(method, payload):
        fake.calls.append((method, payload))
        if method == "getUpdates":
            return {"ok": True, "result": []}
        return {"ok": True, "result": {"id": 1, "username": "x_bot"}}

    lines: list[str] = []
    assert notify.cmd_whoami(notify.TelegramBot("token", transport=no_updates), out=lines.append) == 0
    assert "no chats seen yet" in "\n".join(lines)


def test_send_test_message_delivers_with_both_buttons() -> None:
    fake = FakeTelegram()
    lines: list[str] = []
    status = notify.cmd_send_test_message(
        notify.TelegramBot("token", transport=fake), 999, out=lines.append
    )
    assert status == 0
    assert "delivered: message id 42" in "\n".join(lines)
    keyboard = fake.calls[0][1]["reply_markup"]["inline_keyboard"]
    assert [b["text"] for b in (row[0] for row in keyboard)] == ["Done", "Not mine"]


# --- per-event messages with buttons (small digests) ---------------------------
def test_digest_below_threshold_sends_one_buttoned_message_per_event() -> None:
    fake = FakeTelegram()
    bot = notify.TelegramBot("token", transport=fake)
    events = [
        event(EventKind.NEW, "case-a", "Case A"),
        event(EventKind.PAYOUT_CHANGED, "case-b", "Case B", "100 -> 250"),
    ]
    lines: list[str] = []
    status = notify.cmd_notify_digest_events(bot, 123, events, out=lines.append)
    assert status == 0
    sends = [c for c in fake.calls if c[0] == "sendMessage"]
    assert len(sends) == 2
    for method, payload in sends:
        keyboard = payload["reply_markup"]["inline_keyboard"]
        assert [b["text"] for b in (row[0] for row in keyboard)] == ["Done", "Not mine"]
    assert "with buttons" in "\n".join(lines)


def test_digest_above_threshold_sends_a_grouped_digest_without_buttons() -> None:
    fake = FakeTelegram()
    bot = notify.TelegramBot("token", transport=fake)
    events = [event(EventKind.NEW, f"case-{i:03d}", f"Case number {i:03d}") for i in range(8)]
    lines: list[str] = []
    status = notify.cmd_notify_digest_events(bot, 123, events, out=lines.append)
    assert status == 0
    sends = [c for c in fake.calls if c[0] == "sendMessage"]
    assert len(sends) >= 1
    assert all("reply_markup" not in payload for _, payload in sends)
    assert "covering" in "\n".join(lines)


# --- credentials ---------------------------------------------------------------
def test_credentials_come_from_the_environment_only() -> None:
    token, chat_id = notify.load_credentials(
        {"KYA_TELEGRAM_BOT_TOKEN": "  abc  ", "KYA_TELEGRAM_CHAT_ID": "77"}
    )
    assert (token, chat_id) == ("abc", "77")


# --- Milestone C: the decisions loop -------------------------------------------
def settlement_fixture(sid: str, title: str):
    from kya.models import Settlement

    return Settlement(id=sid, source_url="https://example.com", title=title, tiers=[])


def test_digest_skips_cases_already_decided_by_the_worker() -> None:
    fake = FakeTelegram()
    bot = notify.TelegramBot("token", transport=fake)
    settlements = [
        settlement_fixture("case-a", "Decided Case"),
        settlement_fixture("case-b", "Fresh Case"),
    ]
    lines: list[str] = []
    status = notify.cmd_notify_digest(
        bot,
        123,
        settlements,
        previous={},
        decisions={"case-a": {"action": "done"}},
        out=lines.append,
    )
    assert status == 0
    assert "skipping 1 case(s) already decided" in "\n".join(lines)
    sends = [c for c in fake.calls if c[0] == "sendMessage"]
    assert len(sends) == 1
    assert "Fresh Case" in sends[0][1]["text"]
    assert "Decided Case" not in sends[0][1]["text"]


def test_digest_degrades_to_unfiltered_when_the_worker_is_down(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise notify.TelegramError("HTTP 503 from https://w.test/decisions")

    monkeypatch.setattr(notify, "fetch_decisions", boom)
    fake = FakeTelegram()
    bot = notify.TelegramBot("token", transport=fake)
    lines: list[str] = []
    status = notify.cmd_notify_digest(
        bot, 123, [settlement_fixture("case-a", "Case A")], previous={}, out=lines.append
    )
    assert status == 0, "a decisions outage must not silence the digest"
    assert "decisions unavailable" in "\n".join(lines)
    assert any(c[0] == "sendMessage" for c in fake.calls)


def test_fetch_decisions_reads_the_worker_json_and_fails_loudly() -> None:
    calls: list[tuple[str, dict]] = []

    def getter(url: str, headers: dict) -> tuple[int, str]:
        calls.append((url, headers))
        return 200, '{"ok": true, "decisions": {"case-a": {"action": "done"}}}'

    env = {notify.ENV_DECISIONS_URL: "https://w.test/decisions", notify.ENV_WEBHOOK_SECRET: "s"}
    assert notify.fetch_decisions(env, getter=getter) == {"case-a": {"action": "done"}}
    assert calls == [("https://w.test/decisions", {"Authorization": "Bearer s"})]

    with pytest.raises(notify.TelegramError, match="HTTP 403"):
        notify.fetch_decisions(env, getter=lambda url, headers: (403, "forbidden"))

    with pytest.raises(notify.TelegramError, match="non-JSON"):
        notify.fetch_decisions(env, getter=lambda url, headers: (200, "<html>"))


def test_fetch_decisions_with_no_url_configured_never_touches_the_network() -> None:
    def must_not_fetch(url, headers):
        raise AssertionError("no URL configured - nothing to fetch")

    assert notify.fetch_decisions({notify.ENV_WEBHOOK_SECRET: "s"}, getter=must_not_fetch) == {}


def test_set_webhook_registers_the_url_with_the_shared_secret() -> None:
    fake = FakeTelegram()
    bot = notify.TelegramBot("token", transport=fake)
    lines: list[str] = []
    env = {notify.ENV_TOKEN: "t", notify.ENV_WEBHOOK_SECRET: "shh"}
    status = notify.cmd_set_webhook(bot, "https://w.workers.dev/", environ=env, out=lines.append)
    assert status == 0
    assert fake.calls == [
        (
            "setWebhook",
            {"url": "https://w.workers.dev/telegram", "secret_token": "shh"},
        )
    ]
    assert "webhook set: https://w.workers.dev/telegram" in "\n".join(lines)


def test_set_webhook_without_a_secret_is_refused_before_any_call() -> None:
    bot = notify.TelegramBot("token", transport=FakeTelegram())
    with pytest.raises(notify.TelegramError, match="KYA_TELEGRAM_WEBHOOK_SECRET"):
        notify.cmd_set_webhook(bot, "https://w.workers.dev", environ={notify.ENV_TOKEN: "t"})


def test_decisions_cli_lists_what_the_worker_recorded() -> None:
    lines: list[str] = []
    env = {notify.ENV_TOKEN: "t", notify.ENV_DECISIONS_URL: "https://w.test/decisions"}
    status = notify.cmd_decisions(
        environ=env,
        getter=lambda url, headers: (200, '{"decisions": {"case-a": {"action": "done"}}}'),
        out=lines.append,
    )
    assert status == 0
    joined = "\n".join(lines)
    assert "case-a: done" in joined
    assert "1 recorded" in joined


def test_decisions_cli_with_nothing_configured_says_so() -> None:
    lines: list[str] = []
    status = notify.cmd_decisions(environ={notify.ENV_TOKEN: "t"}, out=lines.append)
    assert status == 0
    assert "KYA_DECISIONS_URL not set" in "\n".join(lines)


def test_decisions_cli_with_a_configured_worker_names_it_when_empty() -> None:
    lines: list[str] = []
    env = {notify.ENV_TOKEN: "t", notify.ENV_DECISIONS_URL: "https://w.test/decisions"}
    status = notify.cmd_decisions(
        environ=env, getter=lambda url, headers: (200, '{"ok": true, "decisions": {}}'),
        out=lines.append,
    )
    assert status == 0
    assert "none recorded yet (https://w.test/decisions)" in "\n".join(lines)


def test_a_blank_token_is_an_actionable_error() -> None:
    with pytest.raises(notify.TelegramError, match="KYA_TELEGRAM_BOT_TOKEN"):
        notify.load_credentials({})


def test_chat_id_is_optional() -> None:
    token, chat_id = notify.load_credentials({"KYA_TELEGRAM_BOT_TOKEN": "abc"})
    assert chat_id is None