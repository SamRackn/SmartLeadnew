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

import json
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Any, Mapping
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
    "extract_json_ld_job_urls",
    "extract_json_ld_dated_urls",
    "find_feed_link",
    "parse_feed_dates",
    "extract_sitemap_urls",
    "page_has_apply_signal",
    "PAGE_KEYWORDS",
    "ATS_HOST_PATTERNS",
    "JOB_TITLE_NOUNS",
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
        self.feed_links: list[tuple[str, str]] = []  # (href, type)

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
        elif tag == "link":
            rel = (d.get("rel") or "").strip().lower()
            type_ = (d.get("type") or "").strip().lower()
            href = d.get("href")
            if href and rel == "alternate" and ("rss" in type_ or "atom" in type_):
                self.feed_links.append((href, type_))

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


def find_feed_link(html: str, base_url: str) -> str | None:
    """First RSS/Atom `<link rel="alternate" type="application/(rss|atom)+xml">`
    on this page (normally the homepage `<head>`), resolved absolute.

    Most sites do not publish one; ``None`` is a normal "nothing to
    corroborate with", not a failure -- the caller treats it exactly like any
    other best-effort discovery (`find_page_links`, `find_external_ats_link`).
    """
    c = _parse(html)
    for href, _type in c.feed_links:
        return urljoin(base_url, href)
    return None


def extract_sitemap_urls(xml_text: str) -> set[str]:
    """URLs listed in a `sitemap.xml` (or a sitemap-index's children, one
    level deep). Returns an empty set on anything that does not parse --
    a sitemap is corroborating evidence, never a requirement, so a malformed
    or absent one degrades silently rather than raising.

    Why this is worth having at all: a sitemap is generated by the site's own
    CMS/build from its real, server-known set of pages. A client-side-only
    filter route (the Trello-hub failure mode) is essentially never in it,
    because there is no distinct server-rendered page behind it to list. A
    URL's presence here is therefore the site's own inventory vouching for it
    as a real, distinct page -- independent of, and cheaper than, fetching
    the page itself.
    """
    urls: set[str] = set()
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return urls
    for elem in root.iter():
        if _local_tag(elem.tag) == "loc" and elem.text:
            urls.add(elem.text.strip())
    return urls


_APPLY_TEXT_RE = re.compile(r"\bapply\b", re.IGNORECASE)


def page_has_apply_signal(html: str, page_url: str) -> bool:
    """Does this (candidate job req's own) page carry a real "apply" signal:
    an anchor whose text says "apply" (whole word, so "apply now" counts and
    "Applying to jobs" in a nav blurb still would too -- this is a last-resort
    per-candidate check, only ever run on something already ambiguous, so a
    slightly loose match costs little), or an outbound link to a recognised
    ATS host (an "Apply via Greenhouse" pattern)?

    This is deliberately the MOST expensive corroboration signal available
    (it requires fetching the candidate's own page) and is meant to be used
    sparingly, on a bounded number of otherwise-low-confidence candidates --
    see `LiveCompanyAdapter`'s cap. A department filter page or a policy page
    essentially never has a real apply mechanism on it.

    Deliberately reads the RAW anchor list (`_parse(html).links`), not the
    filtered/absolute `extract_links` -- a same-page jump to an embedded
    application form (``href="#apply"``) is a real apply signal and is
    common on modern job pages, but `extract_links` drops fragment-only
    hrefs on purpose (they are not a distinct page to discover). Confirmed
    live: buffer.com's own careers pages use exactly this pattern, and this
    check would silently miss every one of them without reading the raw
    anchors here.
    """
    raw_links = _parse(html).links
    for link in raw_links:
        href = (link.href or "").strip()
        if _APPLY_TEXT_RE.search(link.text or ""):
            return True
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        if is_ats_host(urljoin(page_url, href)):
            return True
    return False


def _local_tag(tag: str) -> str:
    """Strip an XML namespace off an ElementTree tag name (`{ns}loc` -> `loc`)."""
    return tag.rsplit("}", 1)[-1].lower()


def parse_feed_dates(feed_xml: str) -> dict[str, str]:
    """Best-effort RSS 2.0 / Atom entry -> normalized published date, keyed by
    the entry's own `<link>` URL exactly as the feed states it (feed links are
    already absolute per both specs -- no `base_url` to join against).

    Never raises: a feed we cannot parse, or that has no recognisable
    item/entry shape, contributes no corroboration, which is exactly as good
    as not having found a feed at all. This is deliberately a minimal RSS 2.0
    / Atom 1.0 reader, not a general feed-format library.
    """
    out: dict[str, str] = {}
    try:
        root = ET.fromstring(feed_xml)
    except ET.ParseError:
        return out

    for elem in root.iter():
        local = _local_tag(elem.tag)
        if local == "item":  # RSS 2.0
            link: str | None = None
            date_text: str | None = None
            for child in elem:
                clocal = _local_tag(child.tag)
                if clocal == "link" and child.text:
                    link = child.text.strip()
                elif clocal in ("pubdate", "date") and child.text and date_text is None:
                    date_text = child.text.strip()
            if link and date_text:
                normalized = _normalize_date(date_text)
                if normalized:
                    out[link] = normalized
        elif local == "entry":  # Atom
            link = None
            date_text = None
            for child in elem:
                clocal = _local_tag(child.tag)
                if clocal == "link" and link is None:
                    href = child.attrib.get("href")
                    rel = child.attrib.get("rel", "alternate")
                    if href and rel in ("alternate", ""):
                        link = href.strip()
                elif clocal in ("published", "updated") and child.text:
                    if date_text is None or clocal == "published":
                        date_text = child.text.strip()
            if link and date_text:
                normalized = _normalize_date(date_text)
                if normalized:
                    out[link] = normalized
    return out


# --------------------------------------------------------------------------
# Careers page -> job items
# --------------------------------------------------------------------------

_JOB_HREF_HINTS = ("job", "position", "opening", "req", "vacan", "opportunit")

# Extensions a real, browsable job requisition page never has. A path
# *segment* whose stem happens to start with a hint but which is actually a
# feed/data/asset file (basecamp's own careers page linked its RSS feed as
# "grab the RSS feed" -> /feed/jobs.xml) is not a job posting -- it is a
# non-HTML resource that merely has "jobs" in its filename. Kept as a
# denylist of known non-page extensions, rather than an allowlist of page
# extensions, because enumerating every real page extension a company might
# use (.html, .php, .aspx, or nothing at all) is open-ended; the set of
# "this is definitely not a page a person reads a job req on" formats is not.
_NON_PAGE_HREF_EXTENSIONS = {
    "xml", "json", "csv", "tsv", "rss", "atom", "rdf",
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "zip", "rar", "7z", "gz", "tar",
    "ics", "txt",
    "jpg", "jpeg", "png", "gif", "svg", "webp", "ico",
    "css", "js", "map",
    "mp3", "mp4", "wav", "avi", "mov",
    "woff", "woff2", "ttf", "eot",
}


def _path_segment_matches_job_hint(path: str) -> bool:
    """Whole-path-segment version of the old ``any(hint in path for hint in
    _JOB_HREF_HINTS)`` substring check.

    The substring form matched a hint ANYWHERE in the path string, including
    mid-word and spanning segment/hyphen boundaries -- so
    ".../find-their-next-job/" (Zapier's "Next Play Program" blog post, not a
    posting) matched on "job" exactly as readily as a real "/jobs/12345"
    would. A hint is only real corroboration if it actually NAMES the
    segment: the segment, split on "/", must *start with* the hint (so
    "jobs", "jobs-list", "job-1234" all still match; "find-their-next-job"
    does not, because "job" is buried at the end behind other words, not the
    segment's own name).

    A segment is skipped entirely -- ineligible regardless of its stem -- if
    it carries a known non-page file extension (see
    `_NON_PAGE_HREF_EXTENSIONS`): a job requisition is always a page a human
    reads, never a feed/document/asset file.
    """
    for segment in path.split("/"):
        if not segment:
            continue
        stem = segment
        if "." in segment:
            stem, _, ext = segment.rpartition(".")
            if ext in _NON_PAGE_HREF_EXTENSIONS:
                continue
            if not stem:
                stem = segment
        if any(stem.startswith(hint) for hint in _JOB_HREF_HINTS):
            return True
    return False


_JOB_TITLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 /,&'\-()]{2,90}$")
_JOB_PAGE_STOPWORDS = _NAV_TEXT_STOPWORDS | {
    "benefits", "culture", "our values", "values", "faq", "faqs", "diversity",
    "perks", "why join us", "our team", "life at", "life here", "our culture",
    "equal opportunity", "all jobs", "view all", "load more", "back to careers",
    "back to jobs", "apply now", "share", "open positions",
}

# Whole-word vocabulary that plausibly names an actual JOB TITLE, as opposed to
# a department/category filter ("Engineering", "Design", "All Teams") or a
# policy page ("Equal Employment Opportunity", "Diversity, Equity & Inclusion").
# This is deliberately a different list from score.py's SUPPORTED/OFF_TARGET
# role-fit vocabulary: this one answers "is this plausibly a job title at all",
# not "is it a role our product supports". Matched on whole tokens via
# `_tokens()`, so "Engineering" (a filter) does not match "engineer" (a title).
JOB_TITLE_NOUNS: frozenset[str] = frozenset(
    {
        "engineer", "engineers", "manager", "managers", "specialist",
        "specialists", "representative", "representatives", "director",
        "directors", "analyst", "analysts", "designer", "designers", "lead",
        "leads", "coordinator", "coordinators", "associate", "associates",
        "executive", "executives", "officer", "officers", "developer",
        "developers", "architect", "architects", "recruiter", "recruiters",
        "counsel", "administrator", "administrators", "consultant",
        "consultants", "scientist", "scientists", "researcher", "researchers",
        "technician", "technicians", "strategist", "strategists", "planner",
        "planners", "buyer", "buyers", "agent", "agents", "controller",
        "controllers", "president", "chief", "vp", "svp", "evp", "founder",
        "partner", "partners", "accountant", "accountants", "nurse", "nurses",
        "attorney", "attorneys", "counselor", "counselors", "bookkeeper",
        "paralegal", "paralegals", "supervisor", "supervisors", "producer",
        "producers", "editor", "editors", "intern",
    }
)

_JSONLD_SCRIPT_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def extract_json_ld_job_urls(html: str, base_url: str) -> set[str]:
    """Absolute URLs a JSON-LD ``JobPosting`` block on this page vouches for.

    Structured data the site itself publishes in a machine-readable,
    schema.org vocabulary is materially stronger evidence than "this href is a
    sub-path of the careers page" -- it is the site ASSERTING "this is a job
    posting", not us inferring it from URL shape. Deliberately minimal: enough
    to recognise the common single-object and ``@graph`` shapes real
    careers/ATS pages emit, not a general JSON-LD/schema.org engine. A page
    with malformed or absent JSON-LD simply contributes no URLs here -- it
    never raises, since JSON-LD is corroborating evidence, not a requirement.
    """
    urls: set[str] = set()
    for block in _JSONLD_SCRIPT_RE.findall(html):
        try:
            data = json.loads(block)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        nodes: list[dict] = []
        for candidate in data if isinstance(data, list) else [data]:
            if not isinstance(candidate, dict):
                continue
            graph = candidate.get("@graph")
            if isinstance(graph, list):
                nodes.extend(n for n in graph if isinstance(n, dict))
            else:
                nodes.append(candidate)
        for node in nodes:
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if "JobPosting" not in types:
                continue
            url = node.get("url")
            if url:
                urls.add(urljoin(base_url, str(url)))
    return urls


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

    Each returned item also carries ``identification_method`` and
    ``confidence`` (see below) -- this is the provenance-at-ingest fix for
    CLAUDE.md's known guardrail gap: a job req identified ONLY by URL shape
    (a sub-path of the careers page, nothing else corroborating it) is
    indistinguishable from a department filter control (Trello-style JS hubs)
    or a policy page sharing the same path pattern as real reqs
    (Zapier-style mixed careers paths) -- both real measured W3-spike false
    positives. That case is tagged ``confidence="low"`` /
    ``identification_method="url_shape_only"``; `company.pages_payload_to_facts`
    reads it and refuses to admit it at the JOB_POSTING (STRONG-tier) kind.

    Confidence ladder, strongest first:
      * ``json_ld``     -- a JSON-LD ``JobPosting`` block on the page names
                            this exact URL. The site's own structured
                            assertion, not an inference.
      * ``ats_host``     -- the whole page is a recognised ATS/job-board host;
                             every anchor there really is a listing.
      * ``href_keyword`` -- a WHOLE path segment of the href starts with a
                             job-ish hint ("job", "position", "req"...) --
                             not merely a substring anywhere in the path.
                             ("/jobs/12345" matches; ".../find-their-
                             next-job/" and "/feed/jobs.xml" do not -- see
                             `_path_segment_matches_job_hint`.)
      * ``title_signal`` -- the only shape evidence is the sub-path pattern,
                             but the anchor text contains a real job-title noun
                             ("Manager", "Engineer", "Specialist"...) -- a
                             department filter or policy page essentially never
                             does.
      * ``url_shape_only`` -- sub-path pattern and nothing else. LOW
                               confidence; not trustworthy enough for the
                               STRONG tier on its own. `LiveCompanyAdapter`
                               gets one more, more expensive, chance to
                               rescue exactly this bucket -- see
                               `page_has_apply_signal` and its bounded
                               per-company cap.

    A sitemap listing is deliberately NOT in this ladder, despite
    `extract_sitemap_urls` existing and being wired up to fetch one. It was
    tried and measured live against a real careers site (asana.com) rather
    than assumed safe: the site's own sitemap.xml lists its per-location and
    per-department FILTER pages ("Chicago", "Dublin", "Business &
    Marketing"...) as real, distinct, crawlable URLs right alongside actual
    reqs -- so "this URL is in the sitemap" turned out to prove "this is a
    real page", not "this is a job requisition", and would have promoted 19
    department/location names to JOB_POSTING at once. They landed 10 points
    under the scoring threshold only because they were undated; a company
    whose location pages carry a `<time>` tag would have cleared it. Kept as
    a documented, measured rejection rather than silently dropped, in case a
    future path-shape heuristic can distinguish "a specific req" from "a
    facet page" well enough to make it safe.
    """
    on_ats = is_ats_host(page_url)
    page_path = urlparse(page_url).path.rstrip("/").lower()
    links = extract_links(html, page_url)
    structured_urls = extract_json_ld_job_urls(html, page_url)
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
        href_keyword_hit = _path_segment_matches_job_hint(href_path)
        looks_job_shaped = href_keyword_hit or (
            bool(page_path) and href_path.startswith(page_path + "/")
        )
        if not (on_ats or looks_job_shaped):
            continue
        if link.href in seen:
            continue
        seen.add(link.href)

        title_noun = next((t for t in _tokens(text) if t in JOB_TITLE_NOUNS), None)
        # A job-ish keyword found only in the shared prefix this item inherits
        # from the careers page's OWN path (e.g. the careers page lives at
        # "/jobs", so literally every sub-path contains "job") is not evidence
        # about THIS item -- it is just where the page happens to live. Only a
        # keyword in the part of the path specific to this item counts as
        # corroboration.
        relative_path = (
            href_path[len(page_path) :] if page_path and href_path.startswith(page_path) else href_path
        )
        href_keyword_corroborates = _path_segment_matches_job_hint(relative_path)
        if link.href in structured_urls:
            method, confidence = "json_ld", "high"
        elif on_ats:
            method, confidence = "ats_host", "high"
        elif href_keyword_corroborates:
            method, confidence = "href_keyword", "high"
        elif title_noun:
            method, confidence = "title_signal", "medium"
        else:
            method, confidence = "url_shape_only", "low"

        items.append(
            {
                "key": link.href,
                "url": link.href,
                "text": text,
                "published_at": None,
                "identification_method": method,
                "confidence": confidence,
            }
        )
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
# Trailing lookahead is `(?!\d)`, not `\b` -- a real ISO 8601 timestamp is
# routinely glued straight to a "T" time separator ("2026-09-16T10:00:00Z",
# the Atom <updated>/<published> shape), and "T" is a word character, so
# `\b` would refuse to match right after the date. `(?!\d)` still blocks a
# false match inside a longer digit run.
_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})(?!\d)")
_LONG_DATE_RE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t|tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+"
    r"(\d{1,2}),?\s+(\d{4})\b"
)
# RFC 822 date order (RSS `pubDate`, e.g. "Tue, 15 Sep 2026 00:00:00 GMT") --
# day before month name, the reverse of `_LONG_DATE_RE` above.
_RFC822_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t|tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+(\d{4})\b"
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
    m = _RFC822_DATE_RE.search(text)
    if m:
        day, month_name, year = m.groups()
        month = _MONTHS.get(month_name.lower().rstrip("."))
        if month:
            return f"{year}-{month:02d}-{int(day):02d}"
    return None


_DATED_JSONLD_TYPES = {"Article", "BlogPosting", "NewsArticle", "TechArticle"}


def extract_json_ld_dated_urls(html: str, base_url: str) -> dict[str, str]:
    """Absolute URL -> ISO date, for JSON-LD `Article`/`BlogPosting`/
    `NewsArticle`/`TechArticle` nodes that carry a `datePublished` (or
    `dateCreated` as a fallback). Mirrors `extract_json_ld_job_urls` -- a
    listing page's own structured data naming this exact URL and date is
    stronger evidence than anything we infer from the page's visible HTML.
    Walks nested structures (`@graph`, a `Blog.blogPost` array, an
    `ItemList.itemListElement`...) a few levels deep; malformed or absent
    JSON-LD simply contributes nothing here, same as `extract_json_ld_job_urls`.
    """
    out: dict[str, str] = {}
    for block in _JSONLD_SCRIPT_RE.findall(html):
        try:
            data = json.loads(block)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        _collect_dated_jsonld_nodes(data, base_url, out)
    return out


def _collect_dated_jsonld_nodes(node: Any, base_url: str, out: dict[str, str], *, depth: int = 0) -> None:
    if depth > 4:
        return
    if isinstance(node, list):
        for item in node:
            _collect_dated_jsonld_nodes(item, base_url, out, depth=depth + 1)
        return
    if not isinstance(node, dict):
        return
    types = node.get("@type")
    types = types if isinstance(types, list) else [types]
    if any(t in _DATED_JSONLD_TYPES for t in types):
        url = node.get("url") or node.get("mainEntityOfPage") or node.get("@id")
        if isinstance(url, dict):
            url = url.get("@id") or url.get("url")
        date = node.get("datePublished") or node.get("dateCreated")
        if url and date:
            out[urljoin(base_url, str(url))] = str(date)
    for value in node.values():
        if isinstance(value, (dict, list)):
            _collect_dated_jsonld_nodes(value, base_url, out, depth=depth + 1)


# Confidence for each way a date was attached to an item. `None` (no date
# found at all) is deliberately not "low" -- there is nothing to be WRONG
# about, and `score.py`'s existing `undated_multiplier` already discounts it
# heavily. The risk this ladder targets is specifically a WRONG date at FULL
# confidence, because recency is most of the MEDIUM-tier score: a misread
# date does not just add a bad fact, it adds one at maximum weight.
_DATE_METHOD_CONFIDENCE: dict[str, str] = {
    "json_ld": "high",
    "feed": "high",
    "time_attr": "high",
    "time_text": "medium",
    "text_scan": "low",
    "proximity_fallback": "low",
    "undated": "n/a",
}


def extract_dated_items(
    html: str,
    page_url: str,
    *,
    feed_dates: "Mapping[str, str] | None" = None,
) -> list[dict]:
    """Best-effort extraction of dated posts from a blog/changelog/press page.

    Strategy: split into repeated block-level containers (`<article>`, then
    `<li>` as a fallback for list-style changelogs), and within each block take
    the first plausible title link plus the first date found anywhere in the
    block (a `<time datetime=...>` attribute, or an ISO/"Sep 10, 2026"-style
    string in the block text). Blocks with neither a link nor a date are
    dropped. This is a heuristic, not a full DOM walk -- it will miss
    JS-rendered post lists entirely, which is a real, reportable limitation
    rather than something to paper over.

    Each item also carries ``identification_method`` / ``confidence`` -- the
    provenance-at-ingest fix for the same known guardrail gap as
    `extract_job_items`, one tier down: `blog_post` (42.0), `changelog`
    (45.0) and `press` (40.0) each clear the 40.0 scoring threshold ALONE when
    fresh, so a misidentified block is not a smaller problem than a
    misidentified job req, and a WRONG-but-fresh-looking date is the sharper
    failure mode here -- see `_DATE_METHOD_CONFIDENCE` above.

    Confidence ladder, strongest first:
      * ``json_ld`` / ``feed`` -- the site's own structured data (a JSON-LD
        `Article`/`BlogPosting`/... block, or an RSS/Atom feed entry
        discovered from the homepage) names this exact URL and date.
      * ``time_attr`` -- a `<time datetime=...>` attribute inside a genuine
        content block (`<article>`, or a dated `<li>`).
      * ``time_text`` -- a `<time>` tag is present, but only its rendered
        text (no machine-readable ``datetime`` attribute) parsed as a date.
      * ``text_scan`` -- no `<time>` tag at all; a date-shaped string was
        found by scanning the block's raw text. This is where a testimonial
        dateline, a "founded in 2019" mention or a copyright year can be
        misread as a publication date -- LOW confidence.
      * ``proximity_fallback`` -- no repeated block structure at all; the
        item comes from pairing a bare `<time>` tag with the nearest
        PRECEDING anchor anywhere on the page. The link/date PAIRING itself
        is a guess here, independent of how good the date parse is, so this
        is capped at LOW confidence even when the date came from a `<time
        datetime>` attribute (unless structured data independently confirms
        the same URL).
    """
    feed_dates = feed_dates or {}
    structured_dates = extract_json_ld_dated_urls(html, page_url)

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
        method: str | None = None
        time_tags = extract_time_tags(block)
        for dt_attr, dt_text, _ in time_tags:
            if dt_attr:
                published_at = _normalize_date(dt_attr) or dt_attr[:10]
                method = "time_attr"
                break
            candidate = _normalize_date(dt_text)
            if candidate:
                published_at = candidate
                method = "time_text"
                break
        if published_at is None:
            candidate = _normalize_date(block)
            if candidate:
                published_at = candidate
                method = "text_scan"

        if require_date and published_at is None:
            continue

        seen.add(title_link.href)
        abs_url = title_link.href
        if abs_url in structured_dates:
            published_at, method = structured_dates[abs_url], "json_ld"
        elif abs_url in feed_dates:
            published_at, method = feed_dates[abs_url], "feed"
        elif method is None:
            method = "undated"

        items.append(
            {
                "key": abs_url,
                "url": abs_url,
                "text": title_link.text.strip(),
                "published_at": published_at,
                "identification_method": method,
                "confidence": _DATE_METHOD_CONFIDENCE.get(method, "low"),
            }
        )

    if items:
        return items

    # No repeated block structure found (common on simpler pages): fall back to
    # whole-page anchor + time-tag proximity. Pair each <time> with the nearest
    # preceding anchor -- most list templates put the date right next to the
    # title link. The pairing itself is a guess (see docstring), so this whole
    # path is capped at LOW confidence unless structured data independently
    # confirms the URL.
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
        abs_url = nearest.href
        if abs_url in structured_dates:
            published_at, method, confidence = structured_dates[abs_url], "json_ld", "high"
        elif abs_url in feed_dates:
            published_at, method, confidence = feed_dates[abs_url], "feed", "high"
        elif published_at is None:
            method, confidence = "undated", "n/a"
        else:
            method, confidence = "proximity_fallback", "low"
        items.append(
            {
                "key": abs_url,
                "url": abs_url,
                "text": text,
                "published_at": published_at,
                "identification_method": method,
                "confidence": confidence,
            }
        )
    return items
