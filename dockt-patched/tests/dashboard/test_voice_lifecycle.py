from test_dashboard import MIC_STUB
import uuid


def test_selected_microphone_is_used_by_pet_and_remembered(dash):
    dash.page.add_init_script(MIC_STUB + """
      navigator.mediaDevices.enumerateDevices = async () => [
        {kind:'audioinput', deviceId:'airpods', label:'AirPods headset'},
        {kind:'audioinput', deviceId:'usb', label:'USB microphone'}];
      const capture = navigator.mediaDevices.getUserMedia;
      navigator.mediaDevices.getUserMedia = async constraints => {
        window.__mic.constraints = constraints;
        const stream = await capture();
        stream.getAudioTracks = () => [{label:'USB microphone'}];
        return stream;
      };
    """)
    dash.open()
    dash.page.wait_for_function("document.querySelector('#micInput').options.length === 3")
    dash.page.select_option("#micInput", "usb")
    token = str(uuid.uuid4())
    dash.page.evaluate("id => petVoiceCommand('start', id)", token)
    dash.page.wait_for_function("petVoiceState.state === 'recording'")
    assert dash.js("__mic.constraints.audio.deviceId.exact") == "usb"
    assert dash.page.evaluate("id => petVoiceCommand('devices', id)", token)["input"] == "USB microphone"
    dash.page.evaluate("id => petVoiceCommand('cancel', id)", token)
    dash.page.reload()
    dash.page.wait_for_function("document.querySelector('#micInput').value === 'usb'")


def test_pet_uses_dashboard_audio_pipeline_without_changing_dashboard_draft(dash):
    dash.page.add_init_script(MIC_STUB)
    dash.open()
    dash.page.fill("#q", "Keep my dashboard draft")
    token = str(uuid.uuid4())
    dash.page.evaluate("id => petVoiceCommand('start', id)", token)
    dash.page.wait_for_function("petVoiceState.state === 'recording'")
    dash.page.evaluate("id => petVoiceCommand('stop', id)", token)
    dash.page.wait_for_function("petVoiceState.state === 'done'")
    result = dash.page.evaluate("id => petVoiceCommand('status', id)", token)
    assert result["text"] == "mock transcript"
    assert dash.page.input_value("#q") == "Keep my dashboard draft"
    assert dash.js("messages.length") == 0
    assert dash.js("__mic.uploads[0].type") == "audio/wav"


def test_pet_microphone_permission_error_returns_to_pet(dash):
    dash.page.add_init_script(MIC_STUB)
    dash.open()
    dash.page.evaluate("__mic.deny = true")
    dash.page.evaluate("id => petVoiceCommand('start', id)", str(uuid.uuid4()))
    dash.page.wait_for_function("petVoiceState.state === 'error'")
    assert "Microphone blocked" in dash.js("petVoiceState.error")


def test_pet_cancel_discards_transcript(dash):
    dash.page.add_init_script(MIC_STUB)
    dash.open()
    token = str(uuid.uuid4())
    dash.page.evaluate("id => petVoiceCommand('start', id)", token)
    dash.page.wait_for_function("petVoiceState.state === 'recording'")
    dash.page.evaluate("id => petVoiceCommand('cancel', id)", token)
    dash.page.wait_for_function("micState === 'idle'")
    assert dash.js("petVoiceState.state") == "cancelled"
    assert dash.js("__mic.uploads.length") == 0


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


def test_pet_auto_stop_transcribes_without_a_second_click(dash):
    from test_dashboard import SPEAKING_MIC
    dash.page.add_init_script(SPEAKING_MIC)
    dash.open()
    token = str(uuid.uuid4())
    dash.page.fill("#q", "Keep draft")
    dash.page.evaluate("id => petVoiceCommand('start', id)", token)
    dash.page.wait_for_function("petVoiceState.state === 'recording'")
    dash.page.wait_for_timeout(500)
    dash.js("__mic.gain.value = 0.3")
    dash.page.wait_for_timeout(900)
    dash.js("__mic.gain.value = 0")
    dash.page.wait_for_function("petVoiceState.state === 'done'", timeout=6000)
    assert dash.js("petVoiceState.text") == "mock transcript"
    assert dash.page.input_value("#q") == "Keep draft"
    assert dash.js("__mic.uploads.length") == 1
    assert dash.js("__mic.tracksStopped") >= 1
