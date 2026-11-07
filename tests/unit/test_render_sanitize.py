import re

from markdown_it import MarkdownIt

from hackerblog.core.render.sanitize import (
    clean_markdown,
    markdown_html,
    plain,
    strip_markdown_html,
)


def test_markdown_html_keeps_allowed_structure() -> None:
    html = markdown_html("## Title\n\nSome **bold** and `code`.\n\n- a\n- b\n")
    assert "<h3>Title</h3>" in html
    assert "<strong>bold</strong>" in html
    assert "<code>code</code>" in html
    assert "<ul>" in html


def test_markdown_html_escapes_raw_html() -> None:
    html = markdown_html('<script>alert(1)</script>\n\n<img src=x onerror="alert(1)">')
    assert "<script" not in html
    assert "<img" not in html
    assert "&lt;script&gt;" in html


def test_markdown_html_drops_unsafe_links_and_forces_rel() -> None:
    html = markdown_html("[bad](javascript:alert(1)) [ok](https://example.com/a)")
    assert 'href="javascript' not in html
    assert 'href="https://example.com/a"' in html
    assert 'rel="noopener noreferrer nofollow"' in html


def test_markdown_html_table_and_code_block() -> None:
    html = markdown_html("```python\nx = 1 < 2\n```\n")
    assert "<pre><code>x = 1 &lt; 2\n</code></pre>" in html


def test_strip_markdown_html_removes_tags_outside_code() -> None:
    text = (
        "Hello <script>alert(1)</script> <b onclick=x>bold</b> <!-- hidden -->\n\n"
        '<div style="x">\nblock\n</div>\n\n'
        "Inline `List<String>` stays.\n\n"
        "```html\n<p>kept in fence</p>\n```\n"
    )
    out = strip_markdown_html(text)
    assert "<script>" not in out and "<b " not in out and "<div" not in out
    assert "<!--" not in out
    assert "`List<String>`" in out
    assert "<p>kept in fence</p>" in out
    assert "bold" in out and "block" in out


def test_strip_markdown_html_neutralises_script_links() -> None:
    out = strip_markdown_html("[x](javascript:alert(1)) [y]( JavaScript:void(0)) [z](https://a.b)")
    assert "javascript:" not in out.lower()
    assert "(https://a.b)" in out


def test_strip_markdown_html_keeps_autolinks() -> None:
    assert strip_markdown_html("see <https://example.com>") == "see <https://example.com>"
    assert strip_markdown_html("x <5> y") == "x <5> y"


def link_targets(markdown: str) -> list[str]:
    permissive = MarkdownIt("commonmark")
    permissive.validateLink = lambda url: True
    found: list[str] = []
    for block in permissive.parse(markdown):
        for token in block.children or []:
            if token.type == "link_open":
                found.append(str(token.attrs["href"]))
            if token.type == "image":
                found.append(str(token.attrs["src"]))
    return found


def test_strip_markdown_html_drops_every_unsafe_link_target() -> None:
    hostile = (
        "[a](java&#58;script:alert(1)) [b](data:text/html;base64,PHNjcmlwdD4=)\n"
        "![c](data:image/png;base64,AAAA) [d](<javascript:alert(1)>)\n"
        "[e](java&#9;script:alert(1)) [f](//evil.example/x) [g](VBScript:x)\n"
        "[h](mailto:a@b.example) <javascript:alert(1)> <a@b.example>\n\n"
        "[ref]\n\n[ref]: javascript:alert(1)\n"
    )
    out = strip_markdown_html(hostile)
    assert set(link_targets(out)) <= {"#"}
    assert "java&#58;script" not in out and "data:" not in out


def test_strip_markdown_html_keeps_safe_and_code_links() -> None:
    text = (
        "[ok](https://a.example/c) [rel](other/page.html) [frag](#sec)\n\n"
        "Inline `[x](javascript:y)` code.\n\n```md\n[x](javascript:z)\n```\n"
    )
    out = strip_markdown_html(text)
    assert link_targets(out) == ["https://a.example/c", "other/page.html", "#sec"]
    assert "`[x](javascript:y)`" in out and "[x](javascript:z)\n" in out


def test_markdown_html_denies_relative_urls_except_anchors() -> None:
    html = markdown_html("[a](/etc/passwd) [b](page.html) [c](#part) [d](//evil.example/x)")
    assert re.findall(r'href="([^"]*)"', html) == ["#part"]


def test_escape_fallback_leaves_no_links_outside_code() -> None:
    text = "[a](https://a.example) <https://b.example> ![i](x.png) `[c](d)`\n"
    out = clean_markdown(text, escape=True)
    assert link_targets(out) == []
    assert "`[c](d)`" in out


def test_plain_leaves_no_link_in_one_line_fields() -> None:
    probes = [
        "[x](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)",
        "[x](javascript&#58;alert(1))",
        "see [x](javascript&#58;alert(1)) and ![i](data:image/svg+xml,<svg/onload=alert(1)>)",
        "<javascript&#58;alert(1)> <data:text/html,x>",
        "[x] [x]: data:text/html,x",
    ]
    for probe in probes:
        out = plain(probe)
        assert link_targets(out) == [], (probe, out)
        assert "\n" not in out
