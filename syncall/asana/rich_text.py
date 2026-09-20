"""Convert between Taskwarrior Markdown notes and Asana rich text."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

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
    escaped = html.escape(text, quote=True)

    code_spans: list[str] = []

    def stash_code(match: re.Match[str]) -> str:
        code_spans.append(match.group(1))
        return f"\x00CODE{len(code_spans) - 1}\x00"

    escaped = _CODE_RE.sub(stash_code, escaped)
    escaped = _LINK_RE.sub(
        lambda match: f'<a href="{match.group(2)}">{match.group(1)}</a>',
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


def _block_kind(line: str) -> str | None:
    if line.startswith("```"):
        return "pre"
    if _HEADING_RE.match(line):
        return "heading"
    if _HR_RE.match(line):
        return "hr"
    if _UL_RE.match(line):
        return "ul"
    if _OL_RE.match(line):
        return "ol"
    if line.startswith("> "):
        return "blockquote"
    return None


def markdown_to_asana_html(markdown: str) -> str:
    """Convert lightweight Markdown into Asana's XML-safe rich-text fragment."""
    markdown = markdown.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not markdown:
        return "<body></body>"

    lines = markdown.split("\n")
    output: list[str] = []
    index = 0

    while index < len(lines):
        line = lines[index]

        if not line.strip():
            output.append("")
            index += 1
            continue

        if line.startswith("```"):
            index += 1
            code_lines: list[str] = []
            while index < len(lines) and not lines[index].startswith("```"):
                code_lines.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            output.append(f"<pre>{html.escape(chr(10).join(code_lines))}</pre>")
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            level = 1 if len(heading.group(1)) == 1 else 2
            output.append(
                f"<h{level}>{_inline_markdown_to_html(heading.group(2))}</h{level}>",
            )
            index += 1
            continue

        if _HR_RE.match(line):
            output.append("<hr/>")
            index += 1
            continue

        if _UL_RE.match(line):
            items: list[str] = []
            while index < len(lines):
                match = _UL_RE.match(lines[index])
                if not match:
                    break
                items.append(f"<li>{_inline_markdown_to_html(match.group(1))}</li>")
                index += 1
            output.append(f"<ul>{''.join(items)}</ul>")
            continue

        if _OL_RE.match(line):
            items = []
            while index < len(lines):
                match = _OL_RE.match(lines[index])
                if not match:
                    break
                items.append(f"<li>{_inline_markdown_to_html(match.group(1))}</li>")
                index += 1
            output.append(f"<ol>{''.join(items)}</ol>")
            continue

        if line.startswith("> "):
            quote_lines: list[str] = []
            while index < len(lines) and lines[index].startswith("> "):
                quote_lines.append(lines[index][2:])
                index += 1
            output.append(
                f"<blockquote>{_inline_markdown_to_html(chr(10).join(quote_lines))}</blockquote>",
            )
            continue

        paragraph: list[str] = []
        while index < len(lines):
            candidate = lines[index]
            if not candidate.strip() or _block_kind(candidate) is not None:
                break
            paragraph.append(candidate)
            index += 1
        output.append(_inline_markdown_to_html("\n".join(paragraph)))

    return f"<body>{chr(10).join(output)}</body>"


class _AsanaHTMLToMarkdownParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.list_stack: list[tuple[str, int]] = []
        self.link_stack: list[str | None] = []

    def _newline(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)

        if tag == "body":
            return
        if tag == "strong":
            self.parts.append("**")
        elif tag == "em":
            self.parts.append("*")
        elif tag == "s":
            self.parts.append("~~")
        elif tag == "code":
            self.parts.append("`")
        elif tag in {"h1", "h2"}:
            self._newline()
            self.parts.append("# " if tag == "h1" else "## ")
        elif tag == "blockquote":
            self._newline()
            self.parts.append("> ")
        elif tag == "pre":
            self._newline()
            self.parts.append("```\n")
        elif tag in {"ul", "ol"}:
            self._newline()
            self.list_stack.append((tag, 0))
        elif tag == "li":
            self._newline()
            if self.list_stack:
                list_type, item_index = self.list_stack[-1]
                item_index += 1
                self.list_stack[-1] = (list_type, item_index)
                self.parts.append("- " if list_type == "ul" else f"{item_index}. ")
        elif tag == "a":
            href = attrs_dict.get("href")
            self.link_stack.append(href)
            if href:
                self.parts.append("[")
        elif tag == "hr":
            self._newline()
            self.parts.append("---\n")
        elif tag == "img":
            fallback = attrs_dict.get("alt") or attrs_dict.get("src")
            if fallback:
                self._newline()
                self.parts.append(fallback)
                self._newline()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "strong":
            self.parts.append("**")
        elif tag == "em":
            self.parts.append("*")
        elif tag == "s":
            self.parts.append("~~")
        elif tag == "code":
            self.parts.append("`")
        elif tag in {"h1", "h2", "blockquote", "li"}:
            self._newline()
        elif tag == "pre":
            if self.parts and not self.parts[-1].endswith("\n"):
                self.parts.append("\n")
            self.parts.append("```\n")
        elif tag in {"ul", "ol"}:
            if self.list_stack:
                self.list_stack.pop()
            self._newline()
        elif tag == "a":
            href = self.link_stack.pop() if self.link_stack else None
            if href:
                self.parts.append(f"]({href})")

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
