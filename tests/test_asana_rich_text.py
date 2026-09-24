from syncall.asana.rich_text import (
    asana_html_to_markdown,
    canonical_asana_html,
    markdown_to_asana_html,
)


def test_empty_notes() -> None:
    assert markdown_to_asana_html("") == "<body></body>"
    assert asana_html_to_markdown("<body></body>") == ""


def test_markdown_to_asana_html() -> None:
    markdown = """# Review

Check **organic traffic** and *conversion*.

- UK
- US

1. Open report
2. Compare pages

> Keep this short.

Use `task next`.

~~Old note~~
"""

    html = markdown_to_asana_html(markdown)

    assert html.startswith("<body>")
    assert html.endswith("</body>")
    assert "<h1>Review</h1>" in html
    assert "<strong>organic traffic</strong>" in html
    assert "<em>conversion</em>" in html
    assert "<ul><li>UK</li><li>US</li></ul>" in html
    assert "<ol><li>Open report</li><li>Compare pages</li></ol>" in html
    assert "<blockquote>Keep this short.</blockquote>" in html
    assert "<code>task next</code>" in html
    assert "<s>Old note</s>" in html


def test_asana_html_to_markdown() -> None:
    html = (
        "<body><h2>Review</h2>Check <strong>traffic</strong> and <em>CVR</em>."
        "<ul><li>UK</li><li>US</li></ul>"
        '<a href="https://example.com">Dashboard</a>'
        "<pre>line 1\nline 2</pre></body>"
    )

    markdown = asana_html_to_markdown(html)

    assert "## Review" in markdown
    assert "Check **traffic** and *CVR*." in markdown
    assert "- UK" in markdown
    assert "- US" in markdown
    assert "[Dashboard](https://example.com)" in markdown
    assert "```\nline 1\nline 2\n```" in markdown


def test_supported_markdown_round_trip() -> None:
    markdown = """## Analysis

Check **revenue**.

- Sessions
- Orders

> Compare both markets.
"""

    assert asana_html_to_markdown(markdown_to_asana_html(markdown)) == markdown.strip()


def test_unknown_asana_tag_keeps_inner_text() -> None:
    html = "<body>Before <future-tag>new content</future-tag> after</body>"
    assert asana_html_to_markdown(html) == "Before new content after"


def test_special_characters_are_xml_safe() -> None:
    html = markdown_to_asana_html("A < B & C > D")
    assert html == "<body>A &lt; B &amp; C &gt; D</body>"
    assert asana_html_to_markdown(html) == "A < B & C > D"


def test_apostrophes_remain_literal_in_generated_asana_html() -> None:
    html = markdown_to_asana_html("I've checked Sean's notes.")

    assert html == "<body>I've checked Sean's notes.</body>"


def test_asana_image_projects_to_stable_asset_link() -> None:
    html = (
        "<body>Before"
        '<img src="https://asanausercontent.com/signed" '
        'data-asana-gid="1218736823205951" alt="image.png" />'
        "After</body>"
    )

    markdown = asana_html_to_markdown(html)

    assert (
        "[image.png](https://app.asana.com/app/asana/-/get_asset?asset_id=1218736823205951)"
    ) in markdown


def test_canonical_asana_html_ignores_signed_asset_query_tokens() -> None:
    first = (
        '<body><img src="https://asanausercontent.com/us1/assets/1/2/abc'
        '?e=1790276561&amp;v=0&amp;t=OldToken" /></body>'
    )
    second = (
        '<body><img src="https://asanausercontent.com/us1/assets/1/2/abc'
        '?e=1790276562&amp;v=0&amp;t=NewToken" /></body>'
    )
    changed = (
        '<body><img src="https://asanausercontent.com/us1/assets/1/2/abc'
        '?e=1790276562&amp;v=0&amp;t=NewToken" alt="other" /></body>'
    )

    assert canonical_asana_html(first) == canonical_asana_html(second)
    assert canonical_asana_html(first) != canonical_asana_html(changed)
