"""
Pwnbox.io RSS Challenge Tracker - Telegram Bot.

Watches https://pwnbox.io/api/challenges/rss.xml and notifies subscribed
Telegram users whenever a new challenge appears.

Run:
    pip install -r requirements.txt
    python bot.py

Then open Telegram, message your bot, and send /start to subscribe.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import feedparser
import telebot

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN_HERE"
RSS_URL = "https://pwnbox.io/api/challenges/rss.xml"
POLL_INTERVAL_SECONDS = 300
SUMMARY_MAX_CHARS = 400
USER_AGENT = "PwnboxTrackerBot/1.0"
STATE_FILE = Path(__file__).resolve().parent / "state.json"

# ---------------------------------------------------------------------------
# Topic tagging
# ---------------------------------------------------------------------------

TOPIC_ORDER = ("ssrf", "xss", "cors", "sqli", "auth", "rce", "other")
TOPIC_LABELS = {
    "ssrf": "SSRF",
    "xss": "XSS",
    "cors": "CORS",
    "sqli": "SQLi",
    "auth": "auth",
    "rce": "RCE",
    "other": "other",
}
TOPIC_PATTERNS = {
    "ssrf": (
        r"\bssrf\b",
        r"server[-\s]+side request forgery",
        r"\bmetadata service\b",
        r"\binternal metadata\b",
        r"\b169\.254\.169\.254\b",
    ),
    "xss": (
        r"\bxss\b",
        r"cross[-\s]+site scripting",
        r"\bscript injection\b",
        r"<\s*script\b",
        r"\bdom\b",
    ),
    "cors": (
        r"\bcors\b",
        r"cross[-\s]+origin",
        r"access[-\s]+control[-\s]+allow[-\s]+origin",
        r"\borigin header\b",
    ),
    "sqli": (
        r"\bsqli\b",
        r"\bsql injection\b",
        r"\bblind sql\b",
        r"\bunion select\b",
        r"\bor\s+1\s*=\s*1\b",
    ),
    "auth": (
        r"\bauth(?:entication|orization)?\b",
        r"\blogin\b",
        r"\bsession\b",
        r"\bjwt\b",
        r"\boauth\b",
        r"access[-\s]+control",
        r"\bidor\b",
        r"privilege escalation",
        r"\badmin panel\b",
    ),
    "rce": (
        r"\brce\b",
        r"remote code execution",
        r"command injection",
        r"\bcode execution\b",
        r"\bshell\b",
        r"\bdeserialization\b",
        r"template injection",
        r"\bssti\b",
    ),
}
TOPIC_ALIASES = {
    "ssrf": "ssrf",
    "xss": "xss",
    "cors": "cors",
    "sqli": "sqli",
    "sql": "sqli",
    "sql-injection": "sqli",
    "auth": "auth",
    "authentication": "auth",
    "authorization": "auth",
    "login": "auth",
    "jwt": "auth",
    "oauth": "auth",
    "rce": "rce",
    "remote-code-execution": "rce",
    "command-injection": "rce",
    "cmd": "rce",
    "other": "other",
    "uncategorized": "other",
    "unclassified": "other",
}
ALL_TOPIC_FILTER_ALIASES = {"all", "any", "everything", "clear", "off", "none"}
TOPIC_ARG_RE = re.compile(r"[\w+-]+")


def sort_topic_keys(topics) -> list[str]:
    topic_set = set(topics)
    ordered = [topic for topic in TOPIC_ORDER if topic in topic_set]
    ordered.extend(sorted(topic_set.difference(TOPIC_ORDER)))
    return ordered


def normalize_topic_keys(topics) -> set[str]:
    return {topic for topic in topics if topic in TOPIC_LABELS}


def format_topic_labels(topics) -> str:
    return ", ".join(
        TOPIC_LABELS.get(topic, topic) for topic in sort_topic_keys(topics)
    )

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pwnbox-bot")

# ---------------------------------------------------------------------------
# Persistent state (subscribers + already-announced entries)
# ---------------------------------------------------------------------------

class State:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.subscribers: set[int] = set()
        self.seen_ids: set[str] = set()
        self.topic_filters: dict[int, set[str]] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.subscribers = set(data.get("subscribers", []))
            self.seen_ids = set(data.get("seen_ids", []))
            self.topic_filters = self._load_topic_filters(data.get("topic_filters", {}))
            log.info(
                "Loaded state: %d subscribers, %d seen entries, %d topic filters",
                len(self.subscribers),
                len(self.seen_ids),
                len(self.topic_filters),
            )
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not load state file (%s); starting fresh", exc)

    def _load_topic_filters(self, raw_filters) -> dict[int, set[str]]:
        filters: dict[int, set[str]] = {}
        if not isinstance(raw_filters, dict):
            return filters
        for chat_id, topics in raw_filters.items():
            try:
                numeric_chat_id = int(chat_id)
            except (TypeError, ValueError):
                continue
            if not isinstance(topics, list):
                continue
            normalized = normalize_topic_keys(topics)
            if normalized:
                filters[numeric_chat_id] = normalized
        return filters

    def _save(self) -> None:
        payload = {
            "subscribers": sorted(self.subscribers),
            "seen_ids": sorted(self.seen_ids),
            "topic_filters": {
                str(chat_id): sort_topic_keys(topics)
                for chat_id, topics in sorted(self.topic_filters.items())
                if topics
            },
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def add_subscriber(self, chat_id: int) -> bool:
        with self._lock:
            if chat_id in self.subscribers:
                return False
            self.subscribers.add(chat_id)
            self._save()
            return True

    def remove_subscriber(self, chat_id: int) -> bool:
        with self._lock:
            if chat_id not in self.subscribers:
                return False
            self.subscribers.discard(chat_id)
            self.topic_filters.pop(chat_id, None)
            self._save()
            return True

    def set_topic_filters(self, chat_id: int, topics: set[str] | None) -> None:
        with self._lock:
            if topics is None:
                self.topic_filters.pop(chat_id, None)
            else:
                normalized = normalize_topic_keys(topics)
                if normalized:
                    self.topic_filters[chat_id] = normalized
                else:
                    self.topic_filters.pop(chat_id, None)
            self._save()

    def get_topic_filters(self, chat_id: int) -> set[str] | None:
        with self._lock:
            filters = self.topic_filters.get(chat_id)
            return set(filters) if filters else None

    def accepts_topics(self, chat_id: int, topics: list[str]) -> bool:
        with self._lock:
            filters = self.topic_filters.get(chat_id)
            if not filters:
                return True
            return bool(filters.intersection(topics))

    def mark_seen(self, ids: list[str]) -> None:
        if not ids:
            return
        with self._lock:
            self.seen_ids.update(ids)
            self._save()

    def is_new(self, key: str) -> bool:
        return key not in self.seen_ids


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
state = State(STATE_FILE)

# ---------------------------------------------------------------------------
# Feed helpers
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()


def infer_topics(entry) -> list[str]:
    text = strip_html(f"{entry.get('title', '')} {entry.get('summary', '')}").lower()
    topics: list[str] = []
    for topic in TOPIC_ORDER:
        if topic == "other":
            continue
        if any(
            re.search(pattern, text, re.IGNORECASE)
            for pattern in TOPIC_PATTERNS[topic]
        ):
            topics.append(topic)
    return topics or ["other"]


def parse_topic_filter_args(raw: str) -> tuple[set[str] | None, list[str]]:
    tokens = TOPIC_ARG_RE.findall(raw.lower())
    if not tokens:
        return set(), []
    if len(tokens) == 1 and tokens[0] in ALL_TOPIC_FILTER_ALIASES:
        return None, []

    topics: set[str] = set()
    unknown: list[str] = []
    for token in tokens:
        topic = TOPIC_ALIASES.get(token)
        if topic:
            topics.add(topic)
        else:
            unknown.append(token)
    return topics, unknown


def command_args(message) -> str:
    text = message.text or ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


def fetch_entries() -> list:
    feed = feedparser.parse(RSS_URL, agent=USER_AGENT)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"Feed parse failed: {feed.bozo_exception}")
    return feed.entries


def entry_key(entry) -> str:
    return entry.get("id") or entry.get("link") or entry.get("title", "")


def format_published(entry) -> str:
    if entry.get("published_parsed"):
        try:
            ts = time.mktime(entry.published_parsed)
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
        except (TypeError, ValueError):
            pass
    return entry.get("published", "")


def render_challenge(
    entry,
    *,
    header: str = "🆕 <b>New Pwnbox Challenge</b>",
    topics: list[str] | None = None,
) -> str:
    title = escape(entry.get("title", "Untitled"))
    link = entry.get("link", "")
    summary = strip_html(entry.get("summary", ""))
    challenge_topics = topics or infer_topics(entry)
    if len(summary) > SUMMARY_MAX_CHARS:
        summary = summary[:SUMMARY_MAX_CHARS].rsplit(" ", 1)[0] + "…"

    lines = [header, "", f"🎯 <b>{title}</b>"]
    lines.append(f"\n🏷 <b>Topics:</b> {escape(format_topic_labels(challenge_topics))}")
    if summary:
        lines.append(f"\n📝 {escape(summary)}")
    published = format_published(entry)
    if published:
        lines.append(f"\n🕒 <i>{escape(published)}</i>")
    if link:
        lines.append(f'\n🔗 <a href="{escape(link)}">Open challenge ↗</a>')
    return "\n".join(lines)


def broadcast(text: str, *, topics: list[str] | None = None) -> None:
    """Send text to every subscriber; prune anyone who blocked the bot."""
    dead: list[int] = []
    for chat_id in list(state.subscribers):
        if topics is not None and not state.accepts_topics(chat_id, topics):
            continue
        try:
            bot.send_message(chat_id, text, disable_web_page_preview=False)
        except telebot.apihelper.ApiTelegramException as exc:
            log.warning("Send to %s failed (%s)", chat_id, exc)
            if exc.error_code in (400, 403):
                dead.append(chat_id)
    for chat_id in dead:
        state.remove_subscriber(chat_id)
        log.info("Removed unreachable subscriber %s", chat_id)


def check_feed(*, notify: bool) -> int:
    try:
        entries = fetch_entries()
    except Exception as exc:
        log.error("Feed fetch failed: %s", exc)
        return 0

    new_entries = [e for e in entries if entry_key(e) and state.is_new(entry_key(e))]
    if not new_entries:
        return 0

    log.info("Found %d new entries", len(new_entries))

    if notify and state.subscribers:
        # RSS lists newest first; reverse so the chat scroll stays chronological.
        for entry in reversed(new_entries):
            topics = infer_topics(entry)
            broadcast(render_challenge(entry, topics=topics), topics=topics)

    state.mark_seen([entry_key(e) for e in new_entries])
    return len(new_entries)


def poller_loop() -> None:
    log.info(
        "Poller started - checking %s every %ds", RSS_URL, POLL_INTERVAL_SECONDS
    )
    while True:
        try:
            check_feed(notify=True)
        except Exception:
            log.exception("Unexpected error in poller iteration")
        time.sleep(POLL_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Telegram commands
# ---------------------------------------------------------------------------

WELCOME = (
    "👋 <b>Welcome to the Pwnbox Tracker</b>\n\n"
    "I watch <a href=\"https://pwnbox.io\">pwnbox.io</a> and ping you the "
    "moment a new challenge drops.\n\n"
    "<b>Commands</b>\n"
    "• /start — subscribe to alerts\n"
    "• /stop — unsubscribe\n"
    "• /latest — show the latest challenge\n"
    "• /topics — show available topics and your filters\n"
    "• /filter ssrf xss — receive only selected topics\n"
    "• /filter all — receive every challenge\n"
    "• /status — show tracker status\n"
    "• /help — show this message"
)


def render_topic_filter_status(chat_id: int) -> str:
    filters = state.get_topic_filters(chat_id)
    current = "all topics" if filters is None else format_topic_labels(filters)
    available = "\n".join(
        f"• <code>{topic}</code> — {escape(label)}"
        for topic, label in TOPIC_LABELS.items()
    )
    return (
        "🏷 <b>Topic Filters</b>\n\n"
        f"Current alerts: <b>{escape(current)}</b>\n\n"
        "<b>Available topics</b>\n"
        f"{available}\n\n"
        "Use <code>/filter ssrf xss auth</code> to receive only those topics.\n"
        "Use <code>/filter all</code> to receive every challenge."
    )


@bot.message_handler(commands=["start"])
def cmd_start(message):
    chat_id = message.chat.id
    user = message.from_user.username or message.from_user.first_name or "anon"
    if state.add_subscriber(chat_id):
        log.info("Subscribed %s (chat_id=%s)", user, chat_id)
        bot.send_message(
            chat_id,
            "✅ <b>Subscribed!</b>\n\nYou'll be the first to know when a new "
            "challenge appears.\n\n" + WELCOME,
            disable_web_page_preview=True,
        )
    else:
        bot.send_message(
            chat_id,
            "👍 You're already subscribed.\n\n" + WELCOME,
            disable_web_page_preview=True,
        )


@bot.message_handler(commands=["stop"])
def cmd_stop(message):
    chat_id = message.chat.id
    if state.remove_subscriber(chat_id):
        bot.send_message(chat_id, "👋 Unsubscribed. Send /start to resume alerts.")
    else:
        bot.send_message(
            chat_id, "ℹ️ You weren't subscribed. Send /start to subscribe."
        )


@bot.message_handler(commands=["help"])
def cmd_help(message):
    bot.send_message(message.chat.id, WELCOME, disable_web_page_preview=True)


@bot.message_handler(commands=["topics", "filters"])
def cmd_topics(message):
    bot.send_message(message.chat.id, render_topic_filter_status(message.chat.id))


@bot.message_handler(commands=["filter", "subscribe_topics"])
def cmd_filter(message):
    chat_id = message.chat.id
    raw_args = command_args(message)
    if not raw_args:
        bot.send_message(chat_id, render_topic_filter_status(chat_id))
        return

    topics, unknown = parse_topic_filter_args(raw_args)
    if unknown:
        bot.send_message(
            chat_id,
            "⚠️ Unknown topic(s): "
            f"<code>{escape(', '.join(unknown))}</code>\n\n"
            + render_topic_filter_status(chat_id),
        )
        return

    state.add_subscriber(chat_id)
    state.set_topic_filters(chat_id, topics)
    if topics is None:
        bot.send_message(
            chat_id,
            "✅ <b>Topic filter cleared.</b>\n\n"
            "You'll receive every new challenge.",
        )
        return

    bot.send_message(
        chat_id,
        "✅ <b>Topic filter updated.</b>\n\n"
        f"You'll receive only: <b>{escape(format_topic_labels(topics))}</b>",
    )


@bot.message_handler(commands=["latest"])
def cmd_latest(message):
    try:
        entries = fetch_entries()
    except Exception as exc:
        bot.send_message(
            message.chat.id,
            f"⚠️ Could not fetch feed:\n<code>{escape(str(exc))}</code>",
        )
        return
    if not entries:
        bot.send_message(message.chat.id, "📭 The feed is empty right now.")
        return
    bot.send_message(
        message.chat.id,
        render_challenge(entries[0], header="📌 <b>Latest Pwnbox Challenge</b>"),
    )


@bot.message_handler(commands=["status"])
def cmd_status(message):
    text = (
        "📊 <b>Tracker Status</b>\n\n"
        f"🌐 Feed: <code>{escape(RSS_URL)}</code>\n"
        f"⏱ Poll interval: <code>{POLL_INTERVAL_SECONDS}s</code>\n"
        f"👥 Subscribers: <code>{len(state.subscribers)}</code>\n"
        f"📚 Tracked entries: <code>{len(state.seen_ids)}</code>"
    )
    bot.send_message(message.chat.id, text)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        me = bot.get_me()
    except Exception as exc:
        log.critical("Bot authentication failed: %s", exc)
        sys.exit(1)
    log.info("Connected as @%s (%s)", me.username, me.first_name)

    # First run: mark everything currently in the feed as seen, so we don't
    # spam users with the entire backlog the moment they /start.
    # Only silence when there are no subscribers yet; if subscribers exist but
    # seen_ids is empty (state loss/reset), notify them normally instead.
    if not state.seen_ids and not state.subscribers:
        log.info("First run: priming seen-set without notifications")
        check_feed(notify=False)

    threading.Thread(target=poller_loop, name="poller", daemon=True).start()

    log.info("Bot is online. Press Ctrl+C to stop.")
    try:
        bot.infinity_polling(timeout=20, long_polling_timeout=15)
    except KeyboardInterrupt:
        log.info("Shutting down")


if __name__ == "__main__":
    main()
