"""R-MAIL-CONTENT-TRASHFIX Part 2 — email content excerpt pipeline tests.

Contracts:
  * format=full body: text/plain preferred; HTML-only stripped to clean text.
  * Cleaning: zero-width/nbsp spam collapsed, boilerplate lines dropped
    (View in browser / Unsubscribe / social / bare URLs / legal footers).
  * Excerpt = first substantive content, ≤ EMAIL_EXCERPT_CHARS (default 800),
    cut at a sentence boundary.
  * Render: sender/subject/date + indented excerpt block; excerpt captured
    BEFORE the post-action runs; LLM payload never carries the excerpt.
"""
from __future__ import annotations

import email

from modules import gmail_intel as gi
from modules.intel_render import format_gmail_intel_block
from modules.intel_slim import slim_intel_for_llm

# Realistic Bloomberg-style HTML: invisible chars, boilerplate, long body.
_BLOOMBERG_HTML = (
    "<html><head><style>.x{color:red}</style><title>t</title></head><body>"
    "<div>\u200b\u200c\u200d\ufeff&nbsp;&nbsp;</div>"
    "<p><a href='http://x.test/vib'>View in browser</a></p>"
    "<p>Subscribe to get this newsletter delivered to your inbox.</p>"
    "<p>Markets\u00a0are\u00a0bracing for the Fed decision.\u200b The dollar "
    "slid against major peers while traders priced in two cuts.</p>"
    "<p>Oil extended gains after OPEC+ signaled restraint. "
    + "Treasury yields fell for a third session as demand for havens rose. " * 20
    + "</p>"
    "<p>https://twitter.com/business</p>"
    "<p>Facebook</p><p>Twitter</p>"
    "<p>You received this message because you are subscribed.</p>"
    "<p>Unsubscribe | Privacy Policy</p>"
    "<p>\u00a92026 Bloomberg L.P. All rights reserved.</p>"
    "</body></html>"
)


# Realistic Bloomberg text/plain: angle-bracket tracking URL tokens, in the
# consecutive runs that used to eat most of the 800-char excerpt budget
# (R-MAIL-POLISH). Includes a wrapped URL spanning two lines.
_SLI = "https://sli.bloomberg.com/click?id="
_BLOOMBERG_TEXT_URLBLOCKS = (
    f"View in browser <{_SLI}vib123&url=https%3A%2F%2Fwww.bloomberg.com%2Fnews>\n\n"
    f"<{_SLI}run1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa>\n"
    f"<{_SLI}run2bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb> <{_SLI}run3cc>\n"
    f"<{_SLI}wrapped-dddddddddddddddddddddddddddddddddd\n"
    "eeeeeeeeeeeeeeeeeeeeeeeeeeeeee&sig=ffff>\n"
    f"{_SLI}bare-url-line-gggggggggggggggggggggggggggggg\n"
    f"{_SLI}bare1 | {_SLI}bare2\n\n"
    f"Five Things - Americas <{_SLI}header55>\n\n"
    "Markets are bracing for the Fed decision. The dollar slid against major "
    "peers while traders priced in two cuts. Oil extended gains after OPEC+ "
    "signaled restraint.\n\n"
    f"Read the full story <{_SLI}story77>\n"
    f"Unsubscribe <{_SLI}unsub99>\n"
)


def _mime(body: str, ctype: str) -> email.message.Message:
    raw = (
        f"From: Bloomberg <noreply@bloomberg.com>\r\nSubject: 5 Things\r\n"
        f"Date: Wed, 12 Aug 2026 10:00:00 +0000\r\n"
        f"Content-Type: {ctype}; charset=utf-8\r\n\r\n{body}"
    ).encode()
    return email.message_from_bytes(raw)


def test_html_fixture_yields_substantive_excerpt():
    body = gi._get_body(_mime(_BLOOMBERG_HTML, "text/html"))
    excerpt = gi._make_excerpt(body, 800)
    # Substantive market content is there…
    assert "Markets are bracing for the Fed decision" in excerpt
    assert "dollar" in excerpt
    # …tracking garbage and boilerplate are gone.
    for junk in (
        "View in browser", "Subscribe", "Unsubscribe", "All rights reserved",
        "You received this message", "https://twitter.com", "Facebook",
        "Privacy Policy",
    ):
        assert junk not in excerpt
    assert "\u200b" not in excerpt and "\u00a0" not in excerpt
    # Right length: long body respects the cap, cut is clean.
    assert 400 < len(excerpt) <= 800


def test_plain_text_fixture():
    txt = (
        "To get this newsletter delivered, sign up here.\n\n"
        "BTC reclaimed $120K overnight. Funding stayed muted across venues.\n"
        "ETH lagged with unlock pressure building.\n\n"
        "Unsubscribe\nhttp://t.co/abc\n"
    )
    body = gi._get_body(_mime(txt, "text/plain"))
    excerpt = gi._make_excerpt(body, 800)
    assert excerpt.startswith("BTC reclaimed $120K overnight.")
    assert "ETH lagged" in excerpt
    assert "sign up" not in excerpt and "Unsubscribe" not in excerpt


def test_bloomberg_url_blocks_never_reach_excerpt():
    """R-MAIL-POLISH: angle-bracket tracking URLs and URL-only runs are
    stripped BEFORE measuring content — the excerpt budget goes to prose."""
    body = gi._get_body(_mime(_BLOOMBERG_TEXT_URLBLOCKS, "text/plain"))
    excerpt = gi._make_excerpt(body, 800)
    # Starts at the first real content, not a URL block.
    assert excerpt.startswith("Five Things - Americas")
    assert "Markets are bracing for the Fed decision" in excerpt
    # No URL garbage of any shape survives.
    assert "sli.bloomberg.com" not in excerpt
    assert "<http" not in excerpt and "http" not in excerpt
    assert "View in browser" not in excerpt and "Unsubscribe" not in excerpt
    # A line that mixes prose + angle URL keeps its prose ("Read the full
    # story" is substantive; only the URL token is gone).
    assert "Read the full story" in excerpt


def test_url_only_runs_dropped_wrapped_url_spans_lines():
    cleaned = gi._clean_body_text(
        "<https://sli.bloomberg.com/click?id=a\nb&sig=c>\n"
        "https://x.test/1 | https://x.test/2\n"
        "Real sentence stays here.\n"
    )
    assert cleaned == "Real sentence stays here."


def test_sentence_boundary_cut():
    text = ("First sentence here. " * 10 + "Second block continues. " * 30).strip()
    cut = gi._cut_at_sentence(text, 200)
    assert len(cut) <= 200
    assert cut.endswith(".")            # ends exactly at a sentence boundary
    # No mid-word tail: the char right after the cut in the original is space.
    assert text[len(cut)] == " "


def test_excerpt_env_length_respected(monkeypatch):
    monkeypatch.setattr(gi, "EMAIL_EXCERPT_CHARS", 120)
    body = gi._get_body(_mime(_BLOOMBERG_HTML, "text/html"))
    excerpt = gi._make_excerpt(body, gi.EMAIL_EXCERPT_CHARS)
    assert len(excerpt) <= 120


def test_render_shows_indented_excerpt_block():
    gmail = {
        "status": "ok",
        "emails": [{
            "from": "Bloomberg <noreply@bloomberg.com>",
            "subject": "5 Things to Start Your Day",
            "date": "Wed, 12 Aug 2026 10:00:00 +0000",
            "excerpt": "Markets are bracing for the Fed decision. "
                       "The dollar slid against major peers.",
            "snippet": "ignored when excerpt present",
        }],
        "count": 1,
        "post_action": "trash",
        "post_action_dry_run": False,
        "post_action_count": 1,
    }
    out = format_gmail_intel_block(gmail)
    assert "Bloomberg" in out and "5 Things" in out
    # Excerpt rendered as indented block lines.
    assert "\n    Markets are bracing for the Fed decision." in out
    assert "ignored when excerpt present" not in out
    assert "1 verificados en papelera" in out


def test_llm_payload_strips_excerpt():
    intel = {
        "gmail": {
            "status": "ok",
            "emails": [{"subject": "s", "excerpt": "X" * 800, "snippet": "short"}],
        },
        "other": 1,
    }
    slim = slim_intel_for_llm(intel)
    assert "excerpt" not in slim["gmail"]["emails"][0]
    assert slim["gmail"]["emails"][0]["snippet"] == "short"
    # Original never mutated.
    assert intel["gmail"]["emails"][0]["excerpt"] == "X" * 800
