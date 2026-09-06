"""``makervox-publish`` — authorize accounts and inspect configuration.

Deliberately small. Publishing is a library call: a scheduler wants return
values, not exit codes. What genuinely needs a terminal is OAuth, because it
needs a browser, and reading back what is configured.

    makervox-publish auth tiktok <account>
    makervox-publish auth facebook --user-token <token>
    makervox-publish status
"""

from __future__ import annotations

import argparse
import secrets
import sys
from typing import List, Optional

from makervox_publish.errors import ConfigError, MakervoxPublishError
from makervox_publish.version import __version__


def _load_config(path: Optional[str]):
    from makervox_publish import Config
    return Config.load(path) if path else Config.load()


# ------------------------------------------------------------------ auth #

def _auth_tiktok(cfg, account: str) -> int:
    """Loopback OAuth for one TikTok account.

    Refuses to file the result under `account` when the returned open_id already
    belongs to a different local name. TikTok issues a token for whichever
    account the BROWSER is signed into, not the one on the command line, so
    authorizing B while signed in as A silently stores A under B — and the
    symptom surfaces two steps later as spam_risk on a shared draft quota.
    """
    from makervox_publish.cli.auth_listener import wait_for_code
    from makervox_publish.platforms.tiktok.client import TikTokClient

    client = TikTokClient.from_config(cfg)
    if not client.is_configured():
        print("✗ {0}".format(client.not_configured_reason()), file=sys.stderr)
        return 1

    listener = cfg.cli.auth_listener
    state = secrets.token_urlsafe(16)
    url = client.authorize_url(account, state)

    print("Open this in a browser signed in as the {0!r} account:\n".format(account))
    print("  {0}\n".format(url))
    print("⚠️  Use a PRIVATE window if you manage more than one account. TikTok "
          "issues a token for whoever the browser is signed in as, not for the "
          "name you typed.")
    print("Waiting on http://{0}:{1} …".format(listener.host, listener.port))

    params = wait_for_code(listener.host, listener.port)
    if not params:
        print("✗ timed out waiting for the redirect", file=sys.stderr)
        return 1
    if "code" not in params:
        print("✗ no code returned: {0}".format(
            params.get("error_description") or params.get("error") or params),
            file=sys.stderr)
        return 1
    if params.get("state") not in (None, "", state):
        print("✗ state mismatch — refusing to exchange this code", file=sys.stderr)
        return 1

    client.exchange_code(account, params["code"])
    print("✓ authorized {0!r}".format(account))
    print("  Scopes are FIXED at authorization. Adding one to config later does "
          "nothing until you re-run this command.")
    return 0


def _auth_meta(cfg, user_token: str) -> int:
    """Exchange a short-lived Meta user token for non-expiring Page tokens."""
    from makervox_publish.credentials import Credentials
    from makervox_publish.platforms.meta.facebook import FacebookPublisher

    publisher = FacebookPublisher.from_config(cfg)
    pages = publisher.connect(user_token, Credentials.from_config(cfg.credentials))
    if not pages:
        print("✗ no Pages came back for that user token", file=sys.stderr)
        return 1
    print("✓ stored {0} Page token(s):".format(len(pages)))
    for page in pages:
        print("    {0}  {1}".format(page.get("id", "?"), page.get("name", "")))
    print("  Link one to a local account name with "
          "`accounts.<name>.facebook.page_id` in your config.")
    return 0


# ---------------------------------------------------------------- status #

def _status(cfg) -> int:
    """What is configured, and what each platform is still missing."""
    print("makervox-publish {0}".format(__version__))
    print("config: {0}".format(getattr(cfg, "source", None) or "(defaults)"))
    print()

    rows = []
    for name in ("tiktok", "facebook", "instagram", "x"):
        platform = getattr(cfg.platforms, name, None)
        if platform is None:
            continue
        if not getattr(platform, "enabled", False):
            rows.append((name, "disabled", ""))
            continue
        try:
            detail = _probe(cfg, name)
        except Exception as exc:  # a bad config must not hide the other rows
            detail = ("error", "{0}: {1}".format(type(exc).__name__, str(exc)[:70]))
        rows.append((name,) + detail)

    width = max(len(r[0]) for r in rows) if rows else 8
    for name, state, detail in rows:
        print("  {0:<{1}}  {2:<14} {3}".format(name, width, state, detail))
    return 0


def _probe(cfg, name: str):
    if name == "tiktok":
        from makervox_publish.platforms.tiktok.client import TikTokClient
        client = TikTokClient.from_config(cfg)
        return ("ready", "") if client.is_configured() else (
            "not configured", client.not_configured_reason()[:70])
    if name == "x":
        from makervox_publish.platforms.x.client import XClient
        ok, missing = XClient.from_config(cfg).ready()
        return ("ready", "") if ok else ("not configured", ", ".join(missing))
    if name == "facebook":
        from makervox_publish.platforms.meta.facebook import FacebookPublisher
        pages = FacebookPublisher.from_config(cfg).pages()
        return ("ready", "{0} page(s) linked".format(len(pages))) if pages else (
            "not linked", "run: makervox-publish auth facebook --user-token …")
    if name == "instagram":
        from makervox_publish.platforms.meta.instagram import publisher
        publisher(cfg)
        return ("ready", "")
    return ("?", "")


# ------------------------------------------------------------------ main #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="makervox-publish",
        description="Authorize accounts and inspect makervox-publish configuration.",
    )
    parser.add_argument("--version", action="version",
                        version="makervox-publish {0}".format(__version__))
    parser.add_argument("-c", "--config", help="path to makervox-publish.toml")
    sub = parser.add_subparsers(dest="command")

    auth = sub.add_parser("auth", help="authorize an account against a platform")
    auth_sub = auth.add_subparsers(dest="platform")

    tiktok = auth_sub.add_parser("tiktok", help="loopback OAuth for one account")
    tiktok.add_argument("account", help="local account name to store it under")

    for name in ("facebook", "instagram", "meta"):
        meta = auth_sub.add_parser(name, help="exchange a Meta user token for Page tokens")
        meta.add_argument("--user-token", required=True,
                          help="short-lived user token from the Graph API Explorer")

    sub.add_parser("status", help="show what is configured")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2

    try:
        cfg = _load_config(args.config)
        if args.command == "status":
            return _status(cfg)
        if args.command == "auth":
            if args.platform == "tiktok":
                return _auth_tiktok(cfg, args.account)
            if args.platform in ("facebook", "instagram", "meta"):
                return _auth_meta(cfg, args.user_token)
            parser.parse_args([args.command, "--help"])
            return 2
    except ConfigError as exc:
        print("✗ {0}".format(exc), file=sys.stderr)
        return 1
    except MakervoxPublishError as exc:
        print("✗ {0}".format(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ncancelled", file=sys.stderr)
        return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
