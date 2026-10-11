"""
Dashboard regression tests (Lane 2). Every test runs in a fresh browser context (empty
localStorage) against the isolated mock stack from conftest.py, and fails on any uncaught
JS error or any request that leaves 127.0.0.1.

    python -m pytest tests/dashboard -q
"""
import json

import pytest
from conftest import Dash, sse

FRIENDLY_CHAT_ERROR = "Couldn't get an answer just now"


def chat_bodies(dash: Dash) -> list:
    bodies = []
    dash.page.on("request", lambda r: r.url.endswith("/api/chat") and bodies.append(json.loads(r.post_data)))
    return bodies


def route_chat(dash: Dash, *events, delay_ms: int = 0):
    def handle(route):
        if delay_ms:
            dash.page.wait_for_timeout(delay_ms)
        route.fulfill(status=200, headers={"content-type": "text/event-stream"}, body=sse(*events))
    dash.page.route("**/api/chat", handle)


def assert_cited_first(dash: Dash, ids: list[str]):
    """Cited sources come first (in search-rank order, not citation order) and are highlighted."""
    shown = source_ids(dash)
    assert sorted(shown[:len(ids)]) == sorted(i + "*" for i in ids), shown
    assert not any(s.endswith("*") for s in shown[len(ids):]), shown


def chips(dash: Dash, scope: str = "#thread .msg.bot") -> list[str]:
    return dash.page.locator(f"{scope} .cite").evaluate_all("els => els.map(e => e.dataset.id)")


def cited(dash: Dash) -> list[str]:
    """Ids the last answer cited, as the agent returned them (the mock picks different ones per index)."""
    return [str(i) for i in dash.js("messages.findLast(m => m.role === 'assistant' && !m.draft).cited")]


def source_ids(dash: Dash) -> list[str]:
    return dash.page.locator("#sources .card").evaluate_all(
        "els => els.map(e => e.dataset.id + (e.classList.contains('cited') ? '*' : ''))")


# ---------------------------------------------------------------- page shell
def test_page_loads_clean(dash):
    dash.open()
    assert dash.page.title() == "Dockt"
    assert dash.page.inner_text("#localBadge") == "● 100% on-device"
    assert dash.js("document.activeElement.id") == "q"
    assert dash.page.inner_text("#thread").strip() == ""
    assert "show up here" in dash.page.inner_text("#sources")
    assert "Error:" not in dash.page.inner_text("body")


def test_removed_features_stay_removed(dash):
    """Ask/Search toggle, header stats, latest-captures grid and the welcome line were removed on purpose."""
    dash.open()
    for sel in ["#mAsk", "#mSearch", "#stats", "#grid", "#gridTitle"]:
        assert dash.page.locator(sel).count() == 0, sel
    body = dash.page.inner_text("body")
    assert "Ask about anything that was on your screen" not in body
    assert "pulled from your screens" not in body


def test_device_picker_lists_devices(dash):
    dash.open()
    assert dash.page.locator("#dev option").all_inner_texts() == ["All devices", "replay"]


# ---------------------------------------------------------------- chat
def test_chat_streams_tokens_then_cites(dash):
    dash.open()
    dash.js("""window.__snaps = [];
      new MutationObserver(() => { const t = document.querySelector('#thread .msg.bot:last-child .text');
        if (t) __snaps.push(t.innerText); }).observe(document.querySelector('#thread'),
        {subtree: true, childList: true, characterData: true})""")
    dash.ask("where is dinner tonight")
    final = dash.last_bot_text()
    assert final.startswith("Mock answer")
    partials = [s for s in dash.js("__snaps") if s and s != final and not s.startswith("Thinking")]
    assert partials, "answer did not stream: no intermediate text seen"
    ids = cited(dash)
    assert ids and chips(dash) == ids
    assert_cited_first(dash, ids)
    assert "moments retrieved" in dash.page.locator("#thread .msg.bot .meta").inner_text()


def test_citation_chip_rules(dash, dockt):
    """[#9], [9] and #9 become chips only for retrieved ids; **bold** renders; other #numbers stay text."""
    caps = [dockt.get(f"/api/capture/{i}") for i in (1, 2, 5)]
    answer = "Dinner at **Nopalito** [#1], see [5] and #2. Invoice #4471 is not a capture."
    route_chat(dash, {"type": "status", "text": "Searching…"}, {"type": "token", "text": answer},
               {"type": "sources", "items": caps}, {"type": "done", "answer": answer, "cited": [1, 5], "ms": 7})
    dash.open().ask("anything")
    assert chips(dash) == ["1", "5", "2"]
    html = dash.page.locator("#thread .msg.bot .text").inner_html()
    assert "<b>Nopalito</b>" in html and "#4471" in dash.last_bot_text()
    assert source_ids(dash) == ["1*", "5*", "2"]


def test_event_dispatch_status_tokens_escaping(dash):
    """status is a hint only until the first token; tokens are inserted as text, never HTML."""
    dash.open()
    a, b, c = dash.js("""(() => {
      const msg = bubble('bot', ''); const turn = {msg, body: msg.querySelector('.text'), text: '', sources: []};
      handleEvent(turn, {type: 'status', text: 'Searching your screens…'}); const a = turn.body.innerText;
      handleEvent(turn, {type: 'token', text: '<img src=x onerror=alert(1)>'}); const b = turn.body.innerHTML;
      handleEvent(turn, {type: 'status', text: 'late status'}); const c = turn.body.innerText;
      return [a, b, c]; })()""")
    assert "Searching your screens" in a
    assert b == "&lt;img src=x onerror=alert(1)&gt;"
    assert c == "<img src=x onerror=alert(1)>"


def test_follow_up_sends_full_history_role_content_only(dash):
    bodies = chat_bodies(dash)
    dash.open()
    dash.ask("where is dinner tonight")
    dash.ask("who booked it")
    msgs = bodies[1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert all(set(m) == {"role", "content"} for m in msgs), "display data leaked into the API request"
    assert bodies[1]["stream"] is True and bodies[1]["device"] is None


def test_chat_sends_selected_device(dash):
    bodies = chat_bodies(dash)
    dash.open()
    dash.page.select_option("#dev", "replay")
    dash.ask("where is dinner tonight")
    assert bodies[0]["device"] == "replay"


def test_enter_sends_shift_enter_adds_line(dash):
    dash.open()
    dash.page.click("#q")
    dash.page.keyboard.type("first line")
    dash.page.keyboard.press("Shift+Enter")
    dash.page.keyboard.type("second line")
    assert dash.page.input_value("#q") == "first line\nsecond line"
    assert dash.js("messages.length") == 0
    dash.page.keyboard.press("Enter")
    dash.page.wait_for_function("!busy && messages.length === 2")
    assert dash.page.input_value("#q") == ""


@pytest.mark.parametrize("failure", ["error_event", "network", "no_done"])
def test_chat_failures_are_friendly_and_roll_back(dash, failure):
    if failure == "error_event":
        route_chat(dash, {"type": "status", "text": "…"}, {"type": "error", "text": "Traceback: httpx.ConnectError(...)"})
    elif failure == "no_done":
        route_chat(dash, {"type": "token", "text": "half an ans"})
    else:
        dash.page.route("**/api/chat", lambda r: r.abort())
    dash.open().ask("where is dinner tonight")
    text = dash.last_bot_text()
    assert FRIENDLY_CHAT_ERROR in text
    assert "Traceback" not in text and "Error:" not in text
    assert dash.js("messages.length") == 0, "failed question should not stay in history"
    assert dash.page.is_enabled("#go")


def test_send_disabled_and_new_chat_blocked_while_busy(dash):
    route_chat(dash, {"type": "done", "answer": "ok", "cited": [], "ms": 1}, delay_ms=800)
    dash.open()
    dash.page.fill("#q", "slow one")
    dash.page.keyboard.press("Enter")
    dash.page.wait_for_function("busy")
    assert dash.page.is_disabled("#go")
    dash.page.click("#newChat")
    assert dash.js("messages.length") == 1, "New chat must not reset a conversation mid-answer"
    dash.page.wait_for_function("!busy")
    assert dash.js("messages.length") == 2


def test_new_chat_resets(dash):
    dash.open().ask("where is dinner tonight")
    dash.page.click("#newChat")
    assert dash.js("messages.length") == 0
    assert dash.page.locator("#thread .msg").count() == 0
    assert dash.page.inner_text("#sourcesTitle").lower() == "sources"
    assert "show up here" in dash.page.inner_text("#sources")


# ---------------------------------------------------------------- drafts, modal, sources
def test_draft_from_answer(dash):
    drafts = []
    dash.page.on("request", lambda r: r.url.endswith("/api/draft") and drafts.append(json.loads(r.post_data)))
    dash.open().ask("where is dinner tonight")
    dash.page.locator("#thread .msg.bot button[data-kind=message]").click()
    dash.page.wait_for_selector("#thread .msg.bot.draft .copy")
    assert drafts[0]["ids"] == [int(i) for i in cited(dash)] and drafts[0]["kind"] == "message"
    assert "where is dinner tonight" in drafts[0]["instruction"]
    last = dash.js("messages.at(-1)")
    assert last["role"] == "assistant" and last["draft"] is True


def test_draft_failure_is_friendly(dash):
    dash.page.route("**/api/draft", lambda r: r.fulfill(status=500, body="boom"))
    dash.open().ask("where is dinner tonight")
    dash.page.locator("#thread .msg.bot button[data-kind=email]").click()
    dash.page.wait_for_function("document.querySelector('#thread .msg.bot.draft .text').innerText.includes('Couldn')")
    assert "boom" not in dash.last_bot_text()


def test_chip_opens_modal_with_evidence(dash):
    dash.open().ask("where is dinner tonight")
    dash.page.locator("#thread .cite").first.click()
    dash.page.wait_for_selector("#modal", state="visible")
    dash.page.wait_for_function("document.querySelector('#mImg').naturalWidth > 0")
    assert dash.page.inner_text("#mMeta").startswith("#" + cited(dash)[0])
    assert dash.page.inner_text("#mSum").strip() and dash.page.inner_text("#mOcr").strip()
    dash.page.locator("#mActions button[data-kind=todo]").click()
    dash.page.wait_for_selector("#draft .copy")
    dash.page.keyboard.press("Escape")
    assert dash.page.locator("#modal").is_hidden()


def test_source_card_opens_modal_and_hover_highlights(dash):
    dash.open().ask("where is dinner tonight")
    target = cited(dash)[-1]
    dash.page.locator(f"#thread .cite[data-id='{target}']").hover()
    assert dash.page.locator("#sources .card.hl").get_attribute("data-id") == target
    dash.page.locator("#sources .card").nth(2).click()
    dash.page.wait_for_selector("#modal", state="visible")


# ---------------------------------------------------------------- deep links (Lane 1 depends on these)
def test_deep_link_capture_on_load_and_hashchange(dash):
    dash.open("/#/capture/3")
    dash.page.wait_for_selector("#modal", state="visible")
    assert dash.page.inner_text("#mMeta").startswith("#3")
    assert dash.js("location.hash") == "", "hash should be cleared so the same link works twice"
    dash.page.keyboard.press("Escape")
    dash.js("location.hash = '#/capture/7'")
    dash.page.wait_for_function("document.querySelector('#mMeta').textContent.startsWith('#7')")


def test_deep_link_chat_asks(dash):
    dash.open("/#/chat?q=" + "where%20is%20dinner%20tonight")
    dash.page.wait_for_function("!busy && messages.length === 2")
    assert dash.page.locator("#thread .msg.user").inner_text() == "where is dinner tonight"


def test_deep_link_to_missing_capture_does_nothing(dash):
    dash.open("/#/capture/99999")
    dash.page.wait_for_timeout(600)
    assert dash.page.locator("#modal").is_hidden()


# ---------------------------------------------------------------- things you promised
def test_promises_list(dash, dockt):
    items = dockt.get("/api/commitments?hours=48")["items"]
    dash.open()
    dash.page.wait_for_function(f"document.querySelectorAll('#promises li[data-id]').length === {len(items)}")
    open_n = sum(1 for c in items if not c["done"])
    assert f"{open_n} open" in dash.page.inner_text(".todayhead h3")
    dash.page.locator("#promises .cite").first.click()
    dash.page.wait_for_selector("#modal", state="visible")


def test_promise_checkbox_saves_and_undoes(dash, dockt):
    dash.open()
    row = dash.page.locator("#promises li[data-id]:not(.done)").first
    pid = int(row.get_attribute("data-id"))
    done = lambda: next(c["done"] for c in dockt.get("/api/commitments?hours=48&include_done=true")["items"] if c["id"] == pid)
    row.locator("input").click()
    dash.page.wait_for_function(f"document.querySelector('#promises li[data-id=\"{pid}\"]').classList.contains('done')")
    assert done() == 1
    dash.page.locator(f"#promises li[data-id='{pid}'] input").click()
    dash.page.wait_for_function(f"!document.querySelector('#promises li[data-id=\"{pid}\"]').classList.contains('done')")
    assert done() == 0


def test_promise_checkbox_reverts_when_server_fails(dash):
    dash.page.route("**/api/commitments/*/done", lambda r: r.fulfill(status=500, body="nope"))
    dash.open()
    box = dash.page.locator("#promises li[data-id]:not(.done) input").first
    box.click()
    dash.page.wait_for_timeout(400)
    assert not box.is_checked()


# ---------------------------------------------------------------- standup / timesheet
@pytest.mark.parametrize("kind,label", [("standup", "Write my standup"), ("timesheet", "Draft timesheet")])
def test_standup_and_timesheet(dash, kind, label):
    dash.page.route(f"**/api/standup?kind={kind}*", lambda r: (dash.page.wait_for_timeout(500), r.continue_()))
    dash.open()
    dash.page.click(f"button[data-standup={kind}]")
    dash.page.wait_for_function("busy")
    assert dash.page.is_disabled("#go")
    dash.page.wait_for_selector("#thread .msg.bot.draft .copy")
    assert dash.page.locator("#thread .msg.user").inner_text() == label
    assert dash.page.locator("#thread .msg.bot.draft .text b").count() > 0, "**bold** should render"
    assert [m["role"] for m in dash.js("messages")] == ["user", "assistant"]
    assert dash.page.is_enabled("#go")


def test_standup_failure_is_friendly(dash):
    dash.page.route("**/api/standup*", lambda r: r.fulfill(status=500, body="trace"))
    dash.open()
    dash.page.click("button[data-standup=standup]")
    dash.page.wait_for_function("!busy && document.querySelectorAll('#thread .msg.bot').length === 1")
    assert "Couldn't read today's activity" in dash.last_bot_text()
    assert dash.js("messages.length") == 0


# ---------------------------------------------------------------- timeline
def test_timeline_click_fills_sources_and_new_chat_clears(dash):
    dash.open()
    dash.page.wait_for_selector("#timeline .bar")
    assert [t for t in dash.page.locator(".tlaxis span").all_inner_texts() if t], "hour labels missing"
    dash.page.locator("#timeline .bar").first.click()
    dash.page.wait_for_function("document.querySelector('#sourcesTitle').textContent.includes('–')")
    assert dash.page.locator("#sources .card").count() > 0
    assert dash.page.locator("#timeline .bar.sel").count() == 1
    dash.js("refreshTimeline()")
    dash.page.wait_for_timeout(400)
    assert dash.page.locator("#timeline .bar.sel").count() == 1, "selection lost on refresh"
    dash.page.click("#newChat")
    assert dash.page.inner_text("#sourcesTitle").lower() == "sources"


# ---------------------------------------------------------------- recent chats (sidebar)
def chat_titles(dash):
    return dash.page.locator("#chatList li[data-id] .ctitle").all_inner_texts()


def test_recent_chats_lifecycle(dash):
    dash.open()
    assert dash.page.inner_text("#chatList") == "No chats yet."
    dash.ask("where is dinner tonight")
    dash.page.reload()
    dash.page.wait_for_selector("#chatList li[data-id]")
    assert chat_titles(dash) == ["where is dinner tonight"]
    dash.page.click("#newChat")
    dash.ask("find invoice 4471")
    assert chat_titles(dash) == ["find invoice 4471", "where is dinner tonight"]
    assert dash.page.inner_text("#chatList li.on .ctitle") == "find invoice 4471"
    # reopen the older chat: answer, chips and sources come back
    dash.page.locator("#chatList li", has_text="where is dinner").click()
    ids = cited(dash)
    assert ids and chips(dash) == ids
    assert_cited_first(dash, ids)
    # continuing it moves it to the top instead of creating a new entry
    dash.ask("who booked it")
    assert chat_titles(dash) == ["where is dinner tonight", "find invoice 4471"]
    assert dash.js("messages.length") == 4
    # delete the other one
    other = dash.page.locator("#chatList li", has_text="invoice")
    other.hover()
    other.locator(".del").click()
    assert chat_titles(dash) == ["where is dinner tonight"]


def test_saved_chats_are_small_and_local(dash):
    dash.open().ask("where is dinner tonight")
    raw = dash.js("localStorage.getItem('dockt-chats')")
    saved = json.loads(raw)
    assert len(saved) == 1 and saved[0]["messages"][1]["sources"]
    assert '"ocr"' not in raw, "screen text should not be stored in chat history"


def test_saved_chats_open_when_gx10_is_down(dash):
    dash.open().ask("where is dinner tonight")
    dash.page.route("**/api/chat", lambda r: r.abort())
    dash.page.route("**/api/health", lambda r: r.fulfill(
        status=200, content_type="application/json", body='{"models":{"vlm":false,"embed":false,"llm":false}}'))
    dash.page.reload()
    dash.page.wait_for_selector("#chatList li[data-id]")
    dash.page.locator("#chatList li[data-id]").first.click()
    assert chips(dash) and chips(dash) == cited(dash)
    dash.page.wait_for_function("[...document.querySelectorAll('#sources img')].some(i => i.naturalWidth > 0)")
    dash.ask("and who booked it")
    assert FRIENDLY_CHAT_ERROR in dash.last_bot_text()
    assert dash.js("messages.length") == 2


# ---------------------------------------------------------------- on-device badge
def test_badge_reflects_gx10_health(dash):
    dash.open()
    assert dash.page.inner_text("#localBadge") == "● 100% on-device"
    dash.page.route("**/api/health", lambda r: r.fulfill(
        status=200, content_type="application/json", body='{"models":{"vlm":true,"embed":false,"llm":true}}'))
    dash.js("refreshHealth()")
    dash.page.wait_for_function("document.querySelector('#localBadge').textContent.includes('GX10 unreachable')")
    assert "embed" in dash.page.get_attribute("#localBadge", "title")
    assert dash.js("localBadge.classList.contains('down')")
    dash.page.unroute("**/api/health")
    dash.page.route("**/api/health", lambda r: r.abort())
    dash.js("refreshHealth()")
    dash.page.wait_for_function("document.querySelector('#localBadge').textContent.includes('service offline')")


# ---------------------------------------------------------------- theme, contrast, layout
BG = {"light": "rgb(250, 249, 245)", "dark": "rgb(38, 38, 36)"}


@pytest.mark.parametrize("context", ["light", "dark"], indirect=True)
def test_theme_follows_system(dash, request):
    dash.open()
    assert dash.js("getComputedStyle(document.body).backgroundColor") == BG[request.node.callspec.params["context"]]


def test_theme_toggle_persists(dash):
    dash.open()
    dash.page.click("#themeToggle")
    assert dash.js("getComputedStyle(document.body).backgroundColor") == BG["dark"]
    dash.page.reload()
    assert dash.js("document.documentElement.dataset.theme") == "dark"
    assert dash.js("getComputedStyle(document.body).backgroundColor") == BG["dark"]


CONTRAST_JS = """(sels) => {
  const rgba = c => { const m = c.match(/[\\d.]+/g).map(Number); return [m[0], m[1], m[2], m[3] ?? 1]; };
  const lum = ([r, g, b]) => [r, g, b].map(v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; })
                                      .reduce((s, v, i) => s + v * [.2126, .7152, .0722][i], 0);
  const over = (top, under) => top.slice(0, 3).map((v, i) => v * top[3] + under[i] * (1 - top[3]));
  const bgOf = el => { const layers = [];
    for (; el; el = el.parentElement) { const c = rgba(getComputedStyle(el).backgroundColor);
      if (c[3] > 0) { layers.push(c); if (c[3] === 1) break; } }
    return layers.reduceRight((under, top) => over(top, under), [255, 255, 255]); };
  const out = {};
  for (const s of sels) {
    const el = [...document.querySelectorAll(s)].find(e => e.innerText.trim());
    if (!el) { out[s] = null; continue; }
    const a = lum(rgba(getComputedStyle(el).color)), b = lum(bgOf(el));
    out[s] = (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
  }
  return out; }"""
TEXT = ["#thread .msg.bot .text", "#thread .msg.bot .meta", "#thread .msg.user .text", "#thread .cite", "#localBadge",
        ".kind.deadline", ".ptext", "#promises li > .meta", ".tlaxis span", "#sources .card .sum", "#sources .card .meta",
        "#chatList .ctitle", ".sb-foot span", "#dev"]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_text_contrast_meets_aa(dash, theme):
    dash.open()
    dash.js(f"document.documentElement.dataset.theme = '{theme}'")
    dash.ask("where is dinner tonight")
    ratios = dash.page.evaluate(CONTRAST_JS, TEXT)
    missing = [s for s, r in ratios.items() if r is None]
    low = {s: round(r, 2) for s, r in ratios.items() if r is not None and r < 4.5}
    assert not missing, f"elements not found: {missing}"
    assert not low, f"below 4.5:1 in {theme}: {low}"


@pytest.mark.parametrize("width,height", [(1366, 768), (1920, 1080), (900, 700), (400, 820)])
def test_no_horizontal_overflow(browser, dockt, width, height):
    ctx = browser.new_context(viewport={"width": width, "height": height})
    try:
        d = Dash(ctx.new_page(), dockt.base).open()
        d.ask("where is dinner tonight")
        d.page.locator("#thread .msg.bot button[data-kind=email]").click()
        d.page.wait_for_selector("#thread .msg.bot.draft .copy")
        assert not d.js("document.documentElement.scrollWidth > innerWidth"), f"horizontal scroll at {width}px"
        assert not d.errors
    finally:
        ctx.close()


def test_first_screen_shows_promises_at_1366x768(dash):
    dash.open().ask("where is dinner tonight")
    dash.js("scrollTo(0, 0)")
    top = dash.page.locator(".todayhead").bounding_box()["y"]
    assert top < 768, f"'Things you promised' starts below the fold ({top:.0f}px)"


@pytest.mark.viewport(400, 820)
def test_phone_sidebar_drawer(dash):
    dash.open().ask("where is dinner tonight")
    sidebar_x = lambda: dash.page.locator("#sidebar").bounding_box()["x"]
    assert sidebar_x() < 0
    dash.page.click("#menuBtn")
    dash.page.wait_for_timeout(300)
    assert sidebar_x() >= 0
    dash.page.click("#scrim", position={"x": 380, "y": 400})
    dash.page.wait_for_timeout(300)
    assert sidebar_x() < 0
    dash.page.click("#menuBtn")
    dash.page.locator("#chatList li[data-id]").first.click()
    assert not dash.js("document.body.classList.contains('sb-open')")
