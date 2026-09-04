from wanda.watchers.imap_watcher import _strip_html, parse_raw


def test_strip_html_removes_terminated_script():
    assert "alert" not in _strip_html("hello <script>alert(1)</script> world")


def test_strip_html_removes_unterminated_script_to_end():
    # A 64KB partial fetch routinely truncates a body mid-tag; an unterminated
    # <script> must not leak its contents as prose.
    out = _strip_html("intro <script>secret payload never closed")
    assert "secret payload" not in out
    assert "intro" in out


def test_strip_html_removes_unterminated_style_to_end():
    out = _strip_html("visible <style>.x{color:red} truncated")
    assert "color:red" not in out
    assert "visible" in out


def _mime(headers: str, body: str) -> bytes:
    return (headers.strip() + "\r\n\r\n" + body).encode()


def test_parse_raw_returns_body_key_not_snippet():
    raw = _mime("From: a@b.c\r\nSubject: hi", "plain body")
    p = parse_raw(raw, 4096)
    assert "body" in p and "snippet" not in p
    assert "plain body" in p["body"]


def test_parse_raw_html_body_is_stripped():
    raw = _mime("From: a@b.c\r\nSubject: hi\r\nContent-Type: text/html",
                "<p>hello <script>evil()</script></p>")
    p = parse_raw(raw, 4096)
    assert "evil" not in p["body"]
    assert "hello" in p["body"]


def test_parse_raw_truncated_fallback_strips_html():
    # A body the structured parser cannot handle (garbage MIME) falls back to
    # the raw tail — which must still be stripped, not spliced in raw.
    raw = b"From: a@b.c\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n" \
          b"--x\r\nContent-Type: text/html\r\n\r\n<div>hi<script>evil()"
    p = parse_raw(raw, 4096)
    assert "evil()" not in p["body"]
