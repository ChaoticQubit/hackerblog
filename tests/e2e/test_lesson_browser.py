"""Drive the rendered lesson in the installed Google Chrome over the DevTools protocol.

Stdlib only: a tiny WebSocket client speaks CDP to a headless Chrome. Checks zero console
errors, zero CSP violations, zero network requests, keyboard-only completion of every quiz
item, game, slider and flashcard, and in-page accessibility rules. Skipped without Chrome.
"""

import base64
import http.client
import json
import os
import shutil
import socket
import struct
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from tests.samples import digest_data, lesson_data
from tests.unit.test_render_lesson import sample_meta
from tests.unit.test_render_xss import xss_inputs

from hackerblog.core.llm.schemas import LessonSpec, PostDigest
from hackerblog.core.render.index import IndexEntry, render_index
from hackerblog.core.render.lesson import render_lesson

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / ".local-state" / "render"
CHROME_CANDIDATES = (
    os.environ.get("HACKERBLOG_CHROME", ""),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
)
CHROME = next((path for path in CHROME_CANDIDATES if path and Path(path).is_file()), None)
pytestmark = pytest.mark.skipif(CHROME is None, reason="Google Chrome is not installed")

CSP_PROBE = (
    "window.__csp = [];"
    "document.addEventListener('securitypolicyviolation',"
    " e => window.__csp.push(e.violatedDirective + ' ' + e.blockedURI));"
)
KEYS = {
    "Tab": 9,
    "Enter": 13,
    " ": 32,
    "ArrowUp": 38,
    "ArrowDown": 40,
    "ArrowRight": 39,
    "ArrowLeft": 37,
}
A11Y_SCRIPT = """(() => {
  const out = [];
  const parse = (value) => {
    const match = value.match(/rgba?\\(([^)]+)\\)/);
    if (!match) { return null; }
    const parts = match[1].split(",").map(Number);
    return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
  };
  const channel = (v) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  const lum = (c) => 0.2126 * channel(c.r) + 0.7152 * channel(c.g) + 0.0722 * channel(c.b);
  const background = (node) => {
    for (let n = node; n; n = n.parentElement) {
      const c = parse(getComputedStyle(n).backgroundColor);
      if (c && c.a > 0.5) { return c; }
    }
    return { r: 255, g: 255, b: 255, a: 1 };
  };
  const ratio = (a, b) => {
    const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
    return (x + 0.05) / (y + 0.05);
  };
  const shown = (node) => {
    const box = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    const hidden = style.visibility === "hidden" || node.closest(".visually-hidden, .skip");
    return box.width > 0 && box.height > 0 && !hidden;
  };
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const seen = new Set();
  while (walker.nextNode()) {
    const node = walker.currentNode.parentElement;
    const empty = !walker.currentNode.textContent.trim();
    if (empty || !node || seen.has(node) || node.closest("svg") || !shown(node)) { continue; }
    seen.add(node);
    const style = getComputedStyle(node);
    const size = parseFloat(style.fontSize);
    const large = size >= 24 || (Number(style.fontWeight) >= 700 && size >= 18.66);
    const need = large ? 3 : 4.5;
    const got = ratio(parse(style.color), background(node));
    const text = node.textContent.trim().slice(0, 40);
    if (got < need) { out.push(`contrast ${got.toFixed(2)} < ${need}: ${node.tagName} "${text}"`); }
  }
  if (!document.documentElement.lang) { out.push("html has no lang"); }
  if (document.querySelectorAll("h1").length !== 1) { out.push("page needs exactly one h1"); }
  let last = 0;
  document.querySelectorAll("h1, h2, h3, h4, h5, h6").forEach((h) => {
    const level = Number(h.tagName[1]);
    if (level > last + 1) { out.push(`heading jumps to ${h.tagName}: ${h.textContent.trim()}`); }
    last = level;
  });
  document.querySelectorAll("input, select, textarea").forEach((f) => {
    if (f.labels.length === 0 && !f.getAttribute("aria-label")) {
      out.push(`unlabelled ${f.tagName} ${f.id}`);
    }
  });
  document.querySelectorAll("button, a[href], summary").forEach((b) => {
    if (!(b.getAttribute("aria-label") || b.textContent.trim())) {
      out.push(`nameless ${b.tagName}`);
    }
  });
  document.querySelectorAll("button, select, input[type=text], input[type=range]").forEach((b) => {
    const box = b.getBoundingClientRect();
    if (shown(b) && (box.width < 24 || box.height < 24)) {
      out.push(`small target ${b.tagName} ${b.textContent.trim()}`);
    }
  });
  const ids = Array.from(document.querySelectorAll("[id]"), (n) => n.id);
  ids.filter((id, i) => ids.indexOf(id) !== i).forEach((id) => out.push(`duplicate id ${id}`));
  const wide = document.documentElement.scrollWidth;
  if (wide > window.innerWidth + 1) { out.push(`horizontal overflow ${wide}px`); }
  return out;
})()"""


class WebSocket:
    """Minimal RFC 6455 text-frame client, enough for the DevTools protocol."""

    def __init__(self, url: str) -> None:
        assert url.startswith("ws://127.0.0.1:"), url
        host_port, path = url[len("ws://") :].split("/", 1)
        port = int(host_port.split(":")[1])
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET /{path} HTTP/1.1\r\nHost: {host_port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            response += self.sock.recv(1)
        assert response.startswith(b"HTTP/1.1 101"), response[:80]

    def send(self, text: str) -> None:
        payload = text.encode()
        mask = os.urandom(4)
        size = len(payload)
        if size < 126:
            header = struct.pack("!BB", 0x81, 0x80 | size)
        elif size < 65536:
            header = struct.pack("!BBH", 0x81, 0x80 | 126, size)
        else:
            header = struct.pack("!BBQ", 0x81, 0x80 | 127, size)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def _exact(self, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = self.sock.recv(count - len(data))
            assert chunk, "socket closed"
            data += chunk
        return data

    def recv(self) -> str:
        message = b""
        while True:
            first, second = self._exact(2)
            size = second & 0x7F
            if size == 126:
                size = struct.unpack("!H", self._exact(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._exact(8))[0]
            payload = self._exact(size)
            opcode = first & 0x0F
            if opcode == 0x9:
                self.sock.sendall(struct.pack("!BB", 0x8A, 0x80) + b"\0\0\0\0")
                continue
            assert opcode in {0x0, 0x1}, f"unexpected opcode {opcode}"
            message += payload
            if first & 0x80:
                return message.decode()


@dataclass
class Page:
    """One Chrome tab driven over CDP; events are kept for later assertions."""

    ws: WebSocket
    events: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    next_id: int = 0

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        self.next_id += 1
        self.ws.send(json.dumps({"id": self.next_id, "method": method, "params": params}))
        while True:
            message = cast(dict[str, Any], json.loads(self.ws.recv()))
            if message.get("id") == self.next_id:
                assert "error" not in message, message
                return cast(dict[str, Any], message.get("result", {}))
            self.events.append(message)

    def wait_for(self, method: str, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        while not any(e.get("method") == method for e in self.events):
            assert time.monotonic() < deadline, f"timed out waiting for {method}"
            self.events.append(cast(dict[str, Any], json.loads(self.ws.recv())))

    def js(self, expression: str) -> Any:
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        assert "exceptionDetails" not in result, result
        return result["result"].get("value")

    def key(self, key: str, shift: bool = False) -> None:
        code = KEYS[key]
        text = {"Enter": "\r", " ": " "}.get(key, "")
        common: dict[str, Any] = {
            "key": key,
            "windowsVirtualKeyCode": code,
            "modifiers": 8 if shift else 0,
        }
        self.call("Input.dispatchKeyEvent", type="keyDown", text=text, **common)
        self.call("Input.dispatchKeyEvent", type="keyUp", **common)

    def type_text(self, text: str) -> None:
        for char in text:
            self.call("Input.dispatchKeyEvent", type="keyDown", key=char, text=char)
            self.call("Input.dispatchKeyEvent", type="keyUp", key=char)

    def tab_to(self, selector: str, limit: int = 400) -> None:
        probe = f"document.activeElement && document.activeElement.matches({json.dumps(selector)})"
        for _ in range(limit):
            if self.js(probe):
                return
            self.key("Tab")
        raise AssertionError(f"Tab never reached {selector}")

    def screenshot(self, name: str) -> None:
        shot = self.call("Page.captureScreenshot", format="png", captureBeyondViewport=True)
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        (EVIDENCE / name).write_bytes(base64.b64decode(shot["data"]))


def _wait_port(profile: Path, process: subprocess.Popen[bytes]) -> int:
    marker = profile / "DevToolsActivePort"
    deadline = time.monotonic() + 60
    while not marker.is_file() or not marker.read_text().strip():
        assert process.poll() is None, "Chrome exited early"
        assert time.monotonic() < deadline, "Chrome did not open a DevTools port"
        time.sleep(0.1)
    return int(marker.read_text().splitlines()[0])


@pytest.fixture(scope="module")
def chrome(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    assert CHROME is not None
    profile = tmp_path_factory.mktemp("chrome-profile")
    flags = (
        "--headless=new --remote-debugging-port=0 --no-first-run --no-default-browser-check "
        "--disable-extensions --disable-background-networking --disable-component-update "
        "--disable-sync --disable-default-apps --hide-scrollbars"
    )
    args = [CHROME, *flags.split(), f"--user-data-dir={profile}", "about:blank"]
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        yield _wait_port(profile, process)
    finally:
        process.kill()
        process.wait(timeout=30)


def open_page(port: int, url: str, width: int = 1280, scheme: str = "light") -> Page:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request("PUT", "/json/new?about:blank")
    target = cast(dict[str, Any], json.loads(connection.getresponse().read()))
    connection.close()
    page = Page(WebSocket(str(target["webSocketDebuggerUrl"])))
    for domain in ("Page", "Runtime", "Log", "Network"):
        page.call(f"{domain}.enable")
    page.call(
        "Emulation.setDeviceMetricsOverride",
        width=width,
        height=900,
        deviceScaleFactor=1,
        mobile=width < 600,
    )
    page.call(
        "Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": scheme}]
    )
    page.call("Page.addScriptToEvaluateOnNewDocument", source=CSP_PROBE)
    page.call("Page.navigate", url=url)
    page.wait_for("Page.loadEventFired")
    return page


def problems(page: Page) -> list[str]:
    found: list[str] = []
    for event in page.events:
        method, params = event.get("method"), event.get("params", {})
        if method == "Runtime.consoleAPICalled" and params.get("type") == "error":
            found.append(f"console: {params.get('args')}")
        if method == "Runtime.exceptionThrown":
            found.append(f"exception: {params.get('exceptionDetails')}")
        if method == "Log.entryAdded" and params["entry"].get("level") == "error":
            found.append(f"log: {params['entry'].get('text')}")
        if method == "Network.requestWillBeSent":
            url = str(params["request"]["url"])
            if not url.startswith(("file://", "data:")):
                found.append(f"network: {url}")
        if method == "Page.javascriptDialogOpening":
            found.append("dialog opened")
    found.extend(f"csp: {v}" for v in page.js("window.__csp"))
    return found


def write_lesson(directory: Path, digest: PostDigest, lesson: LessonSpec, **meta: Any) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "lesson.html"
    path.write_text(render_lesson(sample_meta(**meta), digest, lesson), encoding="utf-8")
    return path.as_uri()


@pytest.fixture(scope="module")
def lesson_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    digest = PostDigest.model_validate(digest_data())
    lesson = LessonSpec.model_validate(lesson_data())
    return write_lesson(tmp_path_factory.mktemp("lesson"), digest, lesson)


def answer_choice(page: Page, widget: str, index: int) -> None:
    page.tab_to(f"#{widget} input[type=radio]")
    page.key(" ")
    for _ in range(index):
        page.key("ArrowDown")


def answer_ordering(page: Page, widget: str, count: int) -> None:
    for target in range(count):
        page.tab_to(f'#{widget} li[data-index="{target}"]')
        position = page.js(
            f"Array.from(document.querySelectorAll('#{widget} li')).indexOf(document.activeElement)"
        )
        for _ in range(int(position) - target):
            page.key("ArrowUp")


def answer_pairs(page: Page, widget: str, rights: list[str]) -> None:
    for index, right in enumerate(rights):
        page.tab_to(f"#{widget}-pair-{index}")
        page.type_text(right.split()[0])
        time.sleep(1.1)


def check(page: Page, widget: str) -> str:
    page.tab_to(f"#{widget} button.check")
    page.key("Enter")
    return str(page.js(f"document.querySelector('#{widget} .feedback').dataset.state"))


def test_quiz_and_games_complete_by_keyboard(chrome: int, lesson_url: str) -> None:
    raw = lesson_data()
    page = open_page(chrome, lesson_url)
    page.tab_to("#w-check-0 input[type=text]")
    page.type_text(raw["sections"][0]["check"]["accepted_answers"][0])
    page.key("Enter")
    assert page.js("document.querySelector('#w-check-0 .feedback').dataset.state") == "ok"
    answer_choice(page, "w-game", raw["game"]["bug_line"])
    assert check(page, "w-game") == "ok"
    answer_choice(page, "w-final-0", raw["final_quiz"][0]["correct_index"])
    assert check(page, "w-final-0") == "ok"
    answer_ordering(page, "w-final-1", len(raw["final_quiz"][1]["steps_in_order"]))
    assert check(page, "w-final-1") == "ok"
    page.tab_to("#w-final-2 input[type=text]")
    page.type_text("jitter")
    assert check(page, "w-final-2") == "ok"
    answer_pairs(page, "final-3", [right for _, right in raw["final_quiz"][3]["pairs"]])
    assert check(page, "w-final-3") == "ok"
    assert page.js("document.getElementById('score').textContent") == "Your score: 4 of 4."
    page.screenshot("e2e-lesson-answered.png")
    assert problems(page) == []


def test_slider_flashcards_and_copy_by_keyboard(chrome: int, lesson_url: str) -> None:
    page = open_page(chrome, lesson_url)
    page.tab_to("#explore-range")
    for _ in range(19):
        page.key("ArrowRight")
    assert page.js("document.querySelector('.explore-outcome').textContent").startswith("Twenty")
    page.tab_to(".cards-face")
    page.key("Enter")
    assert page.js("document.querySelector('.cards-face').getAttribute('aria-pressed')") == "true"
    page.tab_to("#ask-question")
    page.type_text(""" it's "$HOME" `id`""")
    command = page.js("document.getElementById('ask-command').textContent")
    assert command == """hackerblog ask --post 7 'it'\\''s "$HOME" `id`'"""
    assert problems(page) == []


def test_accessibility_rules_in_both_themes(chrome: int, lesson_url: str) -> None:
    log: list[str] = []
    for scheme in ("light", "dark"):
        for width in (1280, 390):
            page = open_page(chrome, lesson_url, width=width, scheme=scheme)
            found = cast(list[str], page.js(A11Y_SCRIPT))
            log.append(f"{scheme} {width}px: {len(found)} problems")
            log.extend(f"  {line}" for line in found)
            page.screenshot(f"e2e-lesson-{scheme}-{width}.png")
            assert found == [], found
            assert problems(page) == []
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "a11y.log").write_text("\n".join(log) + "\n", encoding="utf-8")


def test_reduced_motion_disables_animation(chrome: int, lesson_url: str) -> None:
    page = open_page(chrome, lesson_url)
    page.call(
        "Emulation.setEmulatedMedia",
        features=[{"name": "prefers-reduced-motion", "value": "reduce"}],
    )
    assert page.js("getComputedStyle(document.documentElement).scrollBehavior") == "auto"
    assert page.js("getComputedStyle(document.querySelector('.btn')).transitionDuration") == "0s"


def test_hostile_lesson_and_index_stay_inert(chrome: int, tmp_path: Path) -> None:
    digest, lesson = xss_inputs()
    url = write_lesson(tmp_path / "hostile", digest, lesson, title="<img src=x onerror=alert(1)>")
    page = open_page(chrome, url)
    assert page.js("document.querySelectorAll('img, iframe, [onerror], [onclick]').length") == 0
    assert problems(page) == []
    folder = "alpha/2026-01-01-x"
    entry = IndexEntry(
        "alpha",
        "<b>x</b>",
        "<script>alert(1)</script>",
        "2026-01-01",
        f"{folder}/lesson.html",
        f"{folder}/notes.md",
        "unread",
    )
    index = tmp_path / "index.html"
    index.write_text(render_index([entry]), encoding="utf-8")
    page = open_page(chrome, index.as_uri())
    page.screenshot("e2e-index.png")
    assert problems(page) == []
