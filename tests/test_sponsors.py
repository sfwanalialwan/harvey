"""SPONSORS — finding the brands that already pay newsletters like yours.

The tests care about the two ways this goes wrong: missing a sponsor that is
plainly labelled, and recording an editorial link (a study, a tool the writer
likes, the newsletter's own pages) as if someone had paid for it.
"""

import asyncio
import os
import tempfile

import httpx
import pytest
import pytest_asyncio

from harvey.collectors import discover as D
from harvey.collectors import sponsors as S
from harvey.config import load_config
from harvey.signals import SIGNAL_CATALOG, seed_signal_catalog
from harvey.state import StateManager

# A beehiiv-style issue: a "Presented by" header over the ad, editorial links
# that beehiiv tags with utm_medium=newsletter, a link back to the newsletter.
BEEHIIV_ISSUE = """
<div id="content-blocks">
  <h2>Good morning</h2>
  <p>Here is what we found this week. Read the
     <a href="https://pubmed.ncbi.nlm.nih.gov/123/?utm_source=gutweekly&utm_medium=newsletter">new study</a>
     and our <a href="https://gutweekly.beehiiv.com/p/archive">last issue</a>.</p>
  <p>I have been using <a href="https://www.cronometer.com/?utm_source=gutweekly&utm_medium=newsletter">Cronometer</a> for this.</p>
  <h3>PRESENTED BY SEED</h3>
  <p><img src="https://cdn.example/seed.png"></p>
  <p>Most probiotics die before they reach the gut.
     <a href="https://seed.com/ds01?utm_source=gutweekly&utm_medium=newsletter">Seed's DS-01</a>
     is built to survive it.</p>
  <p><a href="https://seed.com/ds01?code=GUT25">Try it for 25% off</a></p>
  <h2>The main story</h2>
  <p><a href="https://www.olipop.com/">Olipop</a> is everywhere, so we looked at prebiotic soda.</p>
</div>
"""

# A Substack-style issue: the sponsor is named in a sentence near the top.
SUBSTACK_ISSUE = """
<p>Today's issue is brought to you by <a href="https://www.factor75.com/plans?c=GUT">Factor</a>.</p>
<p>Ready-made meals, no prep.</p>
<h2>Why bloating gets worse after 45</h2>
<p>A trial sponsored by the dairy council found lactose intolerance is
   underreported, which matters because <a href="https://www.zoe.com/learn">ZOE</a>
   has written about it at length and it is a long paragraph that goes on.</p>
<p>Want to reach our readers? <a href="https://gutweekly.beehiiv.com/advertise">Sponsor us</a>.</p>
"""

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Gut Weekly</title>
<item><title>Issue 3</title><link>https://gutweekly.beehiiv.com/p/3</link>
  <pubDate>Mon, 12 Oct 2026 10:00:00 GMT</pubDate>
  <content:encoded><![CDATA[{one}]]></content:encoded></item>
<item><title>Issue 2</title><link>https://gutweekly.beehiiv.com/p/2</link>
  <content:encoded><![CDATA[{two}]]></content:encoded></item>
<item><title>Issue 1</title><link>https://gutweekly.beehiiv.com/p/1</link>
  <description><![CDATA[{three}]]></description></item>
</channel></rss>"""


def _rss(*issues):
    one, two, three = (list(issues) + ["", "", ""])[:3]
    return RSS.replace("{one}", one).replace("{two}", two).replace("{three}", three)


def _domains(hits):
    return {h.domain for h in hits}


# ── Detection ────────────────────────────────────────────────────────


def test_a_labelled_sponsor_section_is_found():
    hits = S.extract_sponsors(BEEHIIV_ISSUE, "gutweekly.beehiiv.com")
    assert _domains(hits) == {"seed.com"}
    seed = hits[0]
    assert seed.confidence == 1.0
    assert seed.marker == "presented by"
    assert seed.name == "Seed's DS-01"


def test_editorial_links_are_not_sponsors():
    """beehiiv tags every link utm_medium=newsletter; that alone means nothing."""
    hits = S.extract_sponsors(BEEHIIV_ISSUE, "gutweekly.beehiiv.com")
    assert "cronometer.com" not in _domains(hits)
    assert "olipop.com" not in _domains(hits), "the section ends at the next heading"


def test_research_and_platform_links_are_never_sponsors():
    html = """<p>Sponsored by</p>
              <p><a href="https://pubmed.ncbi.nlm.nih.gov/1">study</a>
                 <a href="https://www.beehiiv.com/?via=x">beehiiv</a>
                 <a href="https://docs.google.com/forms/x">survey</a></p>"""
    assert S.extract_sponsors(html, "") == []


def test_a_sponsor_named_in_a_sentence_is_found():
    hits = S.extract_sponsors(SUBSTACK_ISSUE, "gutweekly.beehiiv.com")
    assert "factor75.com" in _domains(hits)
    factor = next(h for h in hits if h.domain == "factor75.com")
    assert factor.name == "Factor"


def test_sponsored_in_the_middle_of_editorial_prose_is_ignored():
    """'A trial sponsored by the dairy council' is not an ad for ZOE."""
    hits = S.extract_sponsors(SUBSTACK_ISSUE, "gutweekly.beehiiv.com")
    assert "zoe.com" not in _domains(hits)


def test_the_newsletters_own_advertise_page_is_not_a_sponsor():
    hits = S.extract_sponsors(SUBSTACK_ISSUE, "gutweekly.beehiiv.com")
    assert not any(h.domain.endswith("beehiiv.com") for h in hits)


def test_paid_utm_tags_count_at_lower_confidence():
    html = """<p>Our favourite tea this month is
              <a href="https://pukkaherbs.com/?utm_source=gutweekly&utm_medium=sponsorship">Pukka</a>.</p>"""
    hits = S.extract_sponsors(html, "gutweekly.beehiiv.com")
    assert len(hits) == 1
    assert hits[0].domain == "pukkaherbs.com"
    assert hits[0].marker == "utm" and hits[0].confidence == 0.6


def test_generic_anchor_text_falls_back_to_the_brand_domain():
    html = """<h3>Together with</h3>
              <p><a href="https://www.athleticgreens.com/gut">Click here</a></p>"""
    hits = S.extract_sponsors(html, "")
    assert hits[0].name == "Athleticgreens"


# ── Feeds ────────────────────────────────────────────────────────────


def test_feed_candidates_try_the_common_paths():
    assert S.feed_candidates("gutweekly.beehiiv.com") == [
        "https://gutweekly.beehiiv.com/feed",
        "https://gutweekly.beehiiv.com/rss",
        "https://gutweekly.beehiiv.com/feed.xml",
    ]
    assert S.feed_candidates("https://rss.beehiiv.com/feeds/abc.xml") == [
        "https://rss.beehiiv.com/feeds/abc.xml"]


def test_rss_content_and_description_both_parse():
    posts = S.parse_feed(_rss("<p>one</p>", "<p>two</p>", "<p>three</p>"))
    assert [p.title for p in posts] == ["Issue 3", "Issue 2", "Issue 1"]
    assert posts[0].url == "https://gutweekly.beehiiv.com/p/3"
    assert "one" in posts[0].html and "three" in posts[2].html


def test_atom_feeds_parse():
    atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>Hello</title><link rel="alternate" href="https://x.com/p/1"/>
      <content type="html">&lt;p&gt;hi&lt;/p&gt;</content></entry></feed>"""
    posts = S.parse_feed(atom)
    assert posts[0].title == "Hello" and posts[0].url == "https://x.com/p/1"
    assert "<p>hi</p>" in posts[0].html


def test_an_html_page_is_not_a_feed():
    assert S.parse_feed("<!doctype html><html><body>nope</body></html>") == []


def test_feed_link_autodiscovery():
    html = '<head><link rel="alternate" type="application/rss+xml" href="/rss.xml"></head>'
    assert S.feed_link_from_html(html, "https://news.example.com/") == \
        "https://news.example.com/rss.xml"


def test_summary_counts_placements_across_issues():
    posts = S.parse_feed(_rss(BEEHIIV_ISSUE, BEEHIIV_ISSUE, SUBSTACK_ISSUE))
    rows = S.summarize(posts, "gutweekly.beehiiv.com")
    assert [r.domain for r in rows] == ["seed.com", "factor75.com"]
    assert rows[0].placements == 2
    assert rows[0].latest.title == "Issue 3"
    assert rows[0].newsletter == "gutweekly.beehiiv.com"


def test_shortened_sponsor_links_use_where_they_landed():
    html = """<h3>Presented by</h3><p><a href="https://bit.ly/abc">Kettle &amp; Fire</a></p>"""
    posts = [S.FeedPost(title="t", url="u", html=html)]
    assert S.shortener_links(posts) == ["https://bit.ly/abc"]
    assert S.summarize(posts, "") == [], "an unresolved shortener is not a brand"
    rows = S.summarize(posts, "", {"https://bit.ly/abc": "https://www.kettleandfire.com/broth"})
    assert rows[0].domain == "kettleandfire.com"
    assert rows[0].name == "Kettle & Fire"


# ── The provider, end to end ─────────────────────────────────────────


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "test.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        for sig in SIGNAL_CATALOG:
            await sm.set_signal_status(sig["code"], "confirmed")
        yield sm


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    async def no_sleep(_seconds):
        return None
    monkeypatch.setattr(D.asyncio, "sleep", no_sleep)


def _serve(monkeypatch, routes: dict[str, httpx.Response]):
    """Point every AsyncClient the discovery run opens at canned responses."""
    real = httpx.AsyncClient

    def handler(request):
        return routes.get(str(request.url), httpx.Response(404, text="not found"))

    def client(**kwargs):
        kwargs.pop("transport", None)
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(D.httpx, "AsyncClient", client)


def test_sponsor_queries_come_from_seed_newsletters():
    config = load_config()
    config.icp.seed_newsletters = ["gutweekly.beehiiv.com", " "]
    qs = D.build_queries(config, provider_key="sponsors")
    assert [q.term for q in qs] == ["gutweekly.beehiiv.com"]
    qs = D.build_queries(config, provider_key="sponsors", newsletters=["a.com"])
    assert [q.term for q in qs] == ["a.com"]


@pytest.mark.asyncio
async def test_no_newsletters_is_reported_not_silently_empty(state):
    config = load_config()
    config.icp.seed_newsletters = []
    report = await D.run_discovery(state, config, "sponsors")
    assert report.stopped == "no newsletters"
    assert "seed_newsletters" in report.errors[0]


@pytest.mark.asyncio
async def test_run_records_sponsors_with_evidence(state, monkeypatch):
    _serve(monkeypatch, {
        "https://gutweekly.beehiiv.com/feed": httpx.Response(
            200, text=_rss(BEEHIIV_ISSUE, SUBSTACK_ISSUE, BEEHIIV_ISSUE)),
        "https://bloatfree.substack.com/feed": httpx.Response(
            200, text=_rss(BEEHIIV_ISSUE)),
    })
    config = load_config()
    report = await D.run_discovery(state, config, "sponsors", [
        D.DiscoveryQuery(term="gutweekly.beehiiv.com"),
        D.DiscoveryQuery(term="https://bloatfree.substack.com"),
    ])
    assert report.errors == []
    assert report.new_companies == 2          # seed.com, factor75.com
    assert report.known_companies == 1        # seed.com again, second newsletter

    seed = await state.get_company_by_domain("seed.com")
    assert seed.industry == "newsletter sponsor"
    details = {s["detail"] for s in seed.signals}
    assert details == {
        "Has paid to sponsor the gutweekly.beehiiv.com newsletter",
        "Has paid to sponsor the bloatfree.substack.com newsletter",
    }

    obs = await state.get_observations(company_id=seed.id)
    by_code = {}
    for o in obs:
        by_code.setdefault(o["signal_code"], []).append(o)
    assert {o["value_text"] for o in by_code["SPONSORS_NEWSLETTER"]} == {
        "gutweekly.beehiiv.com", "bloatfree.substack.com"}
    assert max(o["value_num"] for o in by_code["SPONSOR_PLACEMENTS"]) == 2
    assert "FOUND_IN_SERP" not in by_code


@pytest.mark.asyncio
async def test_a_rerun_does_not_stack_duplicate_signals(state, monkeypatch):
    _serve(monkeypatch, {
        "https://gutweekly.beehiiv.com/feed": httpx.Response(200, text=_rss(BEEHIIV_ISSUE)),
    })
    config = load_config()
    q = [D.DiscoveryQuery(term="gutweekly.beehiiv.com")]
    await D.run_discovery(state, config, "sponsors", q)
    await D.run_discovery(state, config, "sponsors", q)
    seed = await state.get_company_by_domain("seed.com")
    assert len(seed.signals) == 1


@pytest.mark.asyncio
async def test_a_newsletter_without_a_feed_is_an_error_not_zero_sponsors(state, monkeypatch):
    _serve(monkeypatch, {})
    report = await D.run_discovery(state, load_config(), "sponsors",
                                   [D.DiscoveryQuery(term="nofeed.example.com")])
    assert report.new_companies == 0
    assert report.errors and "no RSS feed or issue archive found" in report.errors[0]


@pytest.mark.asyncio
async def test_the_feed_advertised_on_the_homepage_is_used(state, monkeypatch):
    _serve(monkeypatch, {
        "https://news.example.com": httpx.Response(
            200, text='<link rel="alternate" type="application/rss+xml" href="/issues.xml">'),
        "https://news.example.com/issues.xml": httpx.Response(200, text=_rss(SUBSTACK_ISSUE)),
    })
    report = await D.run_discovery(state, load_config(), "sponsors",
                                   [D.DiscoveryQuery(term="news.example.com")])
    assert report.errors == []
    assert await state.get_company_by_domain("factor75.com") is not None


def test_reporting_on_a_sponsored_study_is_not_an_ad():
    """Even standing alone, with nothing earlier in the issue to swallow it."""
    for html in (
        '<p>A trial sponsored by <a href="https://www.zoe.com/learn">ZOE</a> found this.</p>',
        "<p>We read the research so you don't have to. A small study sponsored by "
        "the dairy council found lactose intolerance is underreported, and "
        '<a href="https://www.zoe.com/learn">ZOE</a> has written about it at length.</p>',
    ):
        assert S.extract_sponsors(html, "") == []


def test_a_sponsor_in_a_long_intro_paragraph_is_found():
    html = ("<p>Today's issue is brought to you by "
            '<a href="https://www.hungryroot.com/?c=GUT">Hungryroot</a>, the grocery '
            "service that plans your week around what your gut tolerates, so you "
            "spend less time reading labels and more time eating. Now, on to the "
            'news, starting with <a href="https://www.olipop.com/">Olipop</a>.</p>')
    hits = S.extract_sponsors(html, "")
    assert _domains(hits) == {"hungryroot.com"}


def test_a_list_of_sponsors_under_one_label_are_all_found():
    """The shape Lenny's Newsletter and many Substacks use."""
    html = """<p><strong>Brought to you by:</strong></p>
      <ul>
        <li><p><a href="https://workos.com/?utm_source=lenny">WorkOS</a>: Make your app enterprise-ready</p></li>
        <li><p><a href="https://getdx.com/lenny">DX</a>: Engineering intelligence</p></li>
      </ul>
      <p>For inquiries about sponsoring the podcast, email
         <a href="mailto:podcast@example.com">podcast@example.com</a>.</p>"""
    hits = S.extract_sponsors(html, "lennysnewsletter.com")
    assert _domains(hits) == {"workos.com", "getdx.com"}
    assert {h.name for h in hits} == {"WorkOS", "DX"}


def test_a_monthly_community_sponsor_is_found():
    html = ('<p>A big thank-you to this month\'s community sponsor, '
            '<a href="https://posthog.com/">PostHog</a>.</p>')
    assert _domains(S.extract_sponsors(html, "")) == {"posthog.com"}


@pytest.mark.asyncio
async def test_without_a_feed_the_issue_archive_is_read(state, monkeypatch):
    """Plenty of beehiiv sites have no public RSS, but all of them list issues."""
    home = """<html><body>
      <a href="/p/issue-2">Issue 2</a><a href="https://gutweekly.com/p/issue-1/">Issue 1</a>
      <a href="https://elsewhere.com/p/not-ours">x</a><a href="/about">About</a>
    </body></html>"""
    page = """<html><head><meta property="og:title" content="Issue {n}"></head><body>
      <header><p>Powered by <a href="https://www.beehiiv.com">beehiiv</a></p></header>
      <div id="content-blocks">{body}</div></body></html>"""
    _serve(monkeypatch, {
        "https://gutweekly.com": httpx.Response(200, text=home),
        "https://gutweekly.com/p/issue-2": httpx.Response(
            200, text=page.replace("{n}", "2").replace("{body}", BEEHIIV_ISSUE)),
        "https://gutweekly.com/p/issue-1": httpx.Response(
            200, text=page.replace("{n}", "1").replace("{body}", SUBSTACK_ISSUE)),
    })
    report = await D.run_discovery(state, load_config(), "sponsors",
                                   [D.DiscoveryQuery(term="gutweekly.com")])
    assert report.errors == []
    assert report.new_companies == 2
    seed = await state.get_company_by_domain("seed.com")
    assert seed.signals[0]["evidence_title"] == "Issue 2"
    assert seed.signals[0]["evidence_url"] == "https://gutweekly.com/p/issue-2"


def test_post_links_stay_on_the_newsletters_own_site():
    html = '<a href="/p/a">a</a><a href="/p/a/">dup</a><a href="https://x.com/p/b">b</a><a href="/archive">c</a>'
    assert S.post_links_from_html(html, "https://news.example.com/") == [
        "https://news.example.com/p/a"]


def test_the_example_config_loads_and_opts_into_the_skill():
    from pathlib import Path
    from harvey.brain import SKILLS_DIR
    from harvey.config import load_config as load

    path = Path(__file__).resolve().parent.parent / "examples" / "newsletter-sponsorship.yaml"
    config = load(str(path))
    assert config.icp.seed_newsletters
    assert config.skills == ["newsletter_sponsorship"]
    assert (SKILLS_DIR / "newsletter_sponsorship.md").exists()
    assert D.build_queries(config, provider_key="sponsors")


def test_extra_skills_are_added_only_where_an_agent_has_skills(tmp_path):
    from harvey.brain import Brain

    brain = Brain(StateManager(str(tmp_path / "t.db")))
    writer = brain.load_skills_for_agent("writer", extra=["newsletter_sponsorship"])
    assert "Selling Newsletter Sponsorships" in writer
    assert "Selling Newsletter Sponsorships" not in brain.load_skills_for_agent("writer")
    assert brain.load_skills_for_agent("nobody", extra=["newsletter_sponsorship"]) == ""
