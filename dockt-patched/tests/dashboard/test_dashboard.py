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


def open_pane(dash: Dash, name: str):
    """Open Promises / Timeline (or Sources on narrow windows) from the top-right buttons."""
    dash.page.click(f".tab[data-pane={name}]")
    dash.page.wait_for_selector(f"#pane [data-view={name}]", state="visible")


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
    assert dash.page.inner_text("#localBadge") == "● Powered by local AI"
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
    assert "100% on-device" not in body and "Stays on this laptop" not in body


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
    strip = dash.page.locator("#thread .msg.bot .srcs .src")
    assert strip.count() > 0, "every answer shows its sources under it"
    assert strip.evaluate_all("els => els.map(e => e.dataset.id)") == ids, "only the cited sources, in citation order"
    assert " ms" not in dash.last_bot_text() and "moments retrieved" not in dash.page.inner_text("#thread"), "no timing in responses"
    strip.first.click()
    dash.page.wait_for_selector("#modal", state="visible")
    assert dash.page.inner_text("#mMeta").startswith("#" + ids[0])


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
    draft_srcs = dash.page.locator("#thread .msg.bot.draft .srcs .src").evaluate_all("els => els.map(e => e.dataset.id)")
    assert draft_srcs == cited(dash), "a draft lists the cited screenshots it was written from"


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


@pytest.mark.viewport(1440, 900)
def test_screenshot_window_resizes_maximizes_and_sits_above_the_sidebar(dash):
    dash.open().ask("where is dinner tonight")
    dash.page.locator("#thread .cite").first.click()
    dash.page.wait_for_selector("#modal", state="visible")
    box = lambda: dash.page.locator("#modal .box").bounding_box()
    # nothing (the sidebar especially) covers the screenshot's left edge
    b = box()
    assert dash.page.evaluate(f"document.elementFromPoint({b['x'] + 20}, {b['y'] + 60}).closest('#modal') !== null")

    def drag(sel, dx, dy):
        g = dash.page.locator(sel).bounding_box()
        x, y = g["x"] + g["width"] / 2, g["y"] + g["height"] / 2
        dash.page.mouse.move(x, y)
        dash.page.mouse.down()
        dash.page.mouse.move(x + dx, y + dy, steps=6)
        dash.page.mouse.up()

    drag('.m-rs[data-dir="se"]', -150, -100)
    small = box()
    assert small["width"] <= b["width"] - 280 and abs(small["height"] - (b["height"] - 100)) < 4
    assert dash.page.locator("#modal").is_visible(), "a drag that ends over the backdrop must not close the window"
    shot_w = lambda: dash.page.locator("#modal .shot").bounding_box()["width"]
    before = shot_w()
    drag(".m-split", -120, 0)
    assert shot_w() < before - 100, "the divider moves the split between screenshot and details"
    dash.page.click("#mMax")
    assert box()["width"] > 1300 and box()["height"] > 750
    dash.page.click("#mMax")
    assert abs(box()["width"] - small["width"]) < 2, "restore goes back to the dragged size"
    dash.page.click("#mClose")
    assert dash.page.locator("#modal").is_hidden()
    dash.page.locator("#thread .cite").first.click()
    dash.page.wait_for_selector("#modal", state="visible")
    assert abs(box()["width"] - small["width"]) < 2, "the size is remembered"
    dash.page.dblclick('.m-rs[data-dir="se"]')
    assert abs(box()["width"] - b["width"]) < 2, "double-click resets the size"


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
    open_n = sum(1 for c in items if not c["done"])
    dash.page.wait_for_function(f"document.querySelector('#promCount').textContent === '{open_n}'")
    open_pane(dash, "promises")
    dash.page.wait_for_function(f"document.querySelectorAll('#promises li[data-id]').length === {len(items)}")
    assert f"{open_n} open" in dash.page.inner_text("#pane [data-view=promises] h3")
    dash.page.locator("#promises .cite").first.click()
    dash.page.wait_for_selector("#modal", state="visible")


def test_promise_checkbox_saves_and_undoes(dash, dockt):
    dash.open()
    open_pane(dash, "promises")
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
    open_pane(dash, "promises")
    box = dash.page.locator("#promises li[data-id]:not(.done) input").first
    box.click()
    dash.page.wait_for_timeout(400)
    assert not box.is_checked()


# ---------------------------------------------------------------- standup / timesheet
@pytest.mark.parametrize("kind,label", [("standup", "Write my standup"), ("timesheet", "Draft timesheet")])
def test_standup_and_timesheet(dash, kind, label):
    dash.page.route(f"**/api/standup?kind={kind}*", lambda r: (dash.page.wait_for_timeout(500), r.continue_()))
    dash.open()
    open_pane(dash, "promises")
    dash.page.click(f"button[data-standup={kind}]")
    dash.page.wait_for_function("busy")
    assert dash.page.is_disabled("#go")
    dash.page.wait_for_selector("#thread .msg.bot.draft .copy")
    assert dash.page.locator("#thread .msg.user").inner_text() == label
    assert dash.page.locator("#thread .msg.bot.draft .text b").count() > 0, "**bold** should render"
    assert [m["role"] for m in dash.js("messages")] == ["user", "assistant"]
    assert dash.page.is_enabled("#go")
    dash.page.wait_for_selector("#thread .msg.bot.draft .srcs")
    assert dash.page.locator("#thread .msg.bot.draft .srcs .src").count() == 0, "a standup cites no screenshots"
    assert "No sources cited" in dash.page.inner_text("#thread .msg.bot.draft .srcs")
    assert " ms" not in dash.page.inner_text("#thread .msg.bot.draft")


def test_standup_failure_is_friendly(dash):
    dash.page.route("**/api/standup*", lambda r: r.fulfill(status=500, body="trace"))
    dash.open()
    open_pane(dash, "promises")
    dash.page.click("button[data-standup=standup]")
    dash.page.wait_for_function("!busy && document.querySelectorAll('#thread .msg.bot').length === 1")
    assert "Couldn't read today's activity" in dash.last_bot_text()
    assert dash.js("messages.length") == 0


# ---------------------------------------------------------------- timeline
def test_timeline_slot_lists_its_screens_and_new_chat_clears(dash):
    dash.open()
    open_pane(dash, "timeline")
    dash.page.wait_for_selector("#timeline .bar")
    assert [x for x in dash.page.locator(".tlaxis span").all_inner_texts() if x], "hour labels missing"
    dash.page.locator("#timeline .bar").first.click()
    dash.page.wait_for_function("document.querySelector('#tlTitle').textContent.includes('–')")
    assert dash.page.locator("#tlSources .card").count() > 0
    assert dash.page.locator("#timeline .bar.sel").count() == 1
    dash.js("refreshTimeline()")
    dash.page.wait_for_timeout(400)
    assert dash.page.locator("#timeline .bar.sel").count() == 1, "selection lost on refresh"
    dash.page.locator("#tlSources .card").first.click()
    dash.page.wait_for_selector("#modal", state="visible")
    dash.page.keyboard.press("Escape")
    dash.page.click("#newChat")
    assert dash.page.locator("#timeline .bar.sel").count() == 0
    assert dash.page.locator("#tlTitle").is_hidden()


# ---------------------------------------------------------------- recent chats (sidebar)
def chat_titles(dash, expect: list[str] | None = None):
    """Titles in Recents. History is saved through the local service, so the list updates a moment
    after each change: pass `expect` to wait for it (up to 5 s)."""
    titles = lambda: dash.page.locator("#chatList li[data-id] .ctitle").all_inner_texts()
    if expect is not None:
        for _ in range(50):
            if titles() == expect:
                break
            dash.page.wait_for_timeout(100)
        assert titles() == expect, f"Recents shows {titles()}, expected {expect}"
    return titles()


def test_recent_chats_lifecycle(dash):
    dash.open()
    assert dash.page.inner_text("#chatList") == "No chats yet."
    dash.ask("where is dinner tonight")
    dash.page.reload()
    assert chat_titles(dash, ["where is dinner tonight"]) == ["where is dinner tonight"]
    dash.page.click("#newChat")
    dash.ask("find invoice 4471")
    assert chat_titles(dash, ["find invoice 4471", "where is dinner tonight"])
    assert dash.page.inner_text("#chatList li.on .ctitle") == "find invoice 4471"
    # reopen the older chat: answer, chips and sources come back
    dash.page.locator("#chatList li", has_text="where is dinner").click()
    ids = cited(dash)
    assert ids and chips(dash) == ids
    assert_cited_first(dash, ids)
    # continuing it moves it to the top instead of creating a new entry
    dash.ask("who booked it")
    assert chat_titles(dash, ["where is dinner tonight", "find invoice 4471"])
    assert dash.js("messages.length") == 4
    # delete the other one
    other = dash.page.locator("#chatList li", has_text="invoice")
    other.hover()
    other.locator(".del").click()
    assert chat_titles(dash, ["where is dinner tonight"])


def test_chats_saved_in_local_store_shared_with_pet(dash, dockt):
    """History lives in the local service (shared with Lane 1's quick-chat pet), not in browser storage."""
    dash.open().ask("where is dinner tonight")
    dash.page.wait_for_function("document.querySelectorAll('#chatList li[data-id]').length === 1")
    saved = dockt.get("/api/conversations")
    assert [c["title"] for c in saved] == ["where is dinner tonight"]
    assert [m["role"] for m in saved[0]["messages"]] == ["user", "assistant"]
    assert dash.js("localStorage.getItem('dockt-chats')") is None


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
    assert dash.page.inner_text("#localBadge") == "● Powered by local AI"
    dash.page.route("**/api/health", lambda r: r.fulfill(
        status=200, content_type="application/json", body='{"models":{"vlm":true,"embed":false,"llm":true}}'))
    dash.js("refreshHealth()")
    dash.page.wait_for_function("document.querySelector('#localBadge').textContent.includes('Local AI unreachable')")
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


@pytest.mark.parametrize("context", ["dark"], indirect=True)
def test_opening_the_dashboard_saves_no_theme(dash, dockt):
    dash.open()
    dash.page.wait_for_timeout(300)   # let the theme sync finish
    assert dockt.get("/api/theme")["theme"] is None, "only the toggle may save a theme; otherwise follow the system"
    assert dash.js("document.documentElement.dataset.theme") is None
    dash.page.click("#themeToggle")
    dash.page.wait_for_function("fetch('/api/theme').then(r => r.json()).then(j => j.theme === 'light')")
    assert (dockt.data / "theme").read_text() == "light", "the choice is saved in this service's DATA_DIR"


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
TEXT = {
    "sources": ["#thread .msg.bot .text", "#thread .msg.bot .src", "#thread .msg.user .text", "#thread .cite", "#localBadge",
                "#sources .card .sum", "#sources .card .meta", "#chatList .ctitle", "#dev", ".tab"],
    "promises": [".kind.deadline", ".ptext", "#promises li > .meta", "#promCount", "#promHint"],
    "timeline": [".tlaxis span"],
}


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_text_contrast_meets_aa(dash, theme):
    dash.open()
    dash.js(f"document.documentElement.dataset.theme = '{theme}'")
    dash.ask("where is dinner tonight")
    ratios = {}
    for pane, sels in TEXT.items():
        if pane != "sources":
            open_pane(dash, pane)
            dash.page.wait_for_timeout(300)
        ratios.update(dash.page.evaluate(CONTRAST_JS, sels))
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


def test_composer_pinned_to_bottom(dash):
    """The chat box sits at the bottom of the window; the conversation scrolls above it, the page never does."""
    dash.open()
    gap = lambda: 768 - (lambda b: b["y"] + b["height"])(dash.page.locator(".searchrow").bounding_box())
    assert 0 <= gap() < 40, f"composer not at the bottom when empty ({gap():.0f}px gap)"
    for q in ["where is dinner tonight", "who booked it", "find invoice 4471", "what did I do this afternoon"]:
        dash.ask(q)
    assert 0 <= gap() < 40, f"composer moved after a long chat ({gap():.0f}px gap)"
    assert dash.js("document.querySelector('#thread').scrollHeight > document.querySelector('#thread').clientHeight")
    assert not dash.js("document.documentElement.scrollHeight > innerHeight"), "the page itself should not scroll"


def test_panels_open_from_top_right_like_artifacts(dash):
    dash.open().ask("where is dinner tonight")
    pane_w = lambda: dash.page.locator("#pane").bounding_box()["width"]
    visible = lambda v: dash.page.locator(f"#pane [data-view={v}]").is_visible()
    assert visible("sources") and not dash.page.locator(".tab[data-pane=sources]").is_visible()
    narrow_w = pane_w()
    open_pane(dash, "promises")
    assert not visible("sources") and dash.page.locator(".tab[data-pane=promises]").get_attribute("class").count("on")
    dash.page.wait_for_timeout(300)
    assert pane_w() > narrow_w, "an open panel should widen like an artifact"
    dash.page.click(".tab[data-pane=promises]")               # second click closes it
    assert visible("sources")
    open_pane(dash, "timeline")
    dash.page.click("#pane [data-view=timeline] .pane-close")  # so does the x
    assert visible("sources")
    assert dash.page.locator(".tab.on").count() == 0


@pytest.mark.viewport(1440, 900)
def test_sources_panel_resizes_by_dragging_its_edge(dash):
    dash.open().ask("where is dinner tonight")
    pane_w = lambda: round(dash.page.locator("#pane").bounding_box()["width"])
    card_w = lambda: dash.page.locator("#sources .card img").first.bounding_box()["width"]
    start, thumb = pane_w(), card_w()
    g = dash.page.locator(".pane-resize").bounding_box()
    x, y = g["x"] + g["width"] / 2, g["y"] + 200
    dash.page.mouse.move(x, y)
    dash.page.mouse.down()
    dash.page.mouse.move(x - 360, y, steps=6)
    dash.page.mouse.up()
    assert pane_w() >= start + 300, "dragging the left edge should widen the panel"
    assert card_w() > thumb * 2, "a wide panel should show bigger screenshots"
    dash.page.mouse.move(x - 2000, y)   # the chat always keeps a usable column
    assert dash.page.locator(".chat").bounding_box()["width"] >= 400
    dash.page.reload()
    dash.page.wait_for_selector("#pane [data-view=sources]", state="visible")
    assert pane_w() >= start + 300, "the width is remembered"
    open_pane(dash, "promises")
    dash.page.wait_for_timeout(300)
    assert pane_w() == 440, "each panel keeps its own width"
    dash.page.click(".tab[data-pane=promises]")
    dash.page.dblclick(".pane-resize")
    dash.page.wait_for_timeout(300)
    assert pane_w() == start, "double-click resets to the default"


@pytest.mark.viewport(400, 820)
def test_phone_panels_overlay_and_close(dash):
    dash.open().ask("where is dinner tonight")
    assert not dash.js("paneVisible()"), "panel should start closed on a phone"
    open_pane(dash, "sources")
    assert dash.page.locator("#sources .card").count() > 0
    dash.page.click("#pane [data-view=sources] .pane-close")
    dash.page.wait_for_timeout(300)
    assert not dash.js("paneVisible()")
    open_pane(dash, "promises")
    dash.page.locator("#pane button[data-standup=standup]").click()
    dash.page.wait_for_selector("#thread .msg.bot.draft .copy")
    assert not dash.js("paneVisible()"), "standup answers in the chat, so the panel gets out of the way"


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


# ---------------------------------------------------------------- collapsible sidebar
def test_sidebar_collapses_to_rail_and_remembers(dash):
    dash.open().ask("where is dinner tonight")
    width = lambda: dash.page.locator("#sidebar").bounding_box()["width"]
    assert width() > 200 and dash.page.locator("#chatList").is_visible()
    dash.page.click("#sbToggle")
    dash.page.wait_for_timeout(300)
    assert width() < 80, "collapsed sidebar should be a slim rail"
    assert dash.page.locator("#chatList").is_hidden() and dash.page.locator(".sb-new .label").is_hidden()
    assert dash.page.get_attribute("#sbToggle", "aria-expanded") == "false"
    dash.page.reload()
    dash.page.wait_for_timeout(300)
    assert width() < 80, "collapsed state should survive a reload"
    dash.page.click("#newChat")                                  # icon-only New chat still works
    assert dash.js("messages.length") == 0
    dash.page.route("**/api/health", lambda r: r.fulfill(
        status=200, content_type="application/json", body='{"models":{"vlm":false,"embed":true,"llm":true}}'))
    dash.js("refreshHealth()")
    dash.page.wait_for_function("document.querySelector('#localBadge').classList.contains('down')")
    assert "rgb(" in dash.js("getComputedStyle(localBadge).backgroundColor"), "status dot shows health when collapsed"
    dash.page.click("#sbToggle")
    dash.page.wait_for_timeout(300)
    assert width() > 200 and dash.page.locator("#chatList").is_visible()


@pytest.mark.viewport(400, 820)
def test_phone_sidebar_toggle_closes_drawer(dash):
    dash.open()
    dash.page.click("#menuBtn")
    dash.page.wait_for_timeout(300)
    dash.page.click("#sbToggle")
    dash.page.wait_for_timeout(300)
    assert dash.page.locator("#sidebar").bounding_box()["x"] < 0
    assert not dash.js("document.documentElement.classList.contains('sb-collapsed')"), "phones never collapse to a rail"


# ---------- voice input ----------
# Replaces the microphone before the page loads. Recording yields a real 8 kHz WAV of silence, so the page's
# decode-and-resample path runs for real; the upload then goes through server/app.py to the mock Whisper.
MIC_STUB = """
(() => {
  window.__mic = {tracksStopped: 0, deny: false, uploads: []};
  const realFetch = window.fetch;   // note what the page uploads (Playwright can't read Blob multipart bodies)
  window.fetch = (url, opts) => {
    const file = opts && opts.body instanceof FormData && opts.body.get("audio");
    if (file) window.__mic.uploads.push({url: String(url), name: file.name, type: file.type, size: file.size});
    return realFetch(url, opts);
  };
  function silentWav() {
    const n = 2000, view = new DataView(new ArrayBuffer(44 + n * 2));
    const ascii = (at, s) => [...s].forEach((c, i) => view.setUint8(at + i, c.charCodeAt(0)));
    ascii(0, "RIFF"); view.setUint32(4, 36 + n * 2, true); ascii(8, "WAVEfmt ");
    view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
    view.setUint32(24, 8000, true); view.setUint32(28, 16000, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
    ascii(36, "data"); view.setUint32(40, n * 2, true);
    return new Blob([view], {type: "audio/wav"});
  }
  const track = {stop() { window.__mic.tracksStopped++; }};
  navigator.mediaDevices.getUserMedia = async () => {
    if (window.__mic.deny) throw new DOMException("denied", "NotAllowedError");
    return {getTracks: () => [track]};
  };
  window.MediaRecorder = class {
    constructor(stream) { this.stream = stream; this.state = "inactive"; this.mimeType = "audio/wav"; }
    start() { this.state = "recording"; }
    stop() {
      this.state = "inactive";
      setTimeout(() => { this.ondataavailable && this.ondataavailable({data: silentWav()}); this.onstop && this.onstop(); }, 10);
    }
  };
})();
"""
RECORDING = "document.querySelector('#mic').classList.contains('recording')"


def test_voice_input_transcribes_into_the_box_without_sending(dash):
    dash.page.add_init_script(MIC_STUB)
    dash.open()
    dash.page.fill("#q", "where is")
    dash.page.click("#mic")
    dash.page.wait_for_function(RECORDING)
    assert dash.page.get_attribute("#mic", "aria-pressed") == "true"
    assert "Listening" in dash.page.get_attribute("#q", "placeholder")
    dash.page.click("#mic")
    dash.page.wait_for_function("document.querySelector('#q').value.includes('mock transcript')")
    assert dash.page.input_value("#q") == "where is mock transcript"
    assert dash.js("__mic.tracksStopped") == 1, "the mic must be released after recording"
    assert dash.page.get_attribute("#mic", "aria-pressed") == "false" and dash.page.is_enabled("#mic")
    [upload] = dash.js("__mic.uploads")
    assert upload["url"] == "/api/transcribe" and upload["name"] == "speech.wav" and upload["type"] == "audio/wav"
    assert upload["size"] == 44 + 4000 * 2, "0.25 s resampled to 16 kHz mono 16-bit WAV"
    assert dash.js("messages.length") == 0, "voice input fills the box so a misheard word can be fixed; it doesn't send"


def test_voice_input_escape_cancels_without_uploading(dash):
    dash.page.add_init_script(MIC_STUB)
    uploads = []
    dash.page.on("request", lambda r: r.url.endswith("/api/transcribe") and uploads.append(r.url))
    dash.open()
    dash.page.click("#mic")
    dash.page.wait_for_function(RECORDING)
    dash.page.keyboard.press("Escape")
    dash.page.wait_for_function(f"!({RECORDING}) && __mic.tracksStopped === 1")
    dash.page.wait_for_timeout(200)
    assert uploads == [] and dash.page.input_value("#q") == ""
    assert "Ask anything" in dash.page.get_attribute("#q", "placeholder")


@pytest.mark.parametrize("failure", ["mic_denied", "not_configured", "server_error"])
def test_voice_input_failures_are_friendly(dash, failure):
    dash.page.add_init_script(MIC_STUB)
    if failure == "not_configured":   # what server/app.py answers when models.transcribe is missing
        dash.page.route("**/api/transcribe", lambda r: r.fulfill(
            status=503, content_type="application/json",
            body=json.dumps({"detail": "transcription not configured yet (models.transcribe missing)"})))
    elif failure == "server_error":   # e.g. STT_URL points at a Whisper server that isn't running
        dash.page.route("**/api/transcribe", lambda r: r.fulfill(status=500, body="Internal Server Error"))
    dash.open()
    if failure == "mic_denied":
        dash.js("__mic.deny = true")
    dash.page.click("#mic")
    if failure != "mic_denied":
        dash.page.wait_for_function(RECORDING)
        dash.page.click("#mic")
    expected = {"mic_denied": "Microphone blocked", "not_configured": "isn't set up",
                "server_error": "Couldn't transcribe"}[failure]
    dash.page.wait_for_function(f"document.querySelector('#q').placeholder.includes({json.dumps(expected)})")
    assert dash.page.input_value("#q") == "" and dash.page.is_enabled("#mic")
    assert "Error" not in dash.page.get_attribute("#q", "placeholder")
