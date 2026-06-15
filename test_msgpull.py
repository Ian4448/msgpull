"""Tests for msgpull. Pure stdlib (unittest) — no install needed.

Run from the repo root:
    python3 -m unittest
"""

import json
import struct
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import msgpull
from msgpull import APPLE_EPOCH


def apple_ns(unix_seconds):
    """Build an Apple-epoch nanosecond timestamp from a Unix timestamp."""
    return int((unix_seconds - APPLE_EPOCH) * 1e9)


def attributed_blob(text):
    """Construct a streamtyped NSAttributedString blob like Messages stores.

    Text is length-prefixed UTF-8 after the 'NSString' marker and a '+' tag.
    """
    body = text.encode("utf-8")
    if len(body) < 0x81:
        length = bytes([len(body)])
    else:  # 0x81 marker + uint16 little-endian
        length = b"\x81" + struct.pack("<H", len(body))
    return b"\x04\x0bstreamtyped" + b"NSString" + b"\x01\x95\x84\x01" + b"+" + length + body


class NumberAndTextHelpers(unittest.TestCase):
    def test_normalize_number_strips_country_code_and_formatting(self):
        self.assertEqual(msgpull.normalize_number("+1 (415) 555-0123"), "4155550123")
        self.assertEqual(msgpull.normalize_number("415-555-0123"), "4155550123")
        self.assertEqual(msgpull.normalize_number("14155550123"), "4155550123")

    def test_normalize_number_lowercases_email(self):
        self.assertEqual(msgpull.normalize_number("Alex@iCloud.com"), "alex@icloud.com")

    def test_is_number_or_email(self):
        self.assertTrue(msgpull.is_number_or_email("+14155550123"))
        self.assertTrue(msgpull.is_number_or_email("a@b.com"))
        self.assertFalse(msgpull.is_number_or_email("Mom"))

    def test_resolve_alias_is_case_insensitive(self):
        contacts = {"Mom": "+14155550123"}
        self.assertEqual(msgpull.resolve_alias("mom", contacts), "+14155550123")
        self.assertEqual(msgpull.resolve_alias("Mom", contacts), "+14155550123")
        self.assertIsNone(msgpull.resolve_alias("Dad", contacts))


class TimestampConversion(unittest.TestCase):
    def test_nanosecond_timestamps(self):
        self.assertAlmostEqual(msgpull.apple_time_to_unix(apple_ns(1_700_000_000)),
                               1_700_000_000, delta=1)

    def test_legacy_second_timestamps(self):
        # Old schema stored seconds since 2001; value is small (~7e8).
        self.assertAlmostEqual(msgpull.apple_time_to_unix(700_000_000),
                               700_000_000 + APPLE_EPOCH, delta=1)

    def test_none(self):
        self.assertIsNone(msgpull.apple_time_to_unix(None))


class AttributedBodyDecoding(unittest.TestCase):
    def test_short_string(self):
        self.assertEqual(msgpull.decode_attributed_body(attributed_blob("hello world")),
                         "hello world")

    def test_long_string_uses_extended_length(self):
        text = "a" * 200  # exercises the 0x81 + uint16 length path
        self.assertEqual(msgpull.decode_attributed_body(attributed_blob(text)), text)

    def test_empty_or_garbage_returns_none(self):
        self.assertIsNone(msgpull.decode_attributed_body(None))
        self.assertIsNone(msgpull.decode_attributed_body(b""))
        self.assertIsNone(msgpull.decode_attributed_body(b"no marker here"))

    def test_message_text_prefers_text_column(self):
        self.assertEqual(msgpull.message_text("plain text", attributed_blob("ignored")),
                         "plain text")
        self.assertEqual(msgpull.message_text(None, attributed_blob("from blob")),
                         "from blob")


class BuildOutput(unittest.TestCase):
    def rows(self):
        # (date, is_from_me, text, attributedBody, sender)
        return [
            (apple_ns(1_700_000_000), 0, "hi", None, "+14155550123"),
            (apple_ns(1_700_000_060), 1, "hey back", None, "+14155550123"),
        ]

    def test_labels_me_and_contact(self):
        out = msgpull.build_output(self.rows(), "Alex", "+14155550123",
                                   "+14155550123", {}, False, False)
        self.assertIn("] Alex: hi", out)
        self.assertIn("] Me: hey back", out)
        self.assertIn("# Conversation with Alex (+14155550123)", out)

    def test_skips_empty_bodies(self):
        rows = self.rows() + [(apple_ns(1_700_000_120), 0, None, None, "+14155550123")]
        out = msgpull.build_output(rows, "Alex", "+14155550123",
                                   "+14155550123", {}, False, False)
        # third message has no body, so only two transcript lines appear
        self.assertEqual(sum(1 for line in out.splitlines() if line.startswith("[")), 2)

    def test_group_uses_per_sender_labels(self):
        rows = [
            (apple_ns(1_700_000_000), 0, "from a", None, "+14155550123"),
            (apple_ns(1_700_000_060), 0, "from b", None, "+13105550199"),
        ]
        contacts = {"Alex": "+14155550123", "Bren": "+13105550199"}
        out = msgpull.build_output(rows, "Alex", "group", "group",
                                   contacts, True, False)
        self.assertIn("group chat with", out)
        self.assertIn("] Alex: from a", out)
        self.assertIn("] Bren: from b", out)

    def test_json_shape(self):
        out = msgpull.build_output(self.rows(), "Alex", "+14155550123",
                                   "+14155550123", {}, False, True)
        data = json.loads(out)
        self.assertEqual(len(data), 2)
        self.assertEqual(set(data[0]), {"ts", "iso", "from", "text"})


class ConfigRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._dir, self._file = msgpull.CONFIG_DIR, msgpull.CONFIG_FILE
        msgpull.CONFIG_DIR = self.tmp
        msgpull.CONFIG_FILE = self.tmp / "config.json"

    def tearDown(self):
        msgpull.CONFIG_DIR, msgpull.CONFIG_FILE = self._dir, self._file

    def test_defaults_when_missing(self):
        cfg = msgpull.load_config()
        self.assertEqual(cfg["provider"], "gemini")
        self.assertIn("gemini", cfg["models"])

    def test_save_merges_over_defaults(self):
        cfg = msgpull.load_config()
        cfg["provider"] = "anthropic"
        cfg["keys"]["gemini"] = "secret"
        msgpull.save_config(cfg)
        reloaded = msgpull.load_config()
        self.assertEqual(reloaded["provider"], "anthropic")
        self.assertEqual(reloaded["keys"]["gemini"], "secret")
        # untouched defaults survive the merge
        self.assertEqual(reloaded["models"]["anthropic"], "claude-haiku-4-5")


class DatabaseQueries(unittest.TestCase):
    """Build a synthetic chat.db and exercise the real query path."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "chat.db"
        import sqlite3
        con = sqlite3.connect(self.tmp)
        con.executescript(
            """
            CREATE TABLE message (ROWID INTEGER PRIMARY KEY, date INTEGER,
                is_from_me INTEGER, text TEXT, attributedBody BLOB, handle_id INTEGER);
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, style INTEGER,
                chat_identifier TEXT, guid TEXT);
            CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
            CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
            """
        )
        # handle 1 = Alex (in a 1:1 AND a group), handle 2 = Bren (group only)
        con.executemany("INSERT INTO handle VALUES (?,?)",
                        [(1, "+14155550123"), (2, "+13105550199")])
        # chat 1 = 1:1 (style 45), chat 2 = group (style 43)
        con.executemany("INSERT INTO chat VALUES (?,?,?,?)",
                        [(1, 45, "+14155550123", "g1"), (2, 43, "chat-group", "g2")])
        con.executemany("INSERT INTO chat_handle_join VALUES (?,?)",
                        [(1, 1), (2, 1), (2, 2)])
        con.executemany(
            "INSERT INTO message VALUES (?,?,?,?,?,?)",
            [
                (1, apple_ns(1_700_000_000), 0, "1:1 from Alex", None, 1),
                (2, apple_ns(1_700_000_060), 1, "1:1 from me", None, 1),
                (3, apple_ns(1_700_000_120), 0, "group noise", None, 2),
            ],
        )
        con.executemany("INSERT INTO chat_message_join VALUES (?,?)",
                        [(1, 1), (1, 2), (2, 3)])
        con.commit()
        con.close()
        self.con = msgpull.connect_ro(self.tmp)

    def tearDown(self):
        self.con.close()

    def test_find_handle_rowids(self):
        matches, _ = msgpull.find_handle_rowids(self.con, "4155550123")
        self.assertEqual(matches, [1])

    def test_resolve_chats_prefers_one_to_one(self):
        chat_ids, is_group = msgpull.resolve_chats(self.con, [1])
        self.assertEqual(chat_ids, [1])
        self.assertFalse(is_group)

    def test_resolve_chats_falls_back_to_group(self):
        chat_ids, is_group = msgpull.resolve_chats(self.con, [2])
        self.assertEqual(chat_ids, [2])
        self.assertTrue(is_group)

    def test_fetch_messages_scopes_to_chat_and_orders_chronologically(self):
        rows = msgpull.fetch_messages(self.con, [1], count=50, since_unix=None)
        bodies = [r[2] for r in rows]
        self.assertEqual(bodies, ["1:1 from Alex", "1:1 from me"])  # no group noise
        self.assertNotIn("group noise", bodies)

    def test_pull_transcript_end_to_end(self):
        with unittest.mock.patch.object(msgpull, "load_contacts", return_value={}):
            out = msgpull.pull_transcript("+14155550123", count=50, db_path=self.tmp)
        self.assertIn("# Conversation with +14155550123", out)
        self.assertIn("1:1 from Alex", out)
        self.assertNotIn("group noise", out)

    def test_pull_transcript_unknown_contact_raises(self):
        with unittest.mock.patch.object(msgpull, "load_contacts", return_value={}):
            with self.assertRaises(msgpull.MsgpullError):
                msgpull.pull_transcript("9998887777", db_path=self.tmp)


class McpServer(unittest.TestCase):
    """Drive the stdio MCP server through a minimal JSON-RPC handshake."""

    def _rpc(self, messages):
        here = Path(__file__).resolve().parent
        inp = "".join(json.dumps(m) + "\n" for m in messages)
        proc = subprocess.run([sys.executable, str(here / "mcp_server.py")],
                              input=inp, capture_output=True, text=True, timeout=30)
        return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]

    def test_initialize_and_tools_list(self):
        out = self._rpc([
            {"jsonrpc": "2.0", "id": 0, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"}}},
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        ])
        init = next(r for r in out if r.get("id") == 0)
        self.assertEqual(init["result"]["serverInfo"]["name"], "msgpull")
        tools = next(r for r in out if r.get("id") == 1)["result"]["tools"]
        self.assertEqual({t["name"] for t in tools},
                         {"get_messages", "list_conversations", "list_contacts"})

    def test_unknown_method_returns_jsonrpc_error(self):
        out = self._rpc([{"jsonrpc": "2.0", "id": 9, "method": "no/such/method"}])
        self.assertEqual(out[0]["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
