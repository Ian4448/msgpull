#!/usr/bin/env python3
"""msgpull — on-demand iMessage/SMS → LLM-ready text.

Reads your local Messages database (~/Library/Messages/chat.db) on demand and
copies the last N messages with a contact to the clipboard in a clean,
chronological format ready to paste into an LLM.

Requires Full Disk Access for the terminal you run this in
(System Settings → Privacy & Security → Full Disk Access).

Usage:
    msgpull "Mom"                 last 50 messages with Mom → clipboard
    msgpull "Mom" 100             last 100
    msgpull +15551234567 30       by number
    msgpull "Mom" --days 7        everything in the last 7 days
    msgpull --list                list recent conversations
    msgpull status                show which source (Mac DB / iPhone backup) is available
    msgpull "Mom" --json          structured JSON instead of pretty text
    msgpull "Mom" --no-copy       print to stdout instead of clipboard
    msgpull "Mom" --source backup read from the latest iPhone backup

    msgpull "Mom" 50 --ask "summarize the main topics and any action items"
                                  send the transcript to an LLM, print the answer

    msgpull contacts sync         pull contacts off the phone (iPhone backup)
    msgpull contacts list         show known name → number mappings
    msgpull contacts add NAME NUM add/override one alias manually

    msgpull config show           show LLM provider / model / key status
    msgpull config set provider anthropic
    msgpull config set-key gemini <token>
"""

import argparse
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HOME = Path.home()
CHAT_DB = HOME / "Library" / "Messages" / "chat.db"
BACKUP_ROOT = HOME / "Library" / "Application Support" / "MobileSync" / "Backup"
CONFIG_DIR = HOME / ".config" / "msgpull"
CONTACTS_FILE = CONFIG_DIR / "contacts.json"
CONFIG_FILE = CONFIG_DIR / "config.json"

# LLM defaults. "flash" = each provider's fast/cheap tier (Gemini Flash / Claude Haiku).
DEFAULT_CONFIG = {
    "provider": "gemini",
    "models": {"gemini": "gemini-2.5-flash", "anthropic": "claude-haiku-4-5"},
    "keys": {},
}
API_KEY_ENV = {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}

# Hashed filenames inside an iPhone backup (SHA1 of "<domain>-<relative path>").
BACKUP_MESSAGES_DB = "3d0d7e5fb2ce288813306e4d4636395e047a3d28"   # Messages chat.db
BACKUP_ADDRESSBOOK_DB = "31bb7ba8914766d4ba40d6dfb6113c8b614be442"  # AddressBook

# Apple's Core Data epoch (2001-01-01) as a Unix timestamp.
APPLE_EPOCH = 978307200


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
class MsgpullError(Exception):
    """Raised for expected, user-facing failures. main() turns it into exit 1;
    library callers (e.g. the MCP server) can catch it instead."""


def die(msg, code=1):
    raise MsgpullError(msg)


def is_number_or_email(s):
    return "@" in s or bool(re.fullmatch(r"[+()\-.\s\d]{5,}", s))


def normalize_number(s):
    """Return a comparison key: last 10 digits for phones, lowercase for emails."""
    if not s:
        return ""
    if "@" in s:
        return s.strip().lower()
    digits = re.sub(r"\D", "", s)
    if len(digits) > 10:
        digits = digits[-10:]
    return digits


def apple_time_to_unix(value):
    """message.date is ns since 2001 on modern macOS, seconds on older."""
    if value is None:
        return None
    # Nanosecond timestamps are ~1e18; second timestamps are ~7e8.
    if value > 1e11:
        value = value / 1e9
    return value + APPLE_EPOCH


def fmt_time(unix):
    if unix is None:
        return "??"
    dt = datetime.fromtimestamp(unix)
    return dt.strftime("%b %d, %I:%M %p").replace(" 0", " ")


def copy_to_clipboard(text):
    p = subprocess.run(["pbcopy"], input=text.encode("utf-8"))
    return p.returncode == 0


def connect_ro(path):
    """Open a SQLite DB strictly read-only so we never touch live data."""
    uri = f"file:{path}?mode=ro&immutable=1"
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as e:
        if "authorization denied" in str(e).lower() or "unable to open" in str(e).lower():
            die(
                f"cannot open {path}.\n"
                "  → Grant Full Disk Access to your terminal app:\n"
                "    System Settings → Privacy & Security → Full Disk Access → enable it,\n"
                "    then fully quit and reopen the terminal."
            )
        die(str(e))


# --------------------------------------------------------------------------- #
# attributedBody decoding (modern macOS stores text here, not in `text`)
# --------------------------------------------------------------------------- #
def decode_attributed_body(blob):
    """Extract plain text from a streamtyped NSAttributedString blob.

    Zero-dependency byte parser: the message text is a length-prefixed UTF-8
    string that follows the 'NSString' class marker and a 0x2b ('+') tag.
    """
    if not blob:
        return None
    try:
        marker = blob.find(b"NSString")
        if marker == -1:
            return None
        i = blob.find(b"\x2b", marker)  # '+' precedes the length
        if i == -1:
            return None
        i += 1
        length = blob[i]
        i += 1
        if length == 0x81:  # next 2 bytes, little-endian
            length = int.from_bytes(blob[i:i + 2], "little")
            i += 2
        elif length == 0x82:  # next 4 bytes, little-endian
            length = int.from_bytes(blob[i:i + 4], "little")
            i += 4
        text = blob[i:i + length].decode("utf-8", errors="replace")
        return text or None
    except Exception:
        return None


def message_text(text, attributed_body):
    if text:
        return text
    return decode_attributed_body(attributed_body)


# --------------------------------------------------------------------------- #
# Contacts
# --------------------------------------------------------------------------- #
def load_contacts():
    if CONTACTS_FILE.exists():
        try:
            return json.loads(CONTACTS_FILE.read_text())
        except json.JSONDecodeError:
            die(f"{CONTACTS_FILE} is not valid JSON")
    return {}


def save_contacts(contacts):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONTACTS_FILE.write_text(json.dumps(contacts, indent=2, ensure_ascii=False))


def resolve_alias(name, contacts):
    """Case-insensitive name → number lookup."""
    if name in contacts:
        return contacts[name]
    lower = {k.lower(): v for k, v in contacts.items()}
    return lower.get(name.lower())


def latest_backup_dir():
    if not BACKUP_ROOT.exists():
        return None
    dirs = [d for d in BACKUP_ROOT.iterdir() if d.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda d: d.stat().st_mtime)


def backup_db_path(backup_dir, hashed_name):
    """Files in a backup live under a 2-char prefix subfolder."""
    return backup_dir / hashed_name[:2] / hashed_name


def stage_db(src):
    """Copy a DB (plus -wal/-shm) to a temp file so we can open it read-only."""
    tmp = Path(tempfile.mkdtemp(prefix="msgpull_")) / "db.sqlite"
    shutil.copy2(src, tmp)
    for ext in ("-wal", "-shm"):
        sidecar = Path(str(src) + ext)
        if sidecar.exists():
            shutil.copy2(sidecar, str(tmp) + ext)
    return tmp


def backup_meta(backup_dir):
    """Read a backup's Manifest.plist for its date and encryption status."""
    meta = {"name": backup_dir.name, "date": None, "encrypted": None,
            "has_messages": False, "has_addressbook": False}
    manifest = backup_dir / "Manifest.plist"
    if manifest.exists():
        try:
            data = plistlib.loads(manifest.read_bytes())
            meta["encrypted"] = bool(data.get("IsEncrypted"))
            meta["date"] = data.get("Date")
        except Exception:
            pass
    if meta["date"] is None:
        meta["date"] = datetime.fromtimestamp(backup_dir.stat().st_mtime)
    meta["has_messages"] = backup_db_path(backup_dir, BACKUP_MESSAGES_DB).exists()
    meta["has_addressbook"] = backup_db_path(backup_dir, BACKUP_ADDRESSBOOK_DB).exists()
    return meta


def db_readable(path):
    """Soft check: can we open this DB read-only? (no die() — returns bool)."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        con.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        con.close()
        return True
    except sqlite3.OperationalError:
        return False


def fmt_date(value):
    if isinstance(value, datetime):
        return value.strftime("%b %d %Y, %I:%M %p").replace(" 0", " ")
    return str(value)


def handle_status():
    """Report which sources are available: live Mac DB vs iPhone backup."""
    print("Mac Messages DB  (--source mac, the default):")
    if not CHAT_DB.exists():
        print(f"  not found at {CHAT_DB}")
    else:
        st = CHAT_DB.stat()
        print(f"  updated:  {fmt_date(datetime.fromtimestamp(st.st_mtime))}")
        print(f"  size:     {st.st_size / 1e6:.0f} MB")
        if db_readable(CHAT_DB):
            print("  access:   OK (Full Disk Access granted)")
        else:
            print("  access:   BLOCKED — grant Full Disk Access (see README)")

    print()
    print("iPhone backup    (--source backup; a local Finder backup, NOT the live phone):")
    backup = latest_backup_dir()
    if not backup:
        print("  none found.")
        print("  → Connect iPhone → Finder → 'Back up all data to this Mac' (uncheck Encrypt).")
        return
    m = backup_meta(backup)
    print(f"  latest:   {backup.name[:8]}…")
    print(f"  date:     {fmt_date(m['date'])}")
    if m["encrypted"]:
        print("  encrypted: YES — v1 cannot read encrypted backups; make an unencrypted one")
    else:
        print("  encrypted: no")
    print(f"  messages: {'present' if m['has_messages'] else 'MISSING'}")
    print(f"  contacts: {'present' if m['has_addressbook'] else 'MISSING'}")

    # Freshness hint: which source is newer?
    if CHAT_DB.exists() and isinstance(m["date"], datetime):
        mac_dt = datetime.fromtimestamp(CHAT_DB.stat().st_mtime)
        backup_dt = m["date"].replace(tzinfo=None)
        print()
        if mac_dt >= backup_dt:
            print("Mac DB is newer than the backup — the default (--source mac) is freshest.")
        else:
            print("Backup is newer than the Mac DB — use --source backup for the latest messages.")


def contacts_sync():
    backup = latest_backup_dir()
    if not backup:
        die(
            "no iPhone backup found.\n"
            "  → Connect your iPhone, open Finder, select it, and choose\n"
            "    'Back up all of the data on your iPhone to this Mac' (uncheck Encrypt for v1)."
        )
    ab_src = backup_db_path(backup, BACKUP_ADDRESSBOOK_DB)
    if not ab_src.exists():
        die(f"AddressBook DB not found in backup {backup.name} (expected {ab_src}).")

    staged = stage_db(ab_src)
    con = connect_ro(staged)
    cur = con.cursor()
    cur.execute(
        """
        SELECT p.First, p.Last, p.Organization, v.value
        FROM ABPerson p
        JOIN ABMultiValue v ON v.record_id = p.ROWID
        WHERE v.value IS NOT NULL
        """
    )
    contacts = load_contacts()
    added = 0
    for first, last, org, value in cur.fetchall():
        name = " ".join(part for part in (first, last) if part) or (org or "").strip()
        if not name or not value:
            continue
        value = value.strip()
        if not (is_number_or_email(value)):
            continue
        if name not in contacts:
            added += 1
        contacts[name] = value
    con.close()
    save_contacts(contacts)
    print(
        f"Synced contacts from backup {backup.name}: {added} new, "
        f"{len(contacts)} total → {CONTACTS_FILE}",
        file=sys.stderr,
    )


def contacts_list():
    contacts = load_contacts()
    if not contacts:
        print("No contacts saved yet. Run: msgpull contacts sync", file=sys.stderr)
        return
    for name in sorted(contacts, key=str.lower):
        print(f"{name:<28} {contacts[name]}")


def contacts_add(name, number):
    contacts = load_contacts()
    contacts[name] = number
    save_contacts(contacts)
    print(f"Saved {name} → {number}", file=sys.stderr)


def handle_contacts(argv):
    if not argv:
        die("usage: msgpull contacts {sync|list|add NAME NUMBER}")
    sub = argv[0]
    if sub == "sync":
        contacts_sync()
    elif sub == "list":
        contacts_list()
    elif sub == "add":
        if len(argv) < 3:
            die("usage: msgpull contacts add NAME NUMBER")
        contacts_add(argv[1], argv[2])
    else:
        die(f"unknown contacts command: {sub}")


# --------------------------------------------------------------------------- #
# LLM: send a transcript to an LLM and get an answer (--ask)
# --------------------------------------------------------------------------- #
def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy of defaults
    if CONFIG_FILE.exists():
        try:
            user = json.loads(CONFIG_FILE.read_text())
        except json.JSONDecodeError:
            die(f"{CONFIG_FILE} is not valid JSON")
        cfg["provider"] = user.get("provider", cfg["provider"])
        cfg["models"].update(user.get("models", {}))
        cfg["keys"].update(user.get("keys", {}))
    return cfg


def save_config(config):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2))
    os.chmod(CONFIG_FILE, 0o600)  # may hold API keys


def get_api_key(provider, config):
    env = os.environ.get(API_KEY_ENV.get(provider, ""))
    if env:
        return env
    return config.get("keys", {}).get(provider)


def _post_json(req):
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:600]
        die(f"LLM API error {e.code}: {detail}")
    except urllib.error.URLError as e:
        die(f"network error contacting LLM: {e.reason}")


def call_gemini(prompt, model, key):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"content-type": "application/json", "x-goog-api-key": key},
    )
    data = _post_json(req)
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        die(f"unexpected Gemini response: {json.dumps(data)[:600]}")


def call_anthropic(prompt, model, key):
    # Documented Messages API over raw HTTP to keep msgpull dependency-free.
    url = "https://api.anthropic.com/v1/messages"
    body = json.dumps({
        "model": model,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
    )
    data = _post_json(req)
    try:
        return "".join(
            b["text"] for b in data["content"] if b.get("type") == "text"
        ).strip()
    except (KeyError, TypeError):
        die(f"unexpected Anthropic response: {json.dumps(data)[:600]}")


def ask_llm(transcript, question, args, config):
    provider = args.provider or config.get("provider", "gemini")
    model = args.model or config.get("models", {}).get(provider) \
        or DEFAULT_CONFIG["models"].get(provider)
    if not model:
        die(f"no model configured for provider '{provider}'.")
    key = get_api_key(provider, config)
    if not key:
        env = API_KEY_ENV.get(provider, "")
        die(
            f"no API key for '{provider}'.\n"
            f"  → export {env}=<token>, or run: msgpull config set-key {provider} <token>"
        )
    prompt = f"{question}\n\nHere is the conversation transcript:\n\n{transcript}"
    if provider == "gemini":
        return call_gemini(prompt, model, key)
    if provider == "anthropic":
        return call_anthropic(prompt, model, key)
    die(f"unknown provider: {provider}")


def handle_config(argv):
    config = load_config()
    if not argv or argv[0] in ("show", "list"):
        provider = config["provider"]
        print(f"provider: {provider}")
        print("models:")
        for p, m in config["models"].items():
            print(f"  {p}: {m}")
        print("keys:")
        for p in API_KEY_ENV:
            if os.environ.get(API_KEY_ENV[p]):
                src = f"env ${API_KEY_ENV[p]}"
            elif config.get("keys", {}).get(p):
                src = "config.json"
            else:
                src = "(not set)"
            print(f"  {p}: {src}")
        return

    sub = argv[0]
    if sub == "set" and len(argv) >= 3 and argv[1] == "provider":
        if argv[2] not in API_KEY_ENV:
            die(f"unknown provider: {argv[2]} (choose: {', '.join(API_KEY_ENV)})")
        config["provider"] = argv[2]
        save_config(config)
        print(f"Default provider → {argv[2]}", file=sys.stderr)
    elif sub == "set-model" and len(argv) >= 3:
        config.setdefault("models", {})[argv[1]] = argv[2]
        save_config(config)
        print(f"Model for {argv[1]} → {argv[2]}", file=sys.stderr)
    elif sub == "set-key" and len(argv) >= 3:
        config.setdefault("keys", {})[argv[1]] = argv[2]
        save_config(config)
        print(f"Saved API key for {argv[1]} (in {CONFIG_FILE}, chmod 600)", file=sys.stderr)
    else:
        die(
            "usage: msgpull config {show | set provider <gemini|anthropic> | "
            "set-model <provider> <name> | set-key <provider> <token>}"
        )


# --------------------------------------------------------------------------- #
# Message pulling
# --------------------------------------------------------------------------- #
def find_handle_rowids(con, target):
    """Return handle ROWIDs whose id matches target (by normalized number/email)."""
    key = normalize_number(target)
    cur = con.cursor()
    cur.execute("SELECT ROWID, id FROM handle")
    rows = cur.fetchall()
    matches = [rowid for rowid, hid in rows if normalize_number(hid) == key]
    return matches, rows


def list_conversations(con, contacts, limit=30):
    """Return a text listing of the most recently active conversations."""
    cur = con.cursor()
    cur.execute(
        """
        SELECT h.id, MAX(m.date) AS last
        FROM message m
        JOIN handle h ON h.ROWID = m.handle_id
        GROUP BY h.id
        ORDER BY last DESC
        LIMIT ?
        """,
        (limit,),
    )
    by_number = {normalize_number(v): k for k, v in contacts.items()}
    lines = ["Recent conversations:"]
    for hid, last in cur.fetchall():
        name = by_number.get(normalize_number(hid), "")
        when = fmt_time(apple_time_to_unix(last))
        label = f"{name}  " if name else ""
        lines.append(f"  {label}{hid:<22} (last: {when})")
    return "\n".join(lines)


def resolve_chats(con, handle_rowids):
    """Pick the conversation(s) for a contact.

    Prefer the 1:1 chat(s) (chat.style == 45) so a person who's also in many
    group chats still resolves to their direct thread. Fall back to any chat
    containing the handle (group-only contacts) and report that it's a group.
    """
    placeholders = ",".join("?" for _ in handle_rowids)
    cur = con.cursor()
    cur.execute(
        f"""
        SELECT DISTINCT c.ROWID, c.style
        FROM chat c
        JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
        WHERE chj.handle_id IN ({placeholders})
        """,
        handle_rowids,
    )
    rows = cur.fetchall()
    direct = [cid for cid, style in rows if style == 45]
    if direct:
        return direct, False
    return [cid for cid, _ in rows], True


def fetch_messages(con, chat_ids, count, since_unix):
    placeholders = ",".join("?" for _ in chat_ids)
    params = list(chat_ids)
    where_time = ""
    if since_unix is not None:
        # message.date is nanoseconds since the Apple epoch on modern schemas
        where_time = " AND m.date >= ?"
        params.append(int((since_unix - APPLE_EPOCH) * 1e9))
    sql = f"""
        SELECT m.date, m.is_from_me, m.text, m.attributedBody, h.id
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE cmj.chat_id IN ({placeholders}){where_time}
        ORDER BY m.date DESC
    """
    if since_unix is None:
        sql += " LIMIT ?"
        params.append(count)
    cur = con.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    rows.reverse()  # chronological: oldest → newest
    return rows


def build_output(rows, display_name, number, target, contacts, is_group, as_json):
    # Map any participant's number → saved contact name for nicer labels.
    by_number = {normalize_number(v): k for k, v in contacts.items()}
    target_key = normalize_number(target)

    def label_for(is_from_me, sender):
        if is_from_me:
            return "Me"
        if not sender or normalize_number(sender) == target_key:
            return display_name
        return by_number.get(normalize_number(sender), sender)

    records = []
    for date, is_from_me, text, attr, sender in rows:
        body = message_text(text, attr)
        if not body:
            continue
        unix = apple_time_to_unix(date)
        records.append(
            {
                "ts": unix,
                "iso": datetime.fromtimestamp(unix).isoformat() if unix else None,
                "from": label_for(is_from_me, sender),
                "text": body,
            }
        )

    if as_json:
        return json.dumps(records, indent=2, ensure_ascii=False)

    if not records:
        return ""
    first = fmt_time(records[0]["ts"])
    last = fmt_time(records[-1]["ts"])
    kind = "group chat with" if is_group else "Conversation with"
    noun = "message" if len(records) == 1 else "messages"
    lines = [
        f"# {kind} {display_name} ({number})",
        f"# {len(records)} most recent {noun} · {first} → {last}",
        "",
    ]
    for r in records:
        lines.append(f"[{fmt_time(r['ts'])}] {r['from']}: {r['text']}")
    return "\n".join(lines)


def open_source(source):
    """Resolve a source ("mac" or "backup") to a readable DB path + a note.

    Returns (db_path, note). Raises MsgpullError on missing/encrypted sources.
    """
    if source == "backup":
        backup = latest_backup_dir()
        if not backup:
            die("no iPhone backup found for --source backup. Check: msgpull status")
        src = backup_db_path(backup, BACKUP_MESSAGES_DB)
        if not src.exists():
            die(f"Messages DB not found in backup {backup.name}. Check: msgpull status")
        meta = backup_meta(backup)
        if meta["encrypted"]:
            die(f"backup {backup.name[:8]}… is encrypted; v1 can't read it. "
                "Make an unencrypted Finder backup.")
        return stage_db(src), f"iPhone backup from {fmt_date(meta['date'])}"
    if not CHAT_DB.exists():
        die(f"{CHAT_DB} not found.")
    mtime = datetime.fromtimestamp(CHAT_DB.stat().st_mtime)
    return CHAT_DB, f"Mac live Messages DB (updated {fmt_date(mtime)})"


def resolve_contact(con, contact, contacts):
    """Resolve a name/alias/number/email to (target, display_name, handle_rowids)."""
    target = contact
    display_name = contact
    if not is_number_or_email(target):
        resolved = resolve_alias(target, contacts)
        if resolved:
            target = resolved
        # else fall through: treat as substring against handle ids below
    else:
        by_number = {normalize_number(v): k for k, v in contacts.items()}
        display_name = by_number.get(normalize_number(target), target)

    matches, all_handles = find_handle_rowids(con, target)
    if matches:
        return target, display_name, matches

    # substring fallback against raw handle ids
    sub = [(rid, hid) for rid, hid in all_handles if target.lower() in (hid or "").lower()]
    distinct = sorted({hid for _, hid in sub})
    if not sub:
        die(f"no conversation found matching '{contact}'. Try: msgpull --list")
    if len(distinct) > 1:
        listing = "\n  ".join(distinct)
        die(f"'{contact}' matches multiple handles:\n  {listing}\n"
            "be more specific or pass the exact number/email.")
    matches = [rid for rid, _ in sub]
    target = distinct[0]
    by_number = {normalize_number(v): k for k, v in contacts.items()}
    display_name = by_number.get(normalize_number(target), target)
    return target, display_name, matches


def pull_transcript(contact, count=50, days=None, source="mac", as_json=False, db_path=None):
    """Library entry point: return the formatted transcript for a contact.

    Shared by the CLI and the MCP server. Raises MsgpullError on any failure.
    Pass db_path to reuse an already-opened source (avoids re-staging a backup).
    """
    if db_path is None:
        db_path, _ = open_source(source)
    con = connect_ro(db_path)
    try:
        contacts = load_contacts()
        target, display_name, matches = resolve_contact(con, contact, contacts)
        chat_ids, is_group = resolve_chats(con, matches)
        if not chat_ids:
            die(f"no conversation found for '{contact}'. Try: msgpull --list")
        since_unix = None if days is None else datetime.now().timestamp() - days * 86400
        rows = fetch_messages(con, chat_ids, count, since_unix)
    finally:
        con.close()
    out = build_output(rows, display_name, target, target, contacts, is_group, as_json)
    if not out:
        die("no messages found for that contact/time window.")
    return out


def handle_pull(args):
    db_path, note = open_source(args.source)
    print(f"Source: {note}", file=sys.stderr)

    if args.list:
        con = connect_ro(db_path)
        try:
            print(list_conversations(con, load_contacts()))
        finally:
            con.close()
        return

    if not args.contact:
        die("usage: msgpull CONTACT [COUNT]  (or: msgpull --list)")

    # --ask: send the transcript to an LLM and print/copy its answer.
    if args.ask:
        transcript = pull_transcript(args.contact, args.count, args.days,
                                     args.source, False, db_path=db_path)
        config = load_config()
        provider = args.provider or config.get("provider", "gemini")
        print(f"Asking {provider}…", file=sys.stderr)
        answer = ask_llm(transcript, args.ask, args, config)
        print(answer)
        if not args.no_copy and copy_to_clipboard(answer):
            print("(answer copied to clipboard)", file=sys.stderr)
        return

    out = pull_transcript(args.contact, args.count, args.days,
                          args.source, args.json, db_path=db_path)

    if args.no_copy or args.json:
        print(out)
    else:
        if copy_to_clipboard(out):
            n = sum(1 for line in out.splitlines() if line.startswith("["))
            noun = "message" if n == 1 else "messages"
            print(f"Copied {n} {noun} with {args.contact} to clipboard.", file=sys.stderr)
        else:
            print(out)


# --------------------------------------------------------------------------- #
# CLI entry
# --------------------------------------------------------------------------- #
def run(argv):
    if argv and argv[0] == "contacts":
        handle_contacts(argv[1:])
        return
    if argv and argv[0] == "config":
        handle_config(argv[1:])
        return
    if argv and argv[0] == "status":
        handle_status()
        return

    parser = argparse.ArgumentParser(
        prog="msgpull",
        description="Copy the last N iMessages/SMS with a contact to the clipboard.",
    )
    parser.add_argument("contact", nargs="?", help="contact name (alias), phone, or email")
    parser.add_argument("count", nargs="?", type=int, default=50,
                        help="number of recent messages (default: 50)")
    parser.add_argument("--days", type=int, help="messages from the last N days (overrides count)")
    parser.add_argument("--list", action="store_true", help="list recent conversations")
    parser.add_argument("--json", action="store_true", help="output structured JSON")
    parser.add_argument("--no-copy", action="store_true", help="print instead of copying")
    parser.add_argument("--source", choices=["mac", "backup"], default="mac",
                        help="read from the live Mac DB (default) or latest iPhone backup")
    parser.add_argument("--ask", metavar="PROMPT",
                        help="send the transcript to an LLM with this instruction and print the answer")
    parser.add_argument("--provider", choices=["gemini", "anthropic"],
                        help="LLM provider for --ask (default: from config)")
    parser.add_argument("--model", help="override the LLM model id for --ask")
    args = parser.parse_args(argv)
    handle_pull(args)


def main():
    try:
        run(sys.argv[1:])
    except MsgpullError as e:
        print(f"msgpull: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
