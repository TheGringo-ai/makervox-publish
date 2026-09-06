# Contributing

Contributions are welcome, and the most valuable ones are probably not code.

## The most useful thing you can contribute

**Corrections to [`docs/platform-limits.md`](docs/platform-limits.md).** Platform
tiers, error strings and review requirements change constantly, and a limit
documented here in 2026 may simply be wrong by the time you read it. If you hit
a different error than the one recorded, open an issue with the exact string.
That file is the reason this library is worth using over writing the API calls
yourself, and it decays without help.

**Setup docs for a platform you got approved.** `docs/setup-*.md` exist for
TikTok, Meta, Instagram and X. If you have been through review for a platform
that is missing — LinkedIn, YouTube, Reddit, Pinterest, Bluesky, Threads — the
write-up of what the reviewer actually asked for is worth more than the client
code.

## Adding a platform

Platforms live in `src/makervox_publish/platforms/<name>/` and depend only on the layers
beneath them:

```
config / credentials / state / media / text / identity / http
    <- platforms
        <- cli
```

A platform module must not import another platform. If two need the same
helper, it moves down a layer — `media/cover.py` exists precisely because both
Meta publishers needed a cover offset and putting it inside one of them created
a circular import in the code this was extracted from.

## Ground rules

These are enforced by CI, and one of them is a CI job of its own:

- **Nothing happens at import time.** No socket, no subprocess, no `$HOME`
  write. Directories appear on first use.
- **No new required dependency.** `requests` is the only one. Anything else
  goes behind an optional extra and degrades with a warning when absent.
- **No personal defaults.** Anything only the operator can supply — a redirect
  URI, a bucket, a cloud project id — has no default and raises `ConfigError`.
  A default that points at someone else's infrastructure is a bug.
- **Deny by default.** An empty allow-list means *nothing is permitted*.
- **Never swallow silently.** Any `except` that continues anyway logs why.

## Changing a default that looks paranoid

Read [`docs/scar-tissue.md`](docs/scar-tissue.md) first. Every guard in it
exists because the straightforward version failed in production, sometimes
expensively. If you still think one is unnecessary, say in the PR which
incident you believe no longer applies — that is a perfectly good argument, but
it needs making explicitly rather than by deletion.

## Running the tests

```sh
pip install -e ".[toml]" pytest
pytest -q
```

The suite is offline by design and takes under a second. If a test you add
needs the network, it belongs behind a marker and probably behind a fixture
that fakes the transport instead.

## Reporting a security issue

Do not open a public issue for anything involving credential handling or token
storage. Use GitHub's private vulnerability reporting on this repository.
