"""SPONSORS — which brands already pay to be in newsletters like yours.

For a newsletter selling sponsorships, the best prospect is not a company
that *might* advertise. It is a company that advertised last week, in a
newsletter that reaches the same readers. That fact is public: it sits in the
newsletter's own archive, under "presented by" or "together with".

So this collector reads the RSS feeds of newsletters you name (your peers,
the ones your readers also subscribe to), finds the sponsor sections in each
issue, and records every brand it finds there with the post as evidence.

No API, no account, no model call. Two HTTP requests per newsletter.

Two ways a sponsor gets detected, with different confidence:

* **Marker blocks** (confidence 1.0): a block that says "sponsored by",
  "presented by", "together with", "today's sponsor" and so on. Every brand
  linked in that block and the few blocks after it, up to the next heading,
  is a sponsor ("Brought to you by:" is often a list of several). In a long
  paragraph, only a link right after the marker counts.
* **Paid UTM tags** (confidence 0.6): a link anywhere in the issue tagged
  ``utm_medium=sponsorship`` / ``paid`` / ``sponsored``, or with "sponsor" in
  ``utm_campaign``. Plain ``utm_medium=newsletter`` is NOT counted: beehiiv
  and others add that to every link, editorial ones included.
"""

import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

# Words that open a sponsor section. Matched case-insensitively on a block's
# text, so "PRESENTED BY" in a beehiiv header and "Together with Factor" in a
# Substack intro both count.
SPONSOR_MARKER = re.compile(
    r"\b("
    r"sponsored\s+by|presented\s+by|brought\s+to\s+you\s+by|powered\s+by|"
    r"together\s+with|in\s+partnership\s+with|"
    r"(?:today'?s|this\s+(?:week|month|issue)'?s|our)\s+(?:\w+\s+)?(?:sponsor|partner)s?|"
    r"a\s+(?:word|message|note)\s+from\s+(?:our|today'?s|this\s+week'?s)\s+"
    r"(?:sponsor|partner)s?|"
    r"partner\s+spotlight|sponsor\s+spotlight|"
    r"sponsored|advertisement"
    r")\b",
    re.I,
)

# UTM values that only appear on paid placements.
PAID_UTM_MEDIUM = {
    "sponsorship", "sponsored", "sponsor", "paid", "paid_newsletter",
    "newsletter_sponsorship", "paid-newsletter", "ad", "ads", "cpc", "cpm",
    "paidsocial", "newsletter-sponsorship", "newsletter_ad", "newsletter-ad",
}

BLOCK_TAGS = ("p", "div", "td", "li", "h1", "h2", "h3", "h4", "h5", "h6",
              "blockquote", "section", "table", "figure", "figcaption")
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

# How many blocks, the label included, belong to the sponsor section. Room
# for a "Brought to you by:" label over a list of several sponsors.
WINDOW = 6
# A block this short is a section label; anything longer is prose.
LABEL_MAX = 120
# In prose, the sponsor link has to follow the marker this closely.
NEAR = 60
# "A trial sponsored by the dairy council" is reporting, not advertising.
REPORTING = re.compile(
    r"\b(study|studies|trial|research|paper|survey|report|review|conference|"
    r"event|program|programme|grant|team|race|athlete)\s*$", re.I)

# Hosts that show up in sponsor sections but are never the sponsor: the
# newsletter platforms themselves, forms, podcasts, research references.
# Matched on a dot boundary like discover.JUNK_DOMAIN_PATTERNS.
NOT_A_SPONSOR = tuple(re.compile(p, re.I) for p in (
    r"(^|\.)(beehiiv|substack|ghost|convertkit|kit|mailchimp|list-manage)\.",
    r"(^|\.)(sparkloop|swapstack|paved|passionfroot|letterhead)\.",
    r"(^|\.)(typeform|tally|forms\.gle|docs\.google|calendly)\b",
    r"(^|\.)(spotify|podcasts\.apple|anchor|buzzsprout|transistor)\.",
    r"(^|\.)(doi|pubmed|ncbi|nature|sciencedirect|jamanetwork|bmj)\.",
    r"(^|\.)(gmail|outlook|mailto)\b",
))

# Link shorteners hide the brand. The provider resolves these over the network
# when it can; offline they are dropped rather than recorded as "bit.ly".
SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "ow.ly", "buff.ly", "rebrand.ly",
              "short.io", "lnkd.in", "cutt.ly", "geni.us", "amzn.to"}

GENERIC_ANCHORS = re.compile(
    r"^(learn more|click here|read more|try it|try (it )?(for )?free|get started|"
    r"shop now|sign up|check (it|them) out|here|this link|link|see more|"
    r"claim (your )?offer|get (the )?deal|order now|start (now|here)|"
    r"\W+|\d+% off.*)$",
    re.I,
)


@dataclass
class FeedPost:
    title: str = ""
    url: str = ""
    published: str = ""
    html: str = ""


@dataclass
class SponsorHit:
    """One brand seen in one sponsor section of one issue."""

    domain: str
    url: str
    name: str = ""
    marker: str = ""          # the words that marked the section, or "utm"
    confidence: float = 1.0


@dataclass
class SponsorSummary:
    """Everything one newsletter's feed says about one brand."""

    domain: str
    name: str = ""
    newsletter: str = ""      # the newsletter's domain
    placements: int = 0       # how many issues carried it
    confidence: float = 0.0   # best confidence across those issues
    posts: list[FeedPost] = field(default_factory=list)
    url: str = ""             # a link the sponsor actually used

    @property
    def latest(self) -> FeedPost | None:
        return self.posts[0] if self.posts else None


# ── Feeds ────────────────────────────────────────────────────────────


def feed_candidates(newsletter_url: str) -> list[str]:
    """The URLs worth trying for a newsletter's RSS feed, best first.

    beehiiv, Substack, Ghost and WordPress all serve ``/feed``; a few serve
    ``/rss`` or ``/feed.xml``. A URL that already looks like a feed is used
    as-is.
    """
    raw = newsletter_url.strip()
    if not raw:
        return []
    if "://" not in raw:
        raw = "https://" + raw
    path = urlparse(raw).path.lower()
    if path.endswith((".xml", ".rss", "/feed", "/rss", "/atom")) or "rss" in path:
        return [raw]
    base = raw.rstrip("/")
    return [f"{base}/feed", f"{base}/rss", f"{base}/feed.xml"]


def feed_link_from_html(html: str, page_url: str) -> str:
    """The feed a homepage advertises in <link rel="alternate">, if any."""
    soup = BeautifulSoup(html or "", "html.parser")
    for link in soup.find_all("link", rel=True):
        rel = " ".join(link.get("rel") or []).lower()
        kind = (link.get("type") or "").lower()
        if "alternate" in rel and ("rss" in kind or "atom" in kind) and link.get("href"):
            return urljoin(page_url, link["href"])
    return ""


# Post URLs on the platforms most newsletters run on: beehiiv and Substack use
# /p/<slug>; Ghost and WordPress put the slug at the root, which is too loose
# to guess at, so those rely on their (always-on) RSS feed instead.
POST_PATH = re.compile(r"^/p/[\w\-%]+/?$")


def post_links_from_html(html: str, page_url: str, limit: int = 10) -> list[str]:
    """Issue URLs linked from a newsletter's homepage or archive, newest first.

    The fallback for newsletters without a public feed: many beehiiv sites
    only expose RSS on paid plans, but every one of them lists its issues.
    """
    base = urlparse(page_url)
    soup = BeautifulSoup(html or "", "html.parser")
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        url = urljoin(page_url, a["href"])
        parsed = urlparse(url)
        if parsed.netloc.lower() != base.netloc.lower():
            continue
        if not POST_PATH.match(parsed.path):
            continue
        clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        if clean not in out:
            out.append(clean)
        if len(out) >= limit:
            break
    return out


def post_from_html(html: str, url: str) -> FeedPost:
    """One issue's web page as a FeedPost: its title and its body only.

    The site's header and footer ("Powered by beehiiv", the subscribe form)
    are not part of the issue, so take the article body when there is one.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    body = (soup.select_one("#content-blocks") or soup.select_one(".available-content")
            or soup.select_one("article") or soup.body or soup)
    title = ""
    if soup.find("meta", property="og:title"):
        title = soup.find("meta", property="og:title").get("content", "")
    elif soup.title:
        title = soup.title.get_text(strip=True)
    return FeedPost(title=title.strip(), url=url, html=str(body))


_NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "atom": "http://www.w3.org/2005/Atom",
}


def parse_feed(xml_text: str) -> list[FeedPost]:
    """RSS 2.0 or Atom → posts, newest first as the feed lists them.

    Returns [] for anything that isn't a feed (an HTML page, an error body)
    instead of raising: a bad feed is an observation about that newsletter,
    not a reason to stop the run.
    """
    try:
        root = ET.fromstring((xml_text or "").strip().encode("utf-8"))
    except ET.ParseError:
        return []

    posts: list[FeedPost] = []
    for item in root.iter("item"):
        html = (item.findtext("content:encoded", default="", namespaces=_NS)
                or item.findtext("description", default=""))
        posts.append(FeedPost(
            title=(item.findtext("title") or "").strip(),
            url=(item.findtext("link") or "").strip(),
            published=(item.findtext("pubDate") or "").strip(),
            html=html,
        ))
    if posts:
        return posts

    for entry in root.iter(f"{{{_NS['atom']}}}entry"):
        link = ""
        for ln in entry.findall("atom:link", _NS):
            if ln.get("rel", "alternate") == "alternate" and ln.get("href"):
                link = ln.get("href")
                break
        html = (entry.findtext("atom:content", default="", namespaces=_NS)
                or entry.findtext("atom:summary", default="", namespaces=_NS))
        posts.append(FeedPost(
            title=(entry.findtext("atom:title", default="", namespaces=_NS) or "").strip(),
            url=link,
            published=(entry.findtext("atom:updated", default="", namespaces=_NS) or "").strip(),
            html=html,
        ))
    return posts


# ── Sponsor detection ────────────────────────────────────────────────


def _same_site(domain: str, newsletter_domain: str) -> bool:
    if not newsletter_domain:
        return False
    return domain == newsletter_domain or domain.endswith("." + newsletter_domain)


def candidate_domain(href: str, newsletter_domain: str = "") -> str:
    """The brand domain behind a link, or "" when the link can't be a sponsor."""
    from harvey.collectors.discover import is_junk, normalize_domain

    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return ""
    domain = normalize_domain(href)
    if not domain or _same_site(domain, newsletter_domain):
        return ""
    if any(p.search(domain) for p in NOT_A_SPONSOR):
        return ""
    if is_junk(domain):
        return ""
    return domain


def _brand_name(anchor_text: str, domain: str) -> str:
    text = re.sub(r"\s+", " ", anchor_text or "").strip(" .:→»›-")
    if 2 <= len(text) <= 40 and not GENERIC_ANCHORS.match(text):
        return text
    stem = domain.split(".")[0] if domain.count(".") <= 1 else domain.split(".")[-2]
    return stem.replace("-", " ").title()


def _leaf_blocks(soup: BeautifulSoup) -> list:
    """Block elements that hold content directly, in document order.

    Newsletter HTML nests tables in divs in tables. Taking only the innermost
    blocks gives a flat reading order that a "next few blocks" window can walk.
    """
    out = []
    for el in soup.find_all(BLOCK_TAGS):
        if el.find(BLOCK_TAGS) is not None:
            continue
        if el.get_text(" ", strip=True) or el.find("a"):
            out.append(el)
    return out


def _sponsor_marker(text: str):
    """The first sponsor marker in a block that isn't someone reporting on one."""
    for m in SPONSOR_MARKER.finditer(text):
        if REPORTING.search(text[max(0, m.start() - 30):m.start()]):
            continue
        return m
    return None


def _is_paid_utm(href: str) -> bool:
    q = parse_qs(urlparse(href).query)
    medium = {v.lower() for v in q.get("utm_medium", [])}
    campaign = " ".join(q.get("utm_campaign", [])).lower()
    content = " ".join(q.get("utm_content", [])).lower()
    return bool(medium & PAID_UTM_MEDIUM) or "sponsor" in campaign or "sponsor" in content


def extract_sponsors(html: str, newsletter_domain: str = "") -> list[SponsorHit]:
    """Every sponsor in one issue, one hit per brand, strongest evidence kept."""
    soup = BeautifulSoup(html or "", "html.parser")
    blocks = _leaf_blocks(soup)
    hits: dict[str, SponsorHit] = {}

    # 1. Marker blocks: the marker, then the section it opens.
    i = 0
    while i < len(blocks):
        block = blocks[i]
        text = block.get_text(" ", strip=True)
        m = _sponsor_marker(text)
        if not m:
            i += 1
            continue

        counts: Counter = Counter()
        first: dict[str, tuple[str, str]] = {}

        def take(a):
            domain = candidate_domain(a["href"], newsletter_domain)
            if domain:
                counts[domain] += 1
                first.setdefault(domain, (a["href"], a.get_text(" ", strip=True)))

        j = i + 1
        if len(text) <= LABEL_MAX:
            # A label ("PRESENTED BY", "Together with Factor"): the section is
            # this block and the next few, up to the next heading.
            for a in block.find_all("a", href=True):
                take(a)
            while j < min(len(blocks), i + WINDOW):
                if blocks[j].name in HEADING_TAGS:
                    break
                for a in blocks[j].find_all("a", href=True):
                    take(a)
                j += 1
        else:
            # A sentence inside a longer paragraph ("Today's issue is brought
            # to you by Factor, ..."): only a link right after the marker.
            for a in block.find_all("a", href=True):
                pos = text.find(a.get_text(" ", strip=True), m.end())
                if 0 <= pos - m.end() <= NEAR:
                    take(a)

        # Every brand in the section counts: "Brought to you by:" is often a
        # list of two or three sponsors, one line each.
        for domain, _n in counts.most_common():
            href, anchor = first[domain]
            hits.setdefault(domain, SponsorHit(
                domain=domain, url=href, name=_brand_name(anchor, domain),
                marker=re.sub(r"\s+", " ", m.group(0).lower()), confidence=1.0))
        i = j

    # 2. Paid UTM tags anywhere in the issue.
    for a in soup.find_all("a", href=True):
        if not _is_paid_utm(a["href"]):
            continue
        domain = candidate_domain(a["href"], newsletter_domain)
        if domain and domain not in hits:
            hits[domain] = SponsorHit(
                domain=domain, url=a["href"],
                name=_brand_name(a.get_text(" ", strip=True), domain),
                marker="utm", confidence=0.6)

    return list(hits.values())


def summarize(posts: list[FeedPost], newsletter_domain: str,
              resolve: dict[str, str] | None = None) -> list[SponsorSummary]:
    """Fold per-issue hits into one row per brand for this newsletter.

    ``resolve`` maps shortener URLs to where they landed; hits on a shortener
    that wasn't resolved are dropped.
    """
    from harvey.collectors.discover import normalize_domain

    resolve = resolve or {}
    out: dict[str, SponsorSummary] = {}
    for post in posts:
        for hit in extract_sponsors(post.html, newsletter_domain):
            if hit.domain in SHORTENERS:
                landed = resolve.get(hit.url, "")
                domain = candidate_domain(landed, newsletter_domain) if landed else ""
                if not domain or domain in SHORTENERS:
                    continue
                hit.domain, hit.url = domain, landed
                if hit.name.lower() in {s.split(".")[0] for s in SHORTENERS}:
                    hit.name = _brand_name("", domain)
            row = out.get(hit.domain)
            if row is None:
                row = out[hit.domain] = SponsorSummary(
                    domain=hit.domain, name=hit.name,
                    newsletter=normalize_domain(newsletter_domain), url=hit.url)
            row.placements += 1
            row.confidence = max(row.confidence, hit.confidence)
            row.posts.append(post)
    return sorted(out.values(), key=lambda s: (-s.placements, -s.confidence, s.domain))


def shortener_links(posts: list[FeedPost], newsletter_domain: str = "") -> list[str]:
    """Shortened sponsor links worth resolving before ``summarize``."""
    urls = []
    for post in posts:
        for hit in extract_sponsors(post.html, newsletter_domain):
            if hit.domain in SHORTENERS and hit.url not in urls:
                urls.append(hit.url)
    return urls
