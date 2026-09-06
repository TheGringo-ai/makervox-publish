"""Link tagging and link stripping — shared by every platform.

TWO SEPARATE JOBS, BOTH OF THEM SCAR TISSUE
-------------------------------------------

1. TAGGING. The original tagger matched ONE literal domain, so every other
   account's links shipped untagged with no error at all. A silent no-op is
   worse than an obvious gap, so :class:`NoopTagger` is the DEFAULT and
   :class:`UtmTagger` takes an explicit domain list. Tag nothing, or say what to
   tag; never guess.

2. STRIPPING. Some platforms throttle reach on posts with links in the body, so
   the description goes link-free and the call to action rides in the first
   comment instead. The inherited implementation used a hand-tuned TLD
   allowlist, which quietly let every unlisted TLD through into a description
   that was supposed to be link-free. ``any_dot_tld`` is the default for that
   reason; ``tld_allowlist`` remains available and remains a trap.

Hashtags are never links: ``#moonlit`` must survive both operations untouched.
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from postvox.logging import get_logger

__all__ = [
    "LinkTagger",
    "NoopTagger",
    "UtmTagger",
    "strip_link_lines",
    "readable_text",
    "URL_RE",
]

log = get_logger(__name__)

#: A real URL with a scheme.
URL_RE = re.compile(r"https?://\S+", re.I)

#: A bare host with ANY dot-TLD of 2+ letters, not a curated list. Requires a
#: letter to start the TLD so "v21.0" and "1.5" are not mistaken for domains,
#: and refuses a leading "#" so hashtags never match.
_ANY_DOMAIN_RE = re.compile(
    r"(?<![#\w@.])\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b(?:/\S*)?", re.I
)


class LinkTagger:
    """The interface: ``__call__(text, **ctx) -> text``."""

    def __call__(self, text: str, **ctx: Any) -> str:  # pragma: no cover - interface
        raise NotImplementedError


class NoopTagger(LinkTagger):
    """THE DEFAULT. Returns the text unchanged.

    A package that ships a tagger pointed at somebody else's domain either leaks
    that identity or silently does nothing for everyone else. Doing nothing, on
    purpose and by default, is the honest option.
    """

    def __call__(self, text: str, **ctx: Any) -> str:
        return text or ""

    def describe(self) -> str:
        return "no-op (links.tagger is not configured)"


class UtmTagger(LinkTagger):
    """Append campaign parameters to URLs on domains YOU list.

    ``params`` values expand ``{platform}``, ``{medium}``, ``{account}`` and
    ``{date}`` (a ``datetime.date``, so ``{date:%Y%m%d}`` works). Existing query
    parameters are preserved and never overwritten — a link that already carries
    a ``utm_source`` was tagged deliberately by whoever wrote the caption.
    """

    def __init__(
        self,
        domains: Sequence[str] = (),
        params: Optional[Mapping[str, str]] = None,
        match_scheme_less: bool = False,
        overwrite_existing: bool = False,
    ) -> None:
        self.domains = tuple(d.lower().lstrip(".") for d in (domains or ()))
        self.params = dict(params or {})
        self.match_scheme_less = bool(match_scheme_less)
        self.overwrite_existing = bool(overwrite_existing)
        if not self.domains:
            log.warning(
                "links.tagger is a UtmTagger with an EMPTY domain list, so it "
                "will tag nothing. List the hosts you own, or use NoopTagger."
            )

    @classmethod
    def from_config(cls, spec) -> "UtmTagger":
        options = dict(getattr(spec, "options", {}) or {})
        return cls(
            domains=options.get("domains", ()),
            params=options.get("params", {}),
            match_scheme_less=bool(options.get("match_scheme_less", False)),
            overwrite_existing=bool(options.get("overwrite_existing", False)),
        )

    # -- matching ------------------------------------------------------------ #
    def _owns(self, host: str) -> bool:
        host = (host or "").lower().split(":")[0]
        return any(host == d or host.endswith("." + d) for d in self.domains)

    def _expand(self, **ctx: Any):
        values = {
            "platform": ctx.get("platform", ""),
            "medium": ctx.get("medium", ""),
            "account": ctx.get("account", ""),
            "date": ctx.get("date") or datetime.date.today(),
        }
        out = {}
        for name, template in self.params.items():
            try:
                out[name] = str(template).format(**values)
            except (KeyError, IndexError, ValueError) as exc:
                # A bad template must not cost the post. Ship the link untagged
                # and say which key is wrong.
                log.warning("links.tagger param %r has an unusable template (%s); "
                            "leaving it off", name, exc)
        return out

    def _tag_url(self, url: str, extra) -> str:
        trailing = ""
        while url and url[-1] in ").,;!?'\"":
            trailing = url[-1] + trailing
            url = url[:-1]
        parts = urlsplit(url if "//" in url else "https://" + url)
        if not self._owns(parts.netloc):
            return url + trailing
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        for name, value in extra.items():
            if value and (self.overwrite_existing or name not in query):
                query[name] = value
        rebuilt = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )
        if "//" not in url:
            rebuilt = rebuilt.split("://", 1)[-1]
        return rebuilt + trailing

    def __call__(self, text: str, **ctx: Any) -> str:
        if not text or not self.domains or not self.params:
            return text or ""
        extra = self._expand(**ctx)
        if not extra:
            return text

        out = URL_RE.sub(lambda m: self._tag_url(m.group(0), extra), text)
        if self.match_scheme_less:
            def _bare(match):
                token = match.group(0)
                if "://" in token:
                    return token
                return self._tag_url(token, extra)
            out = _ANY_DOMAIN_RE.sub(_bare, out)
        return out

    def describe(self) -> str:
        return "UTM tagger for {0}".format(", ".join(self.domains) or "(no domains)")


# --------------------------------------------------------------------------- #
# stripping
# --------------------------------------------------------------------------- #
def _line_has_link(line: str, mode: str, tld_allowlist: Sequence[str],
                   keep_hashtags: bool) -> bool:
    probe = line
    if keep_hashtags:
        # "#moonlit" is not a link. Remove hashtag tokens before looking, so a
        # tag that happens to contain a dot cannot drag a whole line out of the
        # description.
        probe = re.sub(r"(?<!\w)#\S+", " ", probe)
    if URL_RE.search(probe):
        return True
    if mode == "urls_only":
        return False
    if mode == "tld_allowlist":
        if not tld_allowlist:
            return False
        pattern = r"(?<![#\w@.])\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:{0})\b".format(
            "|".join(re.escape(t.lstrip(".")) for t in tld_allowlist)
        )
        return bool(re.search(pattern, probe, re.I))
    return bool(_ANY_DOMAIN_RE.search(probe))


def strip_link_lines(
    text: str,
    *,
    mode: str = "any_dot_tld",
    tld_allowlist: Sequence[str] = (),
    keep_hashtags: bool = True,
    collapse_blank_lines: bool = True,
) -> str:
    """Drop every LINE that carries an outbound link or bare domain.

    Whole lines, not just the link token: a caption line reads "Full write-up,
    free -> example.test", and removing only the URL leaves a dangling
    preposition in the description.
    """
    out = []
    for line in (text or "").splitlines():
        if _line_has_link(line, mode, tld_allowlist, keep_hashtags):
            continue
        if collapse_blank_lines and not line.strip() and (not out or not out[-1].strip()):
            # Collapse the blank the removed line left behind.
            continue
        out.append(line)
    return "\n".join(out).strip()


def strip_from_config(text: str, strip_cfg) -> str:
    """``strip_link_lines`` driven by a :class:`postvox.config.StripConfig`."""
    if strip_cfg is None:
        return text or ""
    return strip_link_lines(
        text,
        mode=str(getattr(strip_cfg, "mode", "any_dot_tld")),
        tld_allowlist=tuple(getattr(strip_cfg, "tld_allowlist", ())),
        keep_hashtags=bool(getattr(strip_cfg, "keep_hashtags", True)),
        collapse_blank_lines=bool(getattr(strip_cfg, "collapse_blank_lines", True)),
    )


def readable_text(caption: str) -> str:
    """The caption as a clean readable body: drop the trailing hashtag block.

    A feed that shows a wall of tags instead of the actual writing reads as
    spam, and the hashtags have already done their job on the media post.
    """
    lines = [ln for ln in (caption or "").splitlines() if not ln.strip().startswith("#")]
    return "\n".join(lines).strip()
