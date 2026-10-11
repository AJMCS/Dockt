import json
from conftest import sse


def route_answer(dash):
    bodies = []

    def respond(route):
        bodies.append(json.loads(route.request.post_data))
        route.fulfill(content_type="text/event-stream", body=sse(
            {"type": "sources", "items": []},
            {"type": "done", "answer": "Priya needs the NDA by Friday.", "cited": [], "ms": 1}))

    dash.page.route("**/api/chat", respond)
    return bodies


def test_personalized_click_history_and_reload(dash):
    bodies = route_answer(dash)
    suggestions = [{"label": "Draft Priya's NDA follow-up", "prompt": "Draft Priya a follow-up about Friday's NDA."}]
    requests = []

    def respond(route):
        requests.append(json.loads(route.request.post_data))
        route.fulfill(json={"suggestions": suggestions})

    dash.page.route("**/api/chat/suggestions", respond)
    dash.open()
    dash.ask("When does Priya need the NDA?")
    button = dash.page.locator(".suggestions button").first
    button.wait_for()
    assert button.inner_text() == suggestions[0]["label"]
    assert requests[0]["messages"][-1]["content"] == "Priya needs the NDA by Friday."
    dash.page.wait_for_function("chatCache.some(c => c.messages.some(m => m.suggestions?.length))")
    dash.page.reload()
    dash.page.wait_for_selector("#chatList li[data-id]")
    dash.page.locator("#chatList li[data-id]").first.click()
    dash.page.locator(".suggestions button").first.click()
    dash.page.wait_for_function("!busy")
    assert bodies[-1]["messages"] == [
        {"role": "user", "content": "When does Priya need the NDA?"},
        {"role": "assistant", "content": "Priya needs the NDA by Friday."},
        {"role": "user", "content": suggestions[0]["prompt"]}]


def test_late_suggestions_do_not_leak_into_new_chat(dash):
    route_answer(dash)
    dash.page.route("**/api/chat/suggestions", lambda route: route.fulfill(json={"suggestions": []}))
    dash.open()
    dash.ask("NDA deadline?")
    dash.page.evaluate("""() => {
      window.originalApi = api;
      api = (path, options) => path === '/api/chat/suggestions'
        ? new Promise(resolve => window.releaseSuggestions = resolve) : originalApi(path, options);
      requestSuggestions(document.querySelector('#thread .msg.bot'));
      newChat();
      releaseSuggestions({suggestions: [{label: 'Old NDA', prompt: 'Review old NDA'}]});
    }""")
    dash.page.wait_for_timeout(100)
    assert dash.page.locator(".suggestions").count() == 0
    assert dash.js("messages.length") == 0


def test_suggestions_outage_does_not_break_answer(dash):
    route_answer(dash)
    dash.page.route("**/api/chat/suggestions", lambda route: route.fulfill(status=503, body="offline"))
    dash.open()
    dash.ask("NDA deadline?")
    assert "Friday" in dash.last_bot_text()
    assert dash.page.locator(".suggestions").count() == 0
    assert "Draft follow-up email" not in dash.page.locator("#thread").inner_text()
