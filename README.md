# msgpull

A command-line tool that pulls the last N texts with someone out of macOS
Messages and puts them on your clipboard, formatted for pasting into an LLM
(or anywhere else).

I kept hand-copying chunks of conversations into chat assistants to get them
summarized. This does it in one command. It reads the Messages database the
Messages app already keeps on your Mac (`~/Library/Messages/chat.db`); nothing
is uploaded and the database is opened read-only.

## Requirements

- macOS with Messages signed in (iMessage/SMS syncing to the Mac)
- Python 3.9+
- Full Disk Access for your terminal — the Messages database is protected by macOS

## Install

Clone the repo and link the script onto your `PATH`:

```sh
git clone https://github.com/Ian4448/msgpull.git ~/tools/msgpull
cd ~/tools/msgpull
chmod +x msgpull.py
mkdir -p ~/.local/bin
ln -s "$PWD/msgpull.py" ~/.local/bin/msgpull
```

Make sure `~/.local/bin` is on your `PATH` (most shells already have it; if not,
add `export PATH="$HOME/.local/bin:$PATH"` to your `~/.zshrc`). Then `msgpull`
works from anywhere. To update later: `cd ~/tools/msgpull && git pull`.

It's a single script — you can also just run `./msgpull.py …` from the repo
without linking it.

### Grant Full Disk Access

The Messages database is protected by macOS, so your terminal needs Full Disk
Access:

> System Settings → Privacy & Security → Full Disk Access → add your terminal
> (Terminal, iTerm, VS Code…), then quit and reopen it.

Confirm it worked:

```sh
sqlite3 ~/Library/Messages/chat.db "select count(*) from message;"
```

A number means you're good. `authorization denied` means access isn't granted yet.

## Usage

```sh
msgpull --list                # recent conversations, to find a number
msgpull 4155550123            # last 50 messages, copied to clipboard
msgpull 4155550123 100        # last 100
msgpull 4155550123 --days 7   # everything from the last week
msgpull 4155550123 --no-copy  # print instead of copying
msgpull 4155550123 --json     # machine-readable output
```

Output is plain and chronological:

```
# Conversation with +14155550123
# 50 messages · Jun 10, 2:22 PM → Jun 14, 5:01 PM

[Jun 10, 2:22 PM] Me: are you around this weekend?
[Jun 10, 2:25 PM] +14155550123: yeah, driving up saturday
```

### Names instead of numbers

Save an alias once, then use it:

```sh
msgpull contacts add "Alex" 4155550123
msgpull "Alex" 80
```

`msgpull contacts list` shows what's saved; `msgpull contacts sync` bulk-imports
names from an iPhone backup (see Sources). Aliases live in
`~/.config/msgpull/contacts.json` and are never committed.

### Ask an LLM directly

Skip the copy-paste and send the transcript straight to a model:

```sh
msgpull config set-key gemini YOUR_KEY        # or: export GEMINI_API_KEY
msgpull "Alex" 50 --ask "what did we agree on about the trip?"
```

The answer prints and lands on your clipboard. Defaults to Gemini Flash;
`msgpull config set provider anthropic` switches to Claude (Haiku). Override per
call with `--provider` / `--model`. Keys come from `GEMINI_API_KEY` /
`ANTHROPIC_API_KEY` or `~/.config/msgpull/config.json` (mode 600, never
committed). Requests go to the provider's REST API over the standard library —
no SDKs to install.

## Use it from an assistant (MCP)

`mcp_server.py` exposes the same functionality as an [MCP](https://modelcontextprotocol.io)
server, so a client like Claude Desktop can pull conversations itself — no
copy-paste, no `--ask`. It's stdlib-only (no install) and reuses the CLI's code.

Add it to your client's MCP config. For Claude Desktop
(`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "msgpull": {
      "command": "python3",
      "args": ["/absolute/path/to/msgpull/mcp_server.py"]
    }
  }
}
```

Restart the client. It gets three tools: `get_messages`, `list_conversations`,
and `list_contacts`. The server still needs Full Disk Access (it runs as you) and
reads the database read-only. Note that tool results are visible to whatever
client you connect — only point it at assistants you trust with your messages.

## Sources

msgpull reads the live Messages database on your Mac by default, and prints which
source each run used. To see what's available:

```sh
msgpull status
```

This reports the Mac database (last updated, size, Full Disk Access) and the most
recent local iPhone backup, if one exists.

The Mac database sometimes trails what's on the phone. Two ways to get fresher data:

- Turn on Messages in iCloud (Messages → Settings → iMessage) so the Mac copy
  stays complete.
- Make an unencrypted backup in Finder, then run with `--source backup`. The same
  backup feeds `contacts sync`.

msgpull does not read a connected phone directly — iOS doesn't expose the Messages
database over USB, so a backup is the only route to it. Encrypted backups aren't
supported yet.

## Data handling

- The Messages database is opened read-only; msgpull never writes to it.
- Nothing leaves your machine except with `--ask`, which sends the selected
  transcript to the LLM provider you configured.
- Contacts and API keys live under `~/.config/msgpull/` and are git-ignored.

## Tests

```sh
python3 -m unittest
```

No dependencies and no setup — the suite builds a synthetic database, so it runs
without Full Disk Access or any real messages.

## Contributing

Issues and PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) — the short version
is: keep it single-file and dependency-free, never write to the Messages database,
and don't add network calls beyond the opt-in `--ask`.

## License

MIT — see [LICENSE](LICENSE).
