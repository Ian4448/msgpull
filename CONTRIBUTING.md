# Contributing

Glad you're here. This is a small, single-file tool and I'd like to keep it that
way, so a few notes before you open a PR.

## Ground rules

- **Read-only on your data.** The Messages database is always opened read-only
  (`mode=ro&immutable=1`). Never add a code path that writes to `chat.db`.
- **Nothing leaves the machine** except the transcript you explicitly send with
  `--ask`. Don't add telemetry, analytics, or background network calls.
- **Stay dependency-free.** The tool runs on the Python standard library plus
  `pbcopy`. The LLM calls go over `urllib` on purpose. If a change seems to need
  a third-party package, open an issue first so we can talk it through — there's
  a decent chance it can stay stdlib.
- **Don't commit personal data.** Phone numbers, names, and API keys live under
  `~/.config/msgpull/` and are git-ignored. Use the reserved `555-01xx` range for
  examples.

## Running the tests

```sh
python3 -m unittest
```

No setup, no install. The suite uses a synthetic in-memory `chat.db`, so it runs
anywhere — you don't need real messages or Full Disk Access to run it.

If you change anything around contact resolution, conversation scoping, the
`attributedBody` decoder, or timestamp math, add or update a test for it. Those
are the parts that have bitten us before.

## Style

- Match the surrounding code: plain functions, short docstrings, no framework.
- Keep CLI output terse and useful; errors should say what to do next.
- macOS / iMessage specifics are expected — this tool isn't trying to be
  cross-platform.

## Good first issues

- Support pulling a named group chat (`--group "..."`).
- Read encrypted iPhone backups (with a supplied password).
- A `--source phone` that triggers an on-demand backup over USB.

Open an issue describing what you're thinking before a large change, and thanks
for helping out.
