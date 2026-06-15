#!/usr/bin/env python3
"""MCP server for msgpull — exposes your Messages to an MCP client (Claude
Desktop, etc.) as tools, so an assistant can pull conversations itself instead
of you copy-pasting.

This speaks the Model Context Protocol over stdio (newline-delimited JSON-RPC).
No third-party packages — it reuses the functions in msgpull.py.

It reads the same local Messages database as the CLI, read-only, and runs
locally. The only data that leaves your machine is whatever the connected MCP
client chooses to do with the tool results.

Register it with Claude Desktop (claude_desktop_config.json):

    {
      "mcpServers": {
        "msgpull": { "command": "python3", "args": ["/abs/path/to/mcp_server.py"] }
      }
    }
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import msgpull  # noqa: E402

SERVER_NAME = "msgpull"
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL = "2025-06-18"

TOOLS = [
    {
        "name": "get_messages",
        "description": "Get recent iMessage/SMS conversation with one contact, as a "
                       "chronological transcript. Identify the contact by saved alias "
                       "(see list_contacts), phone number, or email.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact": {"type": "string",
                            "description": "Alias, phone number, or email."},
                "count": {"type": "integer", "default": 50,
                          "description": "How many recent messages (ignored if days is set)."},
                "days": {"type": "integer",
                         "description": "Instead of count, return messages from the last N days."},
                "source": {"type": "string", "enum": ["mac", "backup"], "default": "mac",
                           "description": "Live Mac database (default) or latest iPhone backup."},
            },
            "required": ["contact"],
        },
    },
    {
        "name": "list_conversations",
        "description": "List the most recently active conversations (numbers/names), "
                       "to discover who you can pull messages from.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 30},
                "source": {"type": "string", "enum": ["mac", "backup"], "default": "mac"},
            },
        },
    },
    {
        "name": "list_contacts",
        "description": "List the saved name → number aliases msgpull knows about.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# --------------------------------------------------------------------------- #
# Tool implementations (return a plain string; raise MsgpullError on failure)
# --------------------------------------------------------------------------- #
def tool_get_messages(args):
    return msgpull.pull_transcript(
        contact=args["contact"],
        count=int(args.get("count", 50)),
        days=args.get("days"),
        source=args.get("source", "mac"),
    )


def tool_list_conversations(args):
    db_path, _ = msgpull.open_source(args.get("source", "mac"))
    con = msgpull.connect_ro(db_path)
    try:
        return msgpull.list_conversations(con, msgpull.load_contacts(),
                                          limit=int(args.get("limit", 30)))
    finally:
        con.close()


def tool_list_contacts(args):
    contacts = msgpull.load_contacts()
    if not contacts:
        return "No saved contacts. Add one with: msgpull contacts add NAME NUMBER"
    return "\n".join(f"{name}: {num}" for name, num in sorted(contacts.items()))


TOOL_IMPLS = {
    "get_messages": tool_get_messages,
    "list_conversations": tool_list_conversations,
    "list_contacts": tool_list_contacts,
}


# --------------------------------------------------------------------------- #
# JSON-RPC / MCP plumbing
# --------------------------------------------------------------------------- #
def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def reply(req_id, result):
    send({"jsonrpc": "2.0", "id": req_id, "result": result})


def error(req_id, code, message):
    send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def handle_tools_call(params):
    name = params.get("name")
    args = params.get("arguments") or {}
    impl = TOOL_IMPLS.get(name)
    if impl is None:
        return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                "isError": True}
    try:
        text = impl(args)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except msgpull.MsgpullError as e:
        return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
    except Exception as e:  # don't crash the server on an unexpected tool failure
        return {"content": [{"type": "text", "text": f"Unexpected error: {e}"}],
                "isError": True}


def dispatch(message):
    """Handle one parsed JSON-RPC message. Returns nothing (replies via send)."""
    method = message.get("method")
    req_id = message.get("id")
    params = message.get("params") or {}

    # Notifications (no id) are fire-and-forget.
    if req_id is None:
        return

    if method == "initialize":
        version = params.get("protocolVersion", DEFAULT_PROTOCOL)
        reply(req_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    elif method == "ping":
        reply(req_id, {})
    elif method == "tools/list":
        reply(req_id, {"tools": TOOLS})
    elif method == "tools/call":
        reply(req_id, handle_tools_call(params))
    else:
        error(req_id, -32601, f"Method not found: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            error(None, -32700, "Parse error")
            continue
        try:
            dispatch(message)
        except Exception as e:  # last-resort guard; keep the loop alive
            print(f"mcp_server: dispatch error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
