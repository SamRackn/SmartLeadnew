"""Minimal, dependency-free HTML parsing for `LiveCompanyAdapter`.

No `bs4`/`lxml` in this project's dependency list, and the brief is explicit
about not adding a heavyweight scraping dependency without saying why we need
it -- for a Phase-1 spike measuring signal coverage, the stdlib
`html.parser.HTMLParser` is enough to pull links, a title, meta description
and dated post blocks out of ordinary marketing/blog/ATS HTML. It will not
handle JS-rendered SPAs (a real and reportable limitation -- see the
measurement writeup), but it needs no extra install.

Everything here is pure string-in, data-out: no network, no adapter state.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

__all__ = [
    "Link",
    "extract_links",
    "extract_title",
    "extract_meta_description",
    "extract_time_tags",
    "find_page_links",
    "extract_job_items",
    "extract_dated_items",
    "PAGE_KEYWORDS",
    "ATS_HOST_PATTERNS",
]


class Link:
    __slots__ = ("href", "text", "start", "end")

    def __init__(self, href: str, text: str, start: int, end: int) -> None:
        self.href = href
        self.text = text
        self.start = start
        self.end = end

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Link(href={self.href!r}, text={self.text!r})"


class _Collector(HTMLParser):
    """One pass: anchors (with source offsets), `<title>`, `<meta>`, `<time>`."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[Link] = []
        self.title: str | None = None
        self.meta_description: str | None = None
        self.time_tags: list[tuple[str | None, str, int]] = []  # (datetime, text, pos)

        self._a_href: str | None = None
        self._a_text: list[str] = []
        self._a_start: int = 0
        self._in_title = False
        self._title_parts: list[str] = []
        self._time_dt: str | None = None
        self._time_text: list[str] = []
        self._time_start: int = 0
        self._in_time = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = dict(attrs)
        pos = self._offset()
        if tag == "a" and d.get("href"):
            self._a_href = d["href"]
            self._a_text = []
            self._a_start = pos
        elif tag == "title":
            self._in_title = True
            self._title_parts = []
        elif tag == "meta":
            name = (d.get("name") or d.get("property") or "").strip().lower()
            if name in ("description", "og:description") and d.get("content"):
                if not self.meta_description:
                    self.meta_description = d["content"].strip()
        elif tag == "time":
            self._in_time = True
            self._time_dt = d.get("datetime")
            self._time_text = []
            self._time_start = pos

    def handle_endtag(self, tag: str) -> None:
        pos = self._offset()
        if tag == "a" and self._a_href is not None:
            text = " ".join("".join(self._a_text).split())
            self.links.append(Link(self._a_href, text, self._a_start, pos))
            self._a_href = None
        elif tag == "title":
            self._in_title = False
            self.title = " ".join("".join(self._title_parts).split()) or None
        elif tag == "time":
            self._in_time = False
            text = " ".join("".join(self._time_text).split())
            self.time_tags.append((self._time_dt, text, self._time_start))

    def handle_data(self, data: str) -> None:
        if self._a_href is not None:
            self._a_text.append(data)
        if self._in_title:
            self._title_parts.append(data)
        if self._in_time:
            self._time_text.append(data)

    def _offset(self) -> int:
        # CDATA_CONTENT_ELEMENTS aside, getpos() gives (line, col); we only use
        # this for coarse "nearby in the document" proximity, so line*bignum+col
        # is a fine monotonic-enough proxy without re-deriving a char offset.
        line, col = self.getpos()
        return line * 100_000 + col


def _parse(html: str) -> _Collector:
    c = _Collector()
    try:
        c.feed(html)
        c.close()
    except Exception:
        # A malformed fragment must not take out the whole fetch -- return
        # whatever was collected before the parser choked.
        pass
    return c


def extract_links(html: str, base_url: str) -> list[Link]:
    c = _parse(html)
    out = []
    for link in c.links:
        href = link.href.strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urljoin(base_url, href)
        out.append(Link(absolute, link.text, link.start, link.end))
    return out


def extract_title(html: str) -> str | None:
    return _parse(html).title


def extract_meta_description(html: str) -> str | None:
    return _parse(html).meta_description


def extract_time_tags(html: str) -> list[tuple[str | None, str, int]]:
    return _parse(html).time_tags


# --------------------------------------------------------------------------
# Page discovery: careers / blog / changelog / press, from homepage nav
# --------------------------------------------------------------------------

PAGE_KEYWORDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # kind -> (anchor-text keywords, href-path keywords)
    "careers": (
        ("career", "careers", "jobs", "join us", "join our team", "we're hiring",
         "open positions", "work with us", "open roles"),
        ("career", "careers", "jobs"),
    ),
    "blog": (("blog",), ("blog",)),
    "changelog": (
        ("changelog", "release notes", "product updates", "what's new", "updates", "whats new"),
        ("changelog", "release-notes", "releases", "whats-new", "updates"),
    ),
    "press": (
        ("press", "news", "newsroom", "media", "in the news"),
        ("press", "news", "newsroom", "media"),
    ),
}

FALLBACK_PATHS: dict[str, tuple[str, ...]] = {
    "careers": ("/careers", "/jobs", "/company/careers", "/about/careers"),
    "blog": ("/blog", "/news/blog"),
    "changelog": ("/changelog", "/release-notes", "/updates"),
    "press": ("/press", "/newsroom", "/news"),
}

# Third-party ATS hosts a "careers" nav link commonly points straight at. If we
# land on one of these, the WHOLE page is a job board -- every anchor that
# looks like a job link is a candidate, not just ones near careers-y text.
ATS_HOST_PATTERNS: tuple[str, ...] = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "workable.com",
    "breezy.hr",
    "smartrecruiters.com",
    "myworkdayjobs.com",
    "bamboohr.com",
    "jazzhr.com",
    "recruitee.com",
    "teamtailor.com",
)

_NAV_TEXT_STOPWORDS = {
    "home", "about", "contact", "contact us", "login", "log in", "sign in",
    "sign up", "pricing", "product", "products", "solutions", "resources",
    "support", "help", "docs", "documentation", "privacy", "terms", "legal",
    "cookie", "cookies", "sitemap", "search",
}


def _tokens(text: str) -> set[str]:
    """Whole-word tokens, splitting on anything that is not alphanumeric.

    Plain substring matching ("news" in "newsletter") is a real bug we hit in
    practice -- a footer "Newsletter" link was being discovered as the press
    page. Token-subset matching only fires on whole words, so "news" matches
    a link literally titled "News" but not "Newsletter".
    """
    return set(re.split(r"[^a-z0-9]+", text.lower())) - {""}


def _keyword_hits(haystack_tokens: set[str], keywords: tuple[str, ...]) -> bool:
    return any(_tokens(kw) <= haystack_tokens for kw in keywords)


def find_page_links(homepage_html: str, homepage_url: str) -> dict[str, str]:
    """Best-effort map of page kind -> absolute URL, from the homepage's own
    links (nav, footer, wherever). First plausible match wins per kind."""
    links = extract_links(homepage_html, homepage_url)
    found: dict[str, str] = {}
    for kind, (text_kw, href_kw) in PAGE_KEYWORDS.items():
        best: str | None = None
        for link in links:
            text_tokens = _tokens(link.text)
            path_tokens = _tokens(urlparse(link.href).path)
            if _keyword_hits(text_tokens, text_kw) or _keyword_hits(path_tokens, href_kw):
                best = link.href
                break
        if best:
            found[kind] = best
    return found


def fallback_candidates(kind: str, homepage_url: str) -> list[str]:
    return [urljoin(homepage_url, p) for p in FALLBACK_PATHS.get(kind, ())]


def is_ats_host(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(pattern in host for pattern in ATS_HOST_PATTERNS)


# --------------------------------------------------------------------------
# Careers page -> job items
# --------------------------------------------------------------------------

_JOB_HREF_HINTS = ("job", "position", "opening", "req", "vacan", "opportunit")
_JOB_TITLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 /,&'\-()]{2,90}$")
_JOB_PAGE_STOPWORDS = _NAV_TEXT_STOPWORDS | {
    "benefits", "culture", "our values", "values", "faq", "faqs", "diversity",
    "perks", "why join us", "our team", "life at", "life here", "our culture",
    "equal opportunity", "all jobs", "view all", "load more", "back to careers",
    "back to jobs", "apply now", "share", "open positions",
}


def extract_job_items(html: str, page_url: str) -> list[dict]:
    """Pull individual job-req links off a careers/job-board page.

    Two modes: on a recognised ATS host, the whole page IS the job list, so
    every anchor with plausible title text is a candidate. On a company's own
    careers page, an anchor also has to look job-shaped: either its href has
    a job-ish path segment ("job", "position", "req"...) or it is a sub-path
    of the careers page itself (``/careers/<slug>``, the single most common
    pattern for a homegrown careers page). That still lets through the
    occasional "Benefits" or "FAQ" sub-link, which the stopword list above
    catches by title text -- a heuristic, not a guarantee.
    """
    on_ats = is_ats_host(page_url)
    page_path = urlparse(page_url).path.rstrip("/").lower()
    links = extract_links(html, page_url)
    seen: set[str] = set()
    items: list[dict] = []
    for link in links:
        text = link.text.strip()
        if not text or text.lower() in _JOB_PAGE_STOPWORDS:
            continue
        if not _JOB_TITLE_RE.match(text):
            continue
        href_path = urlparse(link.href).path.lower()
        if href_path == page_path:
            # Same page, different query string only (?team=..., ?location=...)
            # -- a filter control on the listing page, not a distinct req. A
            # real posting almost always has its own path or id.
            continue
        looks_job_shaped = any(hint in href_path for hint in _JOB_HREF_HINTS) or (
            bool(page_path) and href_path.startswith(page_path + "/")
        )
        if not (on_ats or looks_job_shaped):
            continue
        if link.href in seen:
            continue
        seen.add(link.href)
        items.append({"key": link.href, "url": link.href, "text": text, "published_at": None})
    return items


def find_external_ats_link(html: str, page_url: str) -> str | None:
    """A company's own /careers page frequently just redirects/links out to a
    hosted board. Return the first such link, if the page itself yielded no
    job items directly."""
    for link in extract_links(html, page_url):
        if is_ats_host(link.href):
            return link.href
    return None


# --------------------------------------------------------------------------
# Blog / changelog / press -> dated items
# --------------------------------------------------------------------------

_ARTICLE_BLOCK_RE = re.compile(r"<article\b.*?</article>", re.IGNORECASE | re.DOTALL)
_LI_BLOCK_RE = re.compile(r"<li\b.*?</li>", re.IGNORECASE | re.DOTALL)
_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_LONG_DATE_RE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t|tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+"
    r"(\d{1,2}),?\s+(\d{4})\b"
)

_MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
            ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
            ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for m in names
}


def _normalize_date(text: str) -> str | None:
    m = _ISO_DATE_RE.search(text)
    if m:
        return m.group(1)
    m = _LONG_DATE_RE.search(text)
    if m:
        month_name, day, year = m.groups()
        month = _MONTHS.get(month_name.lower().rstrip("."))
        if month:
            return f"{year}-{month:02d}-{int(day):02d}"
    return None


def extract_dated_items(html: str, page_url: str) -> list[dict]:
    """Best-effort extraction of dated posts from a blog/changelog/press page.

    Strategy: split into repeated block-level containers (`<article>`, then
    `<li>` as a fallback for list-style changelogs), and within each block take
    the first plausible title link plus the first date found anywhere in the
    block (a `<time datetime=...>` attribute, or an ISO/"Sep 10, 2026"-style
    string in the block text). Blocks with neither a link nor a date are
    dropped. This is a heuristic, not a full DOM walk -- it will miss
    JS-rendered post lists entirely, which is a real, reportable limitation
    rather than something to paper over.
    """
    blocks = _ARTICLE_BLOCK_RE.findall(html)
    # `<article>` is a real content-block signal; a bare `<li>` is not -- every
    # page's header/footer mega-menu is a pile of `<li>` too. So the `<li>`
    # fallback only keeps blocks that actually carry a date: an undated `<li>`
    # is indistinguishable from a nav item, and admitting it would silently
    # relabel nav copy as "recently dated content".
    require_date = not blocks
    if not blocks:
        blocks = _LI_BLOCK_RE.findall(html)

    items: list[dict] = []
    seen: set[str] = set()
    for block in blocks:
        links = extract_links(block, page_url)
        title_link = next((l for l in links if len(l.text.strip()) >= 4), None)
        if title_link is None:
            continue
        if title_link.href in seen:
            continue

        published_at = None
        time_tags = extract_time_tags(block)
        for dt_attr, dt_text, _ in time_tags:
            if dt_attr:
                published_at = _normalize_date(dt_attr) or dt_attr[:10]
                break
            candidate = _normalize_date(dt_text)
            if candidate:
                published_at = candidate
                break
        if published_at is None:
            published_at = _normalize_date(block)

        if require_date and published_at is None:
            continue

        seen.add(title_link.href)
        items.append(
            {
                "key": title_link.href,
                "url": title_link.href,
                "text": title_link.text.strip(),
                "published_at": published_at,
            }
        )

    if items:
        return items

    # No repeated block structure found (common on simpler pages): fall back to
    # whole-page anchor + time-tag proximity. Pair each <time> with the nearest
    # preceding anchor -- most list templates put the date right next to the
    # title link.
    links = extract_links(html, page_url)
    time_tags = extract_time_tags(html)
    for dt_attr, dt_text, pos in time_tags:
        preceding = [l for l in links if l.start <= pos]
        if not preceding:
            continue
        nearest = max(preceding, key=lambda l: l.start)
        text = nearest.text.strip()
        if len(text) < 4 or nearest.href in seen:
            continue
        published_at = (dt_attr and (_normalize_date(dt_attr) or dt_attr[:10])) or _normalize_date(
            dt_text
        )
        seen.add(nearest.href)
        items.append(
            {"key": nearest.href, "url": nearest.href, "text": text, "published_at": published_at}
        )
    return items
