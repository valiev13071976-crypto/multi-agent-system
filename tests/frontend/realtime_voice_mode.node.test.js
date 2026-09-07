/** Executable (not just static-regex) proof of the ChatGPT-voice-mode-parity
 * defect closure in static/panda/js/realtime.js: continuous listen ->
 * auto-commit -> auto-resume-listening loop, VAD-driven auto barge-in,
 * mute != exit, and silent recovery from a recoverable in-turn error.
 *
 * Runs under plain Node (no browser/DOM, no real microphone hardware --
 * none is available in this environment) by stubbing the small surface of
 * Web APIs realtime.js touches (MediaRecorder/getUserMedia/WebSocket/
 * AudioContext) and driving the class's real methods directly. This is the
 * strongest local proof available for the VAD math and state transitions
 * without a live browser + real mic + live paid STT/TTS provider.
 *
 * Invoked by tests/test_block4_realtime_voice_mode.py via `node --test`.
 */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

function defineGlobal(name, value) {
  // Node >= 21 pre-defines navigator/crypto as getter-only globals -- a
  // plain assignment throws, so redefine the property instead.
  Object.defineProperty(global, name, { value, configurable: true, writable: true });
}

function loadRealtimeModule() {
  global.window = global;
  defineGlobal("location", { protocol: "https:", host: "panda.test" });
  // global.crypto.randomUUID already exists natively in this Node runtime
  // (Node >= 19) -- not stubbed, just reused.
  global.URLSearchParams = URLSearchParams;
  global.Blob = class Blob {};
  global.URL = { createObjectURL: () => "blob:fake" };
  global.WebSocket = class FakeWebSocket {
    constructor() {
      this.readyState = 1;
      this.sent = [];
    }
    send(data) {
      this.sent.push(data);
    }
    close() {
      this.readyState = 3;
    }
  };
  global.WebSocket.OPEN = 1;
  class FakeMediaRecorder {
    constructor(stream, opts) {
      this.stream = stream;
      this.opts = opts;
      this.state = "inactive";
      FakeMediaRecorder.instances.push(this);
    }
    start() {
      this.state = "recording";
      FakeMediaRecorder.startCount += 1;
    }
    stop() {
      this.state = "inactive";
      FakeMediaRecorder.stopCount += 1;
    }
  }
  FakeMediaRecorder.isTypeSupported = () => false;
  FakeMediaRecorder.instances = [];
  FakeMediaRecorder.startCount = 0;
  FakeMediaRecorder.stopCount = 0;
  global.MediaRecorder = FakeMediaRecorder;
  defineGlobal("navigator", {
    mediaDevices: {
      getUserMedia: async () => ({
        getTracks: () => [],
        getAudioTracks: () => fakeTracks,
      }),
    },
  });
  global.AudioContext = class FakeAudioContext {
    createMediaStreamSource() {
      return { connect: () => {} };
    }
    createAnalyser() {
      return { fftSize: 512, getByteTimeDomainData: () => {} };
    }
    close() {}
  };

  const fakeTracks = [{ enabled: true }];

  const src = fs.readFileSync(
    path.join(__dirname, "..", "..", "static", "panda", "js", "realtime.js"),
    "utf8"
  );
  // eslint-disable-next-line no-new-func
  new Function(src)();
  return { PandaRealtime: global.PandaRealtime, FakeMediaRecorder, fakeTracks };
}

function silentAnalyser() {
  return { getByteTimeDomainData: (buf) => buf.fill(128) };
}
function loudAnalyser() {
  // RMS well above VAD_RMS_THRESHOLD (0.02): alternating 0/255 -> deviation 1.0
  return {
    getByteTimeDomainData: (buf) => {
      for (let i = 0; i < buf.length; i++) buf[i] = i % 2 === 0 ? 0 : 255;
    },
  };
}

test("continuous loop: assistant.audio.completed auto-resumes listening and restarts recording", async () => {
  const { PandaRealtime, FakeMediaRecorder } = loadRealtimeModule();
  const controller = new PandaRealtime.RealtimeVoiceController({});
  controller.mediaStream = { getTracks: () => [], getAudioTracks: () => [{ enabled: true }] };
  controller.ws = { readyState: 1, send: () => {} };
  controller._setState(PandaRealtime.STATE_SPEAKING);
  const startCountBefore = FakeMediaRecorder.startCount;

  controller._onTextFrame(
    JSON.stringify({ kind: "event", type: "assistant.audio.completed", turn_id: "t1", data: {} })
  );

  assert.equal(controller.state, PandaRealtime.STATE_LISTENING, "auto-returns to LISTENING");
  assert.ok(
    FakeMediaRecorder.startCount > startCountBefore,
    "recording restarts automatically -- no button press required for the next turn"
  );
});

test("VAD auto-commits after sustained silence following real detected speech", async () => {
  const { PandaRealtime } = loadRealtimeModule();
  const sent = [];
  const controller = new PandaRealtime.RealtimeVoiceController({});
  controller._send = (payload) => sent.push(payload);
  controller._stopRecording = () => {};
  controller._setState(PandaRealtime.STATE_LISTENING);

  let now = 1000;
  const realNow = Date.now;
  Date.now = () => now;
  try {
    controller._analyser = loudAnalyser();
    controller._vadBuf = new Uint8Array(4);
    controller._sampleVad();
    assert.equal(controller._sawSpeechThisTurn, true, "real speech energy was detected");

    controller._analyser = silentAnalyser();
    controller._sampleVad(); // silence starts
    assert.equal(sent.length, 0, "does not commit on the very first silent sample");

    now += 1000; // exceeds VAD_SILENCE_COMMIT_MS (900ms)
    controller._sampleVad();
    assert.equal(sent.length, 1, "auto-commits exactly once after sustained silence");
    assert.equal(sent[0].type, "audio.commit");
  } finally {
    Date.now = realNow;
  }
});

test("VAD does not auto-commit on silence alone (no speech ever detected this turn)", async () => {
  const { PandaRealtime } = loadRealtimeModule();
  const sent = [];
  const controller = new PandaRealtime.RealtimeVoiceController({});
  controller._send = (payload) => sent.push(payload);
  controller._setState(PandaRealtime.STATE_LISTENING);
  controller._analyser = silentAnalyser();
  controller._vadBuf = new Uint8Array(4);

  let now = 2000;
  const realNow = Date.now;
  Date.now = () => now;
  try {
    for (let i = 0; i < 20; i++) {
      now += 100;
      controller._sampleVad();
    }
    assert.equal(sent.length, 0, "never fabricates a committed turn from ambient silence alone");
  } finally {
    Date.now = realNow;
  }
});

test("VAD auto-barge-in fires after sustained speech while Panda is speaking", async () => {
  const { PandaRealtime } = loadRealtimeModule();
  const sent = [];
  const controller = new PandaRealtime.RealtimeVoiceController({});
  controller._send = (payload) => sent.push(payload);
  controller._startRecording = () => {};
  controller.mediaStream = { getTracks: () => [] };
  controller._setState(PandaRealtime.STATE_SPEAKING);
  controller._analyser = loudAnalyser();
  controller._vadBuf = new Uint8Array(4);

  let now = 5000;
  const realNow = Date.now;
  Date.now = () => now;
  try {
    controller._sampleVad(); // speech onset
    assert.equal(sent.length, 0, "requires SUSTAINED speech, not a single sample");
    now += 250; // exceeds VAD_BARGE_IN_MS (180ms)
    controller._sampleVad();
    assert.equal(sent.length, 1);
    assert.equal(sent[0].type, "barge_in");
    assert.equal(controller.state, PandaRealtime.STATE_LISTENING, "barge-in returns to LISTENING immediately");
  } finally {
    Date.now = realNow;
  }
});

test("mute pauses capture without ending the session; unmute resumes listening", async () => {
  const { PandaRealtime, FakeMediaRecorder, fakeTracks } = loadRealtimeModule();
  const controller = new PandaRealtime.RealtimeVoiceController({});
  controller.mediaStream = { getTracks: () => [], getAudioTracks: () => fakeTracks };
  controller.ws = { readyState: 1, send: () => {} };
  controller._setState(PandaRealtime.STATE_LISTENING);
  controller._startRecording();
  assert.equal(controller.recorder.state, "recording");

  controller.mute();
  assert.equal(controller.isMuted, true);
  assert.equal(fakeTracks[0].enabled, false, "the actual mic track is disabled, not just ignored client-side");
  assert.equal(controller.state, PandaRealtime.STATE_LISTENING, "mute is NOT session termination");

  // Muted VAD must never auto-commit/auto-barge-in.
  controller._analyser = loudAnalyser();
  controller._vadBuf = new Uint8Array(4);
  const sent = [];
  controller._send = (payload) => sent.push(payload);
  controller._sampleVad();
  assert.equal(sent.length, 0, "VAD is inert while muted");

  controller.unmute();
  assert.equal(controller.isMuted, false);
  assert.equal(fakeTracks[0].enabled, true);
  assert.equal(controller.recorder.state, "recording", "unmute resumes capture for the current turn");
});

test("close() fully tears down: mic tracks stopped, recorder stopped, transport closed, state IDLE", async () => {
  const { PandaRealtime } = loadRealtimeModule();
  const stoppedTracks = [];
  const controller = new PandaRealtime.RealtimeVoiceController({});
  controller.mediaStream = {
    getTracks: () => [{ stop: () => stoppedTracks.push("track") }],
    getAudioTracks: () => [{ enabled: true }],
  };
  controller.ws = {
    readyState: 1,
    send: () => {},
    close() {
      this.readyState = 3;
    },
  };
  controller._setState(PandaRealtime.STATE_LISTENING);
  controller._startRecording();

  controller.close();

  assert.equal(controller.state, PandaRealtime.STATE_IDLE);
  assert.equal(controller.mediaStream, null, "mic MediaStream reference released");
  assert.equal(stoppedTracks.length, 1, "every mic track explicitly stopped");
  assert.equal(controller.ws, null, "transport reference released");
});

test("start() transitions CONNECTING -> ERROR -> IDLE and surfaces mic_permission_denied when getUserMedia rejects", async () => {
  const { PandaRealtime } = loadRealtimeModule();
  // Simulate a sandboxed environment with no real microphone hardware:
  // getUserMedia rejects immediately (no permission prompt possible).
  global.navigator.mediaDevices.getUserMedia = async () => {
    throw new Error("Requested device not found");
  };
  const states = [];
  const errors = [];
  const controller = new PandaRealtime.RealtimeVoiceController({
    onStateChange: (s) => states.push(s),
    onError: (e) => errors.push(e),
  });

  await controller.start({});

  assert.deepEqual(
    states,
    [PandaRealtime.STATE_CONNECTING, PandaRealtime.STATE_ERROR, PandaRealtime.STATE_IDLE],
    "voice mode surface is shown (CONNECTING) even though it reverts immediately on permission denial -- " +
      "this exact transient is what a screenshot-based manual check can miss due to timing, proven here " +
      "deterministically instead"
  );
  assert.equal(errors.length, 1);
  assert.equal(errors[0].code, "mic_permission_denied");
  assert.equal(controller.state, PandaRealtime.STATE_IDLE, "cleanly reverts to the ordinary text composer");
});

test("a recoverable in-turn error (e.g. empty audio buffer) resumes listening instead of stalling", async () => {
  const { PandaRealtime, FakeMediaRecorder } = loadRealtimeModule();
  const controller = new PandaRealtime.RealtimeVoiceController({});
  controller.mediaStream = { getTracks: () => [], getAudioTracks: () => [{ enabled: true }] };
  controller.ws = { readyState: 1, send: () => {} };
  controller._setState(PandaRealtime.STATE_LISTENING);
  controller.commit(); // stops recording, sends audio.commit -- local state stays LISTENING
  const startCountBefore = FakeMediaRecorder.startCount;

  controller._onTextFrame(
    JSON.stringify({
      kind: "event",
      type: "error",
      data: { code: "rt_audio_empty", message: "rt_audio_empty" },
    })
  );

  assert.equal(controller.state, PandaRealtime.STATE_LISTENING, "never gets stuck in a dead turn");
  assert.ok(FakeMediaRecorder.startCount > startCountBefore, "microphone capture resumes automatically");
});
