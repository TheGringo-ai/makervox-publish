"""The CLI exists and its entry point resolves.

v0.1.0 shipped to PyPI declaring a console script that pointed at a module which
did not exist, so `makervox-publish` was a ModuleNotFoundError for anyone who
installed it. Importing the package does not touch its entry points, which is
why every other check passed.

These tests are cheap and would have caught it.
"""

from __future__ import annotations

import pytest

from makervox_publish.cli.main import build_parser, main


def test_the_declared_entry_point_is_importable():
    """pyproject points the console script here; it must resolve."""
    from makervox_publish.cli.main import main as entry
    assert callable(entry)


def test_console_script_target_matches_pyproject():
    """The declared target and the real module must not drift apart."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    text = (root / "pyproject.toml").read_text()
    match = re.search(r'^makervox-publish\s*=\s*"([^"]+)"', text, re.M)
    assert match, "no console script declared"
    module_path, func = match.group(1).split(":")
    module = __import__(module_path, fromlist=[func])
    assert callable(getattr(module, func))


def test_no_arguments_prints_help_and_exits_nonzero():
    assert main([]) == 2


def test_version_flag_exits_cleanly():
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0


def test_auth_and_status_are_both_reachable():
    parser = build_parser()
    assert parser.parse_args(["status"]).command == "status"
    args = parser.parse_args(["auth", "tiktok", "someacct"])
    assert (args.command, args.platform, args.account) == ("auth", "tiktok", "someacct")


def test_meta_auth_requires_a_user_token():
    """A silent default here would store nothing and report success."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["auth", "facebook"])


def test_importing_the_cli_does_not_pull_in_platform_modules():
    """The no-import-side-effects rule applies to the CLI too.

    Commands import what they need inside the function, so `--help` does not pay
    for four platform clients or touch the network.
    """
    import subprocess
    import sys

    code = (
        "import sys; import makervox_publish.cli.main;"
        "loaded = [m for m in sys.modules if m.startswith('makervox_publish.platforms')];"
        "print(loaded)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "[]", out.stdout
