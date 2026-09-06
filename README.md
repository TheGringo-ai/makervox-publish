# postvox

**You bring your own developer apps.** postvox publishes to TikTok, Facebook,
Instagram and X using *your* TikTok app, *your* Meta app and *your* X app.
There is no shared application, no hosted OAuth broker, no account system and
no default that points at anyone else's domain or cloud project. Anything only
you can supply — an OAuth redirect URI, a media staging bucket, a cloud project
id — has **no default** and raises `ConfigError` rather than guessing.

It is a library first and a CLI second, with one runtime dependency
(`requests`) and optional extras for the things you may not want
(`postvox[gcp]`, `postvox[s3]`, `postvox[keyring]`, `postvox[toml]`,
`postvox[yaml]`).

```python
from postvox import Config, Credentials

cfg   = Config.load()          # ./postvox.toml, ~/.config/postvox/…, /etc/postvox/…
creds = Credentials.default()  # environment variables; no file, no cloud, no network

values, missing = creds.require(["X_API_KEY", "X_API_SECRET"])
if missing:
    print("not configured yet:", ", ".join(missing))
```

## Install

```sh
pip install postvox                 # library + CLI
pip install "postvox[toml]"         # TOML config on Python 3.9 / 3.10
pip install "postvox[gcp,keyring]"  # optional credential + staging backends
```

Python 3.9 or newer. `ffmpeg`/`ffprobe` are optional: without them, cover-frame
selection, duration probing, short cuts and pre-upload transcodes are disabled
with one warning at startup instead of failing mid-publish.

## Configure

Copy [`config.example.toml`](config.example.toml) — every value in it is a fake
placeholder — to `./postvox.toml`, `~/.config/postvox/postvox.toml` or
`/etc/postvox/postvox.toml`, or point `POSTVOX_CONFIG` at any path. Any
`POSTVOX_`-prefixed environment variable overlays the file:

```sh
POSTVOX_PLATFORMS__X__GOVERNOR__DAILY_CAP=2
```

**The config file holds secret NAMES, never secret VALUES.** Every
`*_credential` key names something the credential chain resolves — an
environment variable by default. That makes the file safe to commit; your
tokens are not, and `.gitignore` here refuses both by default.

## What it is careful about

The defaults encode failures that have actually happened, not hypotheticals:

- **A filename is not an identity.** A scheduler that renders the same content
  type twice in one day overwrites the first file, so two genuinely different
  videos share one name. Dedupe is on `(account, date, key, slot, content
  hash)`, hashed from the source media, and it says so out loud when it cannot
  resolve an identity instead of silently disabling itself.
- **Deduplication happens inside the publish lock.** A lock only serializes
  callers; the second caller wakes up and publishes the same reel again unless
  the check is re-run after acquiring it.
- **Refresh tokens that rotate on use are treated as unrecoverable.** The
  refresh is serialized, re-read inside the lock so the loser adopts the
  winner's token instead of spending its own, and written to local disk before
  anything that can time out.
- **Allow-lists deny by default.** An empty X account allow-list means *post
  nothing*, because a shared publish path otherwise grants posting rights to
  anything that passes through it.
- **Nothing is swallowed silently.** Every deliberate `except` that continues
  anyway logs its reason; a cover frame is never worth failing a post over, but
  a cover frame that vanished without a word is how a profile grid ends up a
  wall of black thumbnails.
- **Nothing is created on import.** Loading a config on a fresh machine touches
  no socket, no subprocess and no `$HOME`. Directories appear on first write.

`docs/scar-tissue.md` records the incident behind each of these;
`docs/platform-limits.md` records the vendor realities that no amount of code
will fix (scopes are frozen at authorization, Instagram can only pull media by
URL and cannot attach a trending sound, reels report plays and not reach).

## Layout

Strictly one-directional layering, with no lazy imports anywhere:

```
config / credentials / state / media / text / identity / http
    <- platforms
        <- cli
```

`media/cover.py` is the clearest example of why: both Meta publishers need a
cover offset, so `cover_offset_ms()` lives a layer below both of them and
neither owns it. Putting it inside one publisher is what produced the circular
import in the code this package was extracted from.

## License

MIT © Fred Taylor. This is a deliberate relicense of proprietary source; no
account names, domains, cloud project ids or marketing copy crossed the
boundary.
