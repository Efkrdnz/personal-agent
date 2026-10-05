"""The HUD's three files, read as text, against the contract they are served under.

The server sends ``default-src 'none'; script-src 'self'; style-src 'self'``, so
an inline handler, a ``style=""`` attribute or a CDN link is not a style
problem: it is a page that silently does nothing in the user's window while
every Python test stays green. Nothing in CI runs a browser, so these checks are
what stands between an edit and a dead HUD.

Two kinds of check live here. The security ones (no markup from data, no
eval, no external URL) are the contract's. The wiring ones (every id the
script looks up exists, every endpoint it calls is served) are this repo's bug
class: a missing caller, or a caller of nothing, raises nowhere until a person
opens the window.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

import jarvis

STATIC = Path(jarvis.__file__).resolve().parent / "window" / "static"
FILES = ("index.html", "app.css", "app.js")

#: The whole API surface: the window contract's section 5 and the app
#: contract's section 7. The page may call nothing else, and a test below also
#: insists it calls all of these.
ENDPOINTS = frozenset(
    {
        "/api/state",
        "/api/feed",
        "/api/stream",
        "/api/tool",
        "/api/chat",
        "/api/say",
        "/api/answer",
        "/api/stop",
        "/api/setup",
        "/api/setup/secret",
        "/api/setup/setting",
        "/api/setup/wake",
        "/api/setup/preview",
        "/api/setup/claude",
        "/api/app",
        "/api/app/restart",
        "/api/app/quit",
    }
)

#: The one page a link may open: where a Gemini key comes from. It opens in
#: the browser proper (target=_blank), never inside this window.
NAVIGATION = frozenset({"https://aistudio.google.com/apikey"})

#: The only properties a browser animates on the compositor. Anything else in a
#: keyframe or a transition repaints every frame of a window left open all day.
CHEAP = frozenset({"transform", "opacity"})


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _strip_js_comments(src: str) -> str:
    """Drop // and /* */ comments so a sentence ABOUT innerHTML is not a use of it.

    Good enough for this file: it has no regex literal or string containing
    a comment opener, and the test that would break if it grew one is this one.
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)(^|[^:\"'\\])//.*$", r"\1", src)


def _strip_css_comments(src: str) -> str:
    return re.sub(r"/\*.*?\*/", "", src, flags=re.S)


class _Tags(HTMLParser):
    """Every start tag with its attributes, and the text inside each <script>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.script_bodies: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        if tag == "script":
            self._in_script = True
            self.script_bodies.append("")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self.script_bodies[-1] += data


@pytest.fixture(scope="module")
def html() -> str:
    return _read("index.html")


@pytest.fixture(scope="module")
def tags(html: str) -> _Tags:
    parser = _Tags()
    parser.feed(html)
    parser.close()
    return parser


@pytest.fixture(scope="module")
def js() -> str:
    return _strip_js_comments(_read("app.js"))


@pytest.fixture(scope="module")
def css() -> str:
    return _strip_css_comments(_read("app.css"))


# ───────────────────────────── the files ─────────────────────────────


@pytest.mark.parametrize("name", FILES)
def test_the_file_exists_and_is_not_empty(name: str) -> None:
    path = STATIC / name
    assert path.is_file(), f"{path} is missing; the server's allow-list serves it"
    assert path.stat().st_size > 0


def test_index_loads_exactly_the_two_served_assets(tags: _Tags) -> None:
    sheets = [a.get("href") for t, a in tags.tags if t == "link" and a.get("rel") == "stylesheet"]
    scripts = [a.get("src") for t, a in tags.tags if t == "script"]
    assert sheets == ["/static/app.css"]
    assert scripts == ["/static/app.js"]


def test_the_script_is_deferred(tags: _Tags) -> None:
    # Without defer it runs before the body exists, and every lookup returns null.
    script = next(a for t, a in tags.tags if t == "script")
    assert "defer" in script


def test_index_declares_a_viewport_and_a_language(tags: _Tags) -> None:
    metas = [a for t, a in tags.tags if t == "meta"]
    assert any(a.get("name") == "viewport" and a.get("content") for a in metas)
    root = next(a for t, a in tags.tags if t == "html")
    assert root.get("lang"), "<html> needs a lang attribute for screen readers"


# ───────────────────────────── what the CSP forbids ─────────────────────────────


def _without_navigation(text: str) -> str:
    """index.html minus the allowed key link's href, which is navigation, not a fetch."""
    for url in NAVIGATION:
        text = text.replace(f'href="{url}"', 'href="#"')
    return text


@pytest.mark.parametrize("name", FILES)
def test_no_external_url_anywhere(name: str) -> None:
    # connect-src, script-src, style-src and font-src are all 'self': an
    # external URL is not slower, it is blocked. The one exception is a link a
    # person clicks to get a key, and only in the page itself.
    text = _without_navigation(_read(name)) if name == "index.html" else _read(name)
    for pattern in (r"https?://", r"//cdn", r"\bwss?://"):
        hits = re.findall(pattern, text, flags=re.I)
        assert not hits, f"{name} contains {pattern!r}: {hits[:3]}"


def test_no_inline_script_body(tags: _Tags) -> None:
    assert tags.script_bodies, "the page loads no script at all"
    for body in tags.script_bodies:
        assert body.strip() == "", "script-src 'self' refuses an inline script"


def test_no_inline_event_handler_attributes(tags: _Tags) -> None:
    handlers = [(t, k) for t, a in tags.tags for k in a if k.lower().startswith("on")]
    assert not handlers, f"inline handlers never run under this CSP: {handlers}"


def test_no_style_attributes_or_style_elements(html: str, tags: _Tags) -> None:
    styled = [(t, a["style"]) for t, a in tags.tags if "style" in a]
    assert not styled, f"style-src 'self' refuses style=\"\": {styled}"
    assert not any(t == "style" for t, _ in tags.tags), "style-src 'self' refuses <style>"


def test_no_javascript_urls(html: str) -> None:
    assert "javascript:" not in html.lower()


def test_no_markup_from_data(js: str) -> None:
    # The feed holds whatever was said aloud. A sentence is not markup.
    for sink in (
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "DOMParser",
        "createContextualFragment",
    ):
        assert sink not in js, f"app.js uses {sink}"
    for m in re.finditer(r"\.innerHTML\s*(\+?=)\s*([^;\n]*)", js):
        op, value = m.group(1), m.group(2).strip()
        assert op == "=" and value in ('""', "''"), (
            f'innerHTML may only be cleared with = ""; found {m.group(0)!r}'
        )


def test_no_code_from_strings(js: str) -> None:
    assert not re.search(r"\beval\s*\(", js)
    assert "new Function" not in js
    # A string first argument to a timer is eval by another name.
    assert not re.search(r"set(?:Timeout|Interval)\s*\(\s*[\"'`]", js)


# ───────────────────────────── the API it talks to ─────────────────────────────


def test_every_endpoint_the_script_names_is_the_contracts(js: str) -> None:
    named = set(re.findall(r"""["'`](/api/[A-Za-z0-9_/]*)""", js))
    assert named, "app.js names no endpoint at all"
    assert named <= ENDPOINTS, f"not in the contract: {sorted(named - ENDPOINTS)}"


def test_every_contract_endpoint_is_used(js: str) -> None:
    # The reverse direction: a contract endpoint the page never calls is a
    # feature the server offers and the window silently lacks.
    named = set(re.findall(r"""["'`](/api/[A-Za-z0-9_/]*)""", js))
    assert named >= ENDPOINTS, f"never called: {sorted(ENDPOINTS - named)}"
    for key in (
        "state",
        "feed",
        "stream",
        "tool",
        "chat",
        "say",
        "answer",
        "stop",
        "setup",
        "setupSecret",
        "setupSetting",
        "setupWake",
        "setupPreview",
        "setupClaude",
        "appStatus",
        "appRestart",
        "appQuit",
    ):
        assert re.search(rf"\bAPI\.{key}\b", js), f"API.{key} is declared and never used"


def test_the_server_serves_every_endpoint_the_page_names(js: str) -> None:
    # Both halves are this builder's, and a renamed route is a 404 on a click.
    from jarvis.window import server

    named = set(re.findall(r"""["'`](/api/[A-Za-z0-9_/]*)""", js))
    assert named <= set(server._ROUTES), sorted(named - set(server._ROUTES))


def test_one_fetch_and_it_carries_the_token(js: str) -> None:
    # Every request goes through one function, so the header cannot be
    # forgotten on the next endpoint somebody adds.
    assert len(re.findall(r"\bfetch\s*\(", js)) == 1
    assert '"X-Jarvis-Token": app.token' in js


def test_one_event_source_with_the_token_in_the_query(js: str) -> None:
    assert len(re.findall(r"\bnew EventSource\s*\(", js)) == 1
    assert re.search(r"URLSearchParams\(\{\s*t:\s*app\.token\s*\}\)", js)
    assert re.search(r"""\.set\(\s*["']after["']""", js), "a reconnect must resume from `after`"


def test_the_token_leaves_the_address_bar(js: str) -> None:
    assert "location.hash" in js
    assert "history.replaceState" in js


def test_storage_is_only_touched_inside_try(js: str) -> None:
    # sessionStorage throws in a locked-down profile; a direct call is a page
    # that dies on boot there.
    assert not re.search(r"\b(?:local|session)Storage\s*\.", js)
    assert re.search(r"try\s*\{\s*return window\[area\]\.getItem", js)
    assert re.search(r"try\s*\{\s*if \(value === null\) window\[area\]\.removeItem", js)


def test_nothing_polls(js: str) -> None:
    # The stream drives everything; an interval is a cost while nothing happens.
    assert "setInterval" not in js


@pytest.mark.parametrize(
    ("name", "value"),
    [("FEED_CAP", "300"), ("REFRESH_DEBOUNCE_MS", "300"), ("HOLD_MS", "1000")],
)
def test_the_contracts_numbers(js: str, name: str, value: str) -> None:
    assert re.search(rf"\bconst {name} = {value};", js), f"{name} should be {value}"


def test_the_feed_is_capped_at_the_cap(js: str) -> None:
    assert re.search(
        r"childElementCount > FEED_CAP\)\s*dom\.feed\.firstElementChild\.remove\(\)", js
    )


# ───────────────────────────── wiring between the files ─────────────────────────────


def test_every_id_the_script_looks_up_exists(js: str, tags: _Tags) -> None:
    # byId() of a missing id returns null and the first .addEventListener on it
    # throws at boot: a blank HUD, and no Python test anywhere would notice.
    wanted = set(re.findall(r"""(?:byId|fromTemplate)\(\s*["']([^"']+)["']\s*\)""", js))
    present = {a["id"] for _, a in tags.tags if a.get("id")}
    assert wanted, "app.js looks up no ids; the pattern this test reads has changed"
    assert wanted <= present, f"looked up but absent from index.html: {sorted(wanted - present)}"


def test_every_quick_action_button_has_a_preset(js: str, tags: _Tags) -> None:
    buttons = {a["data-quick"] for t, a in tags.tags if t == "button" and a.get("data-quick")}
    block = js[js.index("const QUICK = {") : js.index("const app = {")]
    presets = set(re.findall(r"""^\s{4}["']?([a-z-]+)["']?:\s*\{""", block, flags=re.M))
    assert buttons, "no quick-action buttons"
    assert buttons == presets, f"buttons {sorted(buttons)} vs presets {sorted(presets)}"


def test_every_tab_has_a_panel(tags: _Tags, js: str) -> None:
    tabs = {a["aria-controls"] for t, a in tags.tags if a.get("role") == "tab"}
    panels = {a["id"] for t, a in tags.tags if a.get("role") == "tabpanel"}
    assert tabs == panels
    assert len(tabs) == 7
    # Settings closes the matrix, and the script knows every tab by name.
    names = [a["data-tab"] for t, a in tags.tags if a.get("role") == "tab"]
    assert names[-1] == "settings"
    m = re.search(r"const TABS = \[([^\]]*)\]", js)
    assert m and re.findall(r'"([a-z]+)"', m.group(1)) == names


def test_the_feed_is_announced_politely(tags: _Tags) -> None:
    feed = next(a for _, a in tags.tags if a.get("id") == "feed")
    assert feed.get("aria-live") == "polite"


def test_the_page_hides_its_animations_when_hidden(js: str, css: str) -> None:
    assert re.search(r"""classList\.toggle\(\s*["']is-hidden["'],\s*document\.hidden\s*\)""", js)
    assert re.search(r"\.is-hidden \*[^{]*\{\s*animation-play-state:\s*paused", css)


# ───────────────────────────── the stylesheet ─────────────────────────────


def test_css_imports_and_fetches_nothing(css: str) -> None:
    assert "@import" not in css
    assert "@font-face" not in css, "font-src 'self' serves no font file; a face would 404"
    for m in re.finditer(r"url\(\s*([^)]*)\)", css):
        assert m.group(1).strip("'\" ").startswith("data:"), f"url() fetches: {m.group(0)}"


def test_css_honours_reduced_motion(css: str) -> None:
    m = re.search(r"@media\s*\(\s*prefers-reduced-motion:\s*reduce\s*\)\s*\{", css)
    assert m, "no prefers-reduced-motion block"
    body = css[m.end() : m.end() + 600]
    assert re.search(r"animation:\s*none", body)


def _keyframes(css: str) -> dict[str, str]:
    """Each @keyframes block's body, found by brace matching (blocks nest one level)."""
    out: dict[str, str] = {}
    for m in re.finditer(r"@keyframes\s+([\w-]+)\s*\{", css):
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        out[m.group(1)] = css[m.end() : i - 1]
    return out


def test_keyframes_animate_only_transform_and_opacity(css: str) -> None:
    frames = _keyframes(css)
    assert frames, "no keyframes found; the parser in this test has drifted"
    for name, body in frames.items():
        props = set(re.findall(r"([a-z-]+)\s*:", body))
        assert props <= CHEAP, f"@keyframes {name} animates {sorted(props - CHEAP)}"


def test_transitions_name_only_transform_and_opacity(css: str) -> None:
    for m in re.finditer(r"(?<![\w-])transition\s*:\s*([^;]+);", css):
        value = m.group(1).strip()
        if value == "none !important":
            continue
        for part in value.split(","):
            prop = part.split()[0]
            assert prop in CHEAP, f"transition on {prop!r} repaints: {m.group(0)}"


def test_every_animation_names_a_keyframe_that_exists(css: str) -> None:
    frames = set(_keyframes(css))
    used = set()
    for m in re.finditer(r"(?<![\w-])animation\s*:\s*([^;]+);", css):
        if m.group(1).strip().startswith("none"):
            continue
        used.add(m.group(1).split()[0])
    assert used, "no animations; the orb is supposed to move"
    assert used <= frames, f"animation names with no @keyframes: {sorted(used - frames)}"


# ───────────────────────────── inside the app: no terminal ─────────────────────────────


class _TextScopes(HTMLParser):
    """Each text node of index.html, with whether an ancestor carries ``data-terminal``."""

    VOID = frozenset({"meta", "link", "input", "br", "img", "hr", "source", "col", "wbr"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []
        self.texts: list[tuple[str, bool, str]] = []  # (text, inside data-terminal, tag)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.VOID:
            return
        marked = any(k == "data-terminal" for k, _ in attrs)
        self.stack.append((tag, marked or (bool(self.stack) and self.stack[-1][1])))

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if data.strip():
            inside = bool(self.stack) and self.stack[-1][1]
            self.texts.append((data, inside, self.stack[-1][0] if self.stack else ""))


@pytest.fixture(scope="module")
def scopes(html: str) -> _TextScopes:
    parser = _TextScopes()
    parser.feed(html)
    parser.close()
    return parser


def test_every_terminal_hint_in_the_page_is_marked(scopes: _TextScopes, css: str) -> None:
    # Inside the app the CSS hides [data-terminal]; a command outside one would
    # be shown to somebody who double-clicked an icon and has no terminal.
    loose = [t.strip() for t, inside, tag in scopes.texts if "python -m" in t and not inside]
    assert loose == [], f"terminal commands not inside a [data-terminal] element: {loose}"
    assert any("python -m" in t for t, inside, _ in scopes.texts if inside), "the hints vanished"
    assert re.search(
        r'html\[data-app="true"\]\s*\[data-terminal\]\s*\{\s*display:\s*none\s*!important', css
    )


def test_the_scripts_commands_live_in_one_table_that_only_answers_outside_the_app(
    js: str,
) -> None:
    start = js.index("const TERMINAL = Object.freeze({")
    end = js.index("});", start)
    outside = js[:start] + js[end:]
    assert "python -m" not in outside, "a terminal command outside the TERMINAL table"
    assert "python -m" in js[start:end]
    # Only terminal() reads the table, and it answers "" inside the app.
    reads = [m.start() for m in re.finditer(r"\bTERMINAL\b", js)]
    fn = js.index("function terminal(name)")
    body = js[fn : js.index("}", fn)]
    assert 'app.inApp ? ""' in body
    assert all(r == start + len("const ") or fn < r < fn + len(body) for r in reads), reads


def test_app_mode_comes_from_the_snapshot_and_reaches_the_css(js: str) -> None:
    assert "setInApp(s.app === true)" in js
    assert re.search(r'document\.documentElement\.dataset\.app = on \? "true" : "false"', js)


def test_text_from_other_processes_is_stripped_of_commands_in_the_app(js: str) -> None:
    # Refusals, chat and speech "why" lines, tool results and system feed lines
    # were written for a terminal; each goes through appText() before display.
    for needle in (
        "appText(chat.why",
        "appText(sp.why",
        "appText(p.sentence",
        "appText(text,",
        "appText(typeof data.said",
    ):
        assert needle in js, needle


@pytest.mark.parametrize("name", ["_COMMANDISH", "_COMMAND_TAIL", "_STUMP"])
def test_the_page_and_the_server_strip_commands_by_the_same_rule(js: str, name: str) -> None:
    from jarvis.window import snapshot

    m = re.search(rf"const {name.lstrip('_')} =\s*/(.+?)/i;", js)
    assert m, f"{name.lstrip('_')} is not a regex literal in app.js any more"
    assert m.group(1) == getattr(snapshot, name).pattern


# ───────────────────────────── settings and onboarding ─────────────────────────────


def test_every_settable_key_has_one_control_of_the_right_kind(tags: _Tags) -> None:
    from jarvis import config

    controls = {a["data-key"]: a.get("data-kind") for _, a in tags.tags if a.get("data-key")}
    assert set(controls) == set(config.SETTABLE)
    kinds = {
        "bool": "bool",
        "float": "number",
        "int": "number",
        "tuple[str, ...]": "list",
        "str": "text",
        "str | None": "text",
    }
    for key, kind in controls.items():
        section, _, field = key.rpartition(".")
        assert kind == kinds[config._annotation(section, field)], key


def test_the_secret_rows_are_the_keys_the_service_stores(js: str) -> None:
    from jarvis.app.setup import SECRET_NAMES

    block = js[js.index("const SECRET_INFO = {") : js.index("};", js.index("const SECRET_INFO"))]
    assert re.findall(r"^\s{4}([a-z_]+):", block, flags=re.M) == list(SECRET_NAMES)


def test_key_fields_are_password_fields_that_nothing_remembers(tags: _Tags, js: str) -> None:
    key = next(a for t, a in tags.tags if a.get("id") == "ob-key")
    assert key.get("type") == "password"
    assert key.get("autocomplete") == "off" and key.get("spellcheck") == "false"
    assert 'input.type = "password";' in js
    # Read once, cleared at once.
    assert re.search(r'const value = input\.value\.trim\(\);\s*input\.value = "";', js)


def test_the_page_never_logs_and_stores_only_its_own_conveniences(js: str) -> None:
    assert not re.search(r"\bconsole\.", js), "a console call could print a pasted key"
    keys = set(re.findall(r'writeStore\("(?:local|session)Storage",\s*([A-Z_]+)', js))
    assert keys <= {"TOKEN_KEY", "SPEAK_KEY", "TAB_KEY", "ONBOARD_KEY"}, keys


def test_the_key_link_opens_in_the_browser_proper(tags: _Tags) -> None:
    links = [a for t, a in tags.tags if t == "a" and str(a.get("href", "")).startswith("http")]
    assert links, "the onboarding names where a key comes from"
    for a in links:
        assert a["href"] in NAVIGATION, a["href"]
        assert a.get("target") == "_blank"
        assert {"noopener", "noreferrer"} <= set(str(a.get("rel", "")).split())


def test_quit_is_held_like_stop_not_clicked(js: str, tags: _Tags) -> None:
    quit_btn = next(a for t, a in tags.tags if a.get("id") == "quit")
    assert "hold-btn" in quit_btn.get("class", "")
    assert re.search(r"armHold\(\s*dom\.quit,", js)
    assert re.search(r"armHold\(\s*dom\.stop,", js)
    # The quit call lives inside armQuit and nowhere else.
    start = js.index("function armQuit()")
    assert js.index("API.appQuit") > start
    assert js.count("API.appQuit") == 1


def test_onboarding_opens_on_a_first_run_and_ends_by_starting_the_desk(js: str) -> None:
    assert re.search(r"setup\.first_run && .*openOnboarding\(\"key\"\)", js)
    finish = js[js.index("async function finishOnboarding()") :]
    finish = finish[: finish.index("\n  }\n")]
    assert 'api(API.appRestart, { process: "desk" })' in finish
    # Every save during the questions defers the restart to that one at the end.
    flow = js[js.index("async function obNext()") : js.index("async function finishOnboarding()")]
    assert flow.count("restart: false") >= 5


def test_jarvis_speaks_in_his_own_manner(scopes: _TextScopes, js: str) -> None:
    said = " ".join(t for t, _, tag in scopes.texts if tag not in ("code", "kbd"))
    assert "I'm J.A.R.V.I.S." in said and "at your service" in said
    # Composed: no exclamation marks anywhere he speaks, on the page or in the script.
    assert "!" not in said
    strings = re.findall(r'"([^"\n]*)"|`([^`\n]*)`', js)
    spoken = [a or b for a, b in strings if re.search(r"[A-Za-z]{3,} [a-z]", a or b)]
    assert not [s for s in spoken if re.search(r"[A-Za-z]!", s)], "an exclamation in a sentence"
