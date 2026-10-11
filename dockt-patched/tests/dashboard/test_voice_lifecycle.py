from test_dashboard import MIC_STUB


def test_pending_permission_is_single_flight_and_cancel_releases_mic(dash):
    dash.page.add_init_script(MIC_STUB)
    dash.open()
    dash.page.evaluate("""() => {
      window.permissionCalls = 0;
      navigator.mediaDevices.getUserMedia = () => {
        permissionCalls++;
        return new Promise(resolve => window.allowMic = resolve);
      };
      startMic(); startMic(); newChat();
      allowMic({getTracks: () => [{stop() { window.__mic.tracksStopped++; }}]});
    }""")
    dash.page.wait_for_function("__mic.tracksStopped === 1")
    assert dash.js("permissionCalls") == 1
    assert dash.js("micState") == "idle"


def test_switching_chats_discards_pending_transcription(dash):
    dash.page.add_init_script(MIC_STUB)
    dash.open()
    dash.page.evaluate("""() => {
      const originalApi = api;
      api = (path, options) => path === '/api/transcribe'
        ? new Promise(resolve => window.finishTranscript = resolve) : originalApi(path, options);
    }""")
    dash.page.click("#mic")
    dash.page.wait_for_function("micState === 'recording'")
    dash.page.click("#mic")
    dash.page.wait_for_function("typeof finishTranscript === 'function'")
    dash.page.evaluate("newChat(); finishTranscript({text: 'Old voice question'});")
    dash.page.wait_for_timeout(100)
    assert dash.page.input_value("#q") == ""
    assert dash.js("micState") == "idle"
