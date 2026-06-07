import tempfile
from pathlib import Path
import sys
import types
import unittest

feedparser_stub = types.SimpleNamespace(parse=lambda *args, **kwargs: None)


class TeleBotStub:
    def __init__(self, *args, **kwargs):
        pass

    def message_handler(self, *args, **kwargs):
        def decorator(func):
            return func

        return decorator


class ApiTelegramExceptionStub(Exception):
    error_code = 500


telebot_stub = types.SimpleNamespace(
    TeleBot=TeleBotStub,
    apihelper=types.SimpleNamespace(ApiTelegramException=ApiTelegramExceptionStub),
)

sys.modules.setdefault("feedparser", feedparser_stub)
sys.modules.setdefault("telebot", telebot_stub)

import bot


class TopicInferenceTests(unittest.TestCase):
    def test_infers_multiple_topics_from_title_and_summary(self):
        entry = {
            "title": "Blind SSRF callback",
            "summary": "Abuse a permissive CORS policy to read internal metadata.",
        }

        self.assertEqual(bot.infer_topics(entry), ["ssrf", "cors"])

    def test_renders_topic_tags_in_challenge_message(self):
        entry = {
            "title": "Login bypass with SQL injection",
            "summary": "A small auth bug exposes the admin panel.",
            "link": "https://pwnbox.io/challenges/example",
        }

        message = bot.render_challenge(entry)

        self.assertIn("🏷 <b>Topics:</b> SQLi, auth", message)


class TopicFilterTests(unittest.TestCase):
    def test_parses_topic_filter_arguments_case_insensitively(self):
        topics, unknown = bot.parse_topic_filter_args("SSRF xss sql")

        self.assertEqual(topics, {"ssrf", "xss", "sqli"})
        self.assertEqual(unknown, [])

    def test_parses_all_as_no_topic_filter(self):
        topics, unknown = bot.parse_topic_filter_args("all")

        self.assertIsNone(topics)
        self.assertEqual(unknown, [])

    def test_persists_per_subscriber_topic_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = bot.State(path)
            state.add_subscriber(123)
            state.set_topic_filters(123, {"xss", "auth"})

            reloaded = bot.State(path)

        self.assertEqual(reloaded.get_topic_filters(123), {"auth", "xss"})
        self.assertTrue(reloaded.accepts_topics(123, ["auth"]))
        self.assertFalse(reloaded.accepts_topics(123, ["rce"]))


if __name__ == "__main__":
    unittest.main()
