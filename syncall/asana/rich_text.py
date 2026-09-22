"""Convert between Taskwarrior Markdown notes and Asana rich text."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_STRIKE_RE = re.compile(r"~~(.+?)~~")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)|(?<!_)_([^_\n]+)_(?!_)")
_UL_RE = re.compile(r"^\s*[-+*]\s+(.+)$")
_OL_RE = re.compile(r"^\s*\d+[.)]\s+(.+)$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_HR_RE = re.compile(r"^\s*(?:---+|\*\*\*+)\s*$")


def _inline_markdown_to_html(text: str) -> str:
    # Text nodes do not need quotes escaped. Keeping apostrophes/quotes literal matches
    # Asana's own html_notes output and avoids entity strings being re-escaped on write.
    escaped = html.escape(text, quote=False)
    code_spans: list[str] = []

    def stash_code(match: re.Match[str]) -> str:
        code_spans.append(match.group(1))
        return f"\x00CODE{len(code_spans) - 1}\x00"

    escaped = _CODE_RE.sub(stash_code, escaped)
    escaped = _LINK_RE.sub(
        lambda match: (
            f'<a href="{match.group(2).replace(chr(34), "&quot;")}">'
            f"{match.group(1)}</a>"
        ),
        escaped,
    )
    escaped = _BOLD_RE.sub(
        lambda match: f"<strong>{match.group(1) or match.group(2)}</strong>",
        escaped,
    )
    escaped = _STRIKE_RE.sub(lambda match: f"<s>{match.group(1)}</s>", escaped)
    escaped = _ITALIC_RE.sub(
        lambda match: f"<em>{match.group(1) or match.group(2)}</em>",
        escaped,
    )

    for index, code in enumerate(code_spans):
        escaped = escaped.replace(f"\x00CODE{index}\x00", f"<code>{code}</code>")

    return escaped


def _starts_fenced_code(line: str) -> bool:
    return line.startswith("```")


def _starts_heading(line: str) -> bool:
    return _HEADING_RE.match(line) is not None


def _starts_horizontal_rule(line: str) -> bool:
    return _HR_RE.match(line) is not None


def _starts_unordered_list(line: str) -> bool:
    return _UL_RE.match(line) is not None


def _starts_ordered_list(line: str) -> bool:
    return _OL_RE.match(line) is not None


def _starts_blockquote(line: str) -> bool:
    return line.startswith("> ")


_BLOCK_STARTERS: tuple[tuple[str, Callable[[str], bool]], ...] = (
    ("pre", _starts_fenced_code),
    ("heading", _starts_heading),
    ("hr", _starts_horizontal_rule),
    ("ul", _starts_unordered_list),
    ("ol", _starts_ordered_list),
    ("blockquote", _starts_blockquote),
)


def _block_kind(line: str) -> str | None:
    for kind, predicate in _BLOCK_STARTERS:
        if predicate(line):
            return kind
    return None


def _render_fenced_code(lines: list[str], index: int) -> tuple[str, int]:
    index += 1
    code_lines: list[str] = []
    while index < len(lines) and not _starts_fenced_code(lines[index]):
        code_lines.append(lines[index])
        index += 1
    if index < len(lines):
        index += 1
    return f"<pre>{html.escape(chr(10).join(code_lines))}</pre>", index


def _render_heading(line: str, index: int) -> tuple[str, int]:
    match = _HEADING_RE.match(line)
    if match is None:
        raise ValueError(f"Expected Markdown heading: {line!r}")
    level = 1 if len(match.group(1)) == 1 else 2
    return f"<h{level}>{_inline_markdown_to_html(match.group(2))}</h{level}>", index + 1


def _render_list(
    lines: list[str],
    index: int,
    pattern: re.Pattern[str],
    tag: str,
) -> tuple[str, int]:
    items: list[str] = []
    while index < len(lines):
        match = pattern.match(lines[index])
        if match is None:
            break
        items.append(f"<li>{_inline_markdown_to_html(match.group(1))}</li>")
        index += 1
    return f"<{tag}>{''.join(items)}</{tag}>", index


def _render_blockquote(lines: list[str], index: int) -> tuple[str, int]:
    quote_lines: list[str] = []
    while index < len(lines) and _starts_blockquote(lines[index]):
        quote_lines.append(lines[index][2:])
        index += 1
    rendered = _inline_markdown_to_html(chr(10).join(quote_lines))
    return f"<blockquote>{rendered}</blockquote>", index


def _render_paragraph(lines: list[str], index: int) -> tuple[str, int]:
    paragraph: list[str] = []
    while index < len(lines):
        candidate = lines[index]
        if not candidate.strip() or _block_kind(candidate) is not None:
            break
        paragraph.append(candidate)
        index += 1
    return _inline_markdown_to_html("\n".join(paragraph)), index


def _render_horizontal_rule(_lines: list[str], index: int) -> tuple[str, int]:
    return "<hr/>", index + 1


def _render_unordered_list(lines: list[str], index: int) -> tuple[str, int]:
    return _render_list(lines, index, _UL_RE, "ul")


def _render_ordered_list(lines: list[str], index: int) -> tuple[str, int]:
    return _render_list(lines, index, _OL_RE, "ol")


def _render_heading_block(lines: list[str], index: int) -> tuple[str, int]:
    return _render_heading(lines[index], index)


_BLOCK_RENDERERS: dict[str, Callable[[list[str], int], tuple[str, int]]] = {
    "pre": _render_fenced_code,
    "heading": _render_heading_block,
    "hr": _render_horizontal_rule,
    "ul": _render_unordered_list,
    "ol": _render_ordered_list,
    "blockquote": _render_blockquote,
}


def _render_next_block(lines: list[str], index: int) -> tuple[str, int]:
    kind = _block_kind(lines[index])
    renderer = _BLOCK_RENDERERS.get(kind)
    if renderer is None:
        renderer = _render_paragraph
    return renderer(lines, index)


def markdown_to_asana_html(markdown: str) -> str:
    """Convert lightweight Markdown into Asana's XML-safe rich-text fragment."""
    markdown = markdown.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not markdown:
        return "<body></body>"

    lines = markdown.split("\n")
    output: list[str] = []
    index = 0

    while index < len(lines):
        if not lines[index].strip():
            output.append("")
            index += 1
            continue

        rendered, index = _render_next_block(lines, index)
        output.append(rendered)

    return f"<body>{chr(10).join(output)}</body>"


_START_MARKERS = {
    "strong": "**",
    "em": "*",
    "s": "~~",
    "code": "`",
}

_END_MARKERS = _START_MARKERS


class _AsanaHTMLToMarkdownParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.list_stack: list[tuple[str, int]] = []
        self.link_stack: list[str | None] = []

    def _newline(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def _start_h1(self, _attrs: dict[str, str | None]) -> None:
        self._newline()
        self.parts.append("# ")

    def _start_h2(self, _attrs: dict[str, str | None]) -> None:
        self._newline()
        self.parts.append("## ")

    def _start_blockquote(self, _attrs: dict[str, str | None]) -> None:
        self._newline()
        self.parts.append("> ")

    def _start_pre(self, _attrs: dict[str, str | None]) -> None:
        self._newline()
        self.parts.append("```\n")

    def _start_ul(self, _attrs: dict[str, str | None]) -> None:
        self._newline()
        self.list_stack.append(("ul", 0))

    def _start_ol(self, _attrs: dict[str, str | None]) -> None:
        self._newline()
        self.list_stack.append(("ol", 0))

    def _start_li(self, _attrs: dict[str, str | None]) -> None:
        self._newline()
        if not self.list_stack:
            return

        list_type, item_index = self.list_stack[-1]
        item_index += 1
        self.list_stack[-1] = (list_type, item_index)
        self.parts.append("- " if list_type == "ul" else f"{item_index}. ")

    def _start_a(self, attrs: dict[str, str | None]) -> None:
        href = attrs.get("href")
        self.link_stack.append(href)
        if href:
            self.parts.append("[")

    def _start_hr(self, _attrs: dict[str, str | None]) -> None:
        self._newline()
        self.parts.append("---\n")

    def _start_img(self, attrs: dict[str, str | None]) -> None:
        alt = attrs.get("alt") or "Asana image"
        asset_gid = attrs.get("data-asana-gid")
        src = attrs.get("src")
        target = (
            f"https://app.asana.com/app/asana/-/get_asset?asset_id={asset_gid}"
            if asset_gid
            else src
        )
        if not target:
            return
        self._newline()
        self.parts.append(f"[{alt}]({target})")
        self._newline()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "body":
            return

        marker = _START_MARKERS.get(tag)
        if marker is not None:
            self.parts.append(marker)
            return

        handler = getattr(self, f"_start_{tag}", None)
        if handler is not None:
            handler(dict(attrs))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def _end_line_block(self) -> None:
        self._newline()

    def _end_pre(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")
        self.parts.append("```\n")

    def _end_list(self) -> None:
        if self.list_stack:
            self.list_stack.pop()
        self._newline()

    def _end_a(self) -> None:
        href = self.link_stack.pop() if self.link_stack else None
        if href:
            self.parts.append(f"]({href})")

    def handle_endtag(self, tag: str) -> None:
        marker = _END_MARKERS.get(tag)
        if marker is not None:
            self.parts.append(marker)
            return

        if tag in {"h1", "h2", "blockquote", "li"}:
            self._end_line_block()
        elif tag == "pre":
            self._end_pre()
        elif tag in {"ul", "ol"}:
            self._end_list()
        elif tag == "a":
            self._end_a()

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def asana_html_to_markdown(html_notes: str) -> str:
    """Convert Asana rich text into readable Markdown."""
    if not html_notes:
        return ""

    parser = _AsanaHTMLToMarkdownParser()
    parser.feed(html_notes)
    parser.close()

    markdown = "".join(parser.parts).replace("\xa0", " ")
    markdown = re.sub(r"[ \t]+\n", "\n", markdown)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    return markdown.strip()
