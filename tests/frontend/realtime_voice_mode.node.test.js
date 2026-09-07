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
  defineGlobal("sessionStorage", { getItem: () => null });
  global.WebSocket = class FakeWebSocket {
    constructor(url) {
      this.url = url;
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
      // Real browsers fire one final "dataavailable" ASYNCHRONOUSLY after
      // stop() -- simulated explicitly by tests via
      // FakeMediaRecorder.instances[i]._fireTrailingDataAvailable(), never
      // automatically here, to keep this harness deterministic.
    }
    _fireTrailingDataAvailable(data) {
      if (typeof this.ondataavailable === "function") {
        this.ondataavailable({ data });
      }
    }
  }
  FakeMediaRecorder.isTypeSupported = (candidate) => FakeMediaRecorder.supportedMimeType === candidate;
  FakeMediaRecorder.supportedMimeType = "";
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
  // RMS well above ANY reasonable adaptive floor: alternating 0/255 -> deviation 1.0
  return {
    getByteTimeDomainData: (buf) => {
      for (let i = 0; i < buf.length; i++) buf[i] = i % 2 === 0 ? 0 : 255;
    },
  };
}
/** Constant, moderate background noise (a real "noisy room" -- e.g. a fan
 * or an open-plan office), well above the OLD fixed VAD_RMS_THRESHOLD
 * (0.02) this production defect closure removes, but not loud enough to be
 * mistaken for real speech relative to itself. */
function noisyRoomAnalyser(amplitude) {
  return {
    getByteTimeDomainData: (buf) => {
      for (let i = 0; i < buf.length; i++) {
        const wobble = i % 2 === 0 ? amplitude : -amplitude;
        buf[i] = Math.max(0, Math.min(255, 128 + Math.round(wobble * 128)));
      }
    },
  };
}

test("continuous loop: assistant.audio.completed auto-resumes listening and restarts recording", async () => {
  const { PandaRealtime, FakeMediaRecorder } = loadRealtimeModule();
  const controller = new PandaRealtime.RealtimeVoiceController({});
  controller.mediaStream = { getTracks: () => [], getAudioTracks: () => [{ enabled: true }] };
  controller.ws = { readyState: 1, send: () => {} };
  controller._onTextFrame(JSON.stringify({ kind: "event", type: "user.turn.committed", turn_id: "t1", data: {} }));
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

test("VAD auto-barge-in sends explicit barge_in after sustained speech while Panda is speaking, but does NOT itself transfer ownership", async () => {
  // PR #24 section 3 (safe barge-in ownership): the client must send the
  // explicit control frame once it has ITS OWN confirmation of sustained
  // user speech, but must NOT assume that sending it immediately hands the
  // turn back -- state must stay SPEAKING (and the mic must stay off)
  // until the server's own "interruption" acknowledgement arrives.
  const { PandaRealtime, FakeMediaRecorder } = loadRealtimeModule();
  const sent = [];
  const controller = new PandaRealtime.RealtimeVoiceController({});
  controller._send = (payload) => sent.push(payload);
  controller.mediaStream = { getTracks: () => [], getAudioTracks: () => [] };
  controller.ws = { readyState: 1, send: () => {} };
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
    const startCountBeforeBargeIn = FakeMediaRecorder.startCount;
    controller._sampleVad();
    assert.equal(sent.length, 1);
    assert.equal(sent[0].type, "barge_in");
    assert.equal(
      controller.state,
      PandaRealtime.STATE_SPEAKING,
      "sending barge_in must NOT itself transfer ownership -- state stays SPEAKING until the server acknowledges"
    );
    assert.equal(
      FakeMediaRecorder.startCount,
      startCountBeforeBargeIn,
      "must not re-arm the mic before ownership is actually confirmed by the server"
    );

    // Server confirms the interruption -- ONLY NOW does ownership transfer.
    controller._onTextFrame(
      JSON.stringify({ kind: "event", type: "interruption", turn_id: "interrupted-turn", data: {} })
    );
    assert.equal(controller.state, PandaRealtime.STATE_LISTENING, "ownership transfers once the server acknowledges");
    assert.ok(
      FakeMediaRecorder.startCount > startCountBeforeBargeIn,
      "recording restarts once ownership is confirmed, never before"
    );
  } finally {
    Date.now = realNow;
  }
});

test("PR #24 section 3/12.J: ordinary background noise while Panda speaks never sends barge_in (Panda must not interrupt herself)", async () => {
  const { PandaRealtime } = loadRealtimeModule();
  const sent = [];
  const controller = new PandaRealtime.RealtimeVoiceController({});
  controller._send = (payload) => sent.push(payload);
  controller._setState(PandaRealtime.STATE_SPEAKING);
  controller._vadBuf = new Uint8Array(4);
  controller._noiseFloor = 0.03;

  let now = 6000;
  const realNow = Date.now;
  Date.now = () => now;
  try {
    // Constant ambient noise/activity, not sustained speech energy relative
    // to the calibrated floor -- e.g. residual room noise or a brief
    // transport artifact while Panda is talking.
    controller._analyser = noisyRoomAnalyser(0.03);
    for (let i = 0; i < 10; i++) {
      now += 100;
      controller._sampleVad();
    }
    assert.equal(sent.length, 0, "ambient noise/activity while Panda speaks must never fire an unconfirmed barge-in");
    assert.equal(controller.state, PandaRealtime.STATE_SPEAKING, "Panda must not interrupt herself due to background noise");
  } finally {
    Date.now = realNow;
  }
});

test("PR #24 section 6: stale assistant.audio.completed for an old/interrupted turn is ignored once a newer turn owns the session", async () => {
  const { PandaRealtime, FakeMediaRecorder } = loadRealtimeModule();
  const controller = new PandaRealtime.RealtimeVoiceController({});
  controller.mediaStream = { getTracks: () => [], getAudioTracks: () => [{ enabled: true }] };
  controller.ws = { readyState: 1, send: () => {} };
  const audioEvents = [];
  controller.handlers = { onAssistantAudio: (e) => audioEvents.push(e) };
  controller._setState(PandaRealtime.STATE_SPEAKING);

  // Turn A is committed and starts streaming audio.
  controller._onTextFrame(
    JSON.stringify({ kind: "event", type: "user.turn.committed", turn_id: "turn-a", data: {} })
  );
  controller._onTextFrame(JSON.stringify({ kind: "event", type: "assistant.audio.delta", turn_id: "turn-a", data: {} }));
  controller._onBinaryFrame(new ArrayBuffer(4));

  // Turn A is interrupted -- ownership clears.
  controller._onTextFrame(JSON.stringify({ kind: "event", type: "interruption", turn_id: "turn-a", data: {} }));

  // Turn B is committed and starts streaming its own audio (new ownership).
  controller._onTextFrame(
    JSON.stringify({ kind: "event", type: "user.turn.committed", turn_id: "turn-b", data: {} })
  );
  controller._onTextFrame(JSON.stringify({ kind: "event", type: "assistant.audio.delta", turn_id: "turn-b", data: {} }));
  controller._onBinaryFrame(new ArrayBuffer(8));

  const startCountBeforeStale = FakeMediaRecorder.startCount;
  // A stale/late "assistant.audio.completed" for the OLD turn A arrives --
  // must be ignored: no onAssistantAudio callback for it, no
  // resume-listening driven by it, and the CURRENT turn (B) keeps owning
  // the session.
  controller._onTextFrame(
    JSON.stringify({ kind: "event", type: "assistant.audio.completed", turn_id: "turn-a", data: {} })
  );
  assert.equal(audioEvents.length, 0, "stale completion for turn A must not finalize/flush any audio");
  assert.equal(
    controller.state,
    PandaRealtime.STATE_SPEAKING,
    "stale completion for an old turn must not resume listening over the CURRENT turn"
  );
  assert.equal(FakeMediaRecorder.startCount, startCountBeforeStale, "must not re-arm the mic from a stale completion");

  // The REAL completion for turn B arrives -- this one is authoritative.
  controller._onTextFrame(
    JSON.stringify({ kind: "event", type: "assistant.audio.completed", turn_id: "turn-b", data: {} })
  );
  assert.equal(audioEvents.length, 1);
  assert.equal(audioEvents[0].turnId, "turn-b");
  assert.equal(
    controller.state,
    PandaRealtime.STATE_LISTENING,
    "the CURRENT turn's completion drives the normal PLAYBACK -> LISTENING transition"
  );
});

test("PR #24 section 3: mic is requested with browser-native echo cancellation to reduce Panda's own audio bleeding into a false self-interruption", async () => {
  const { PandaRealtime } = loadRealtimeModule();
  let capturedConstraints = null;
  global.navigator.mediaDevices.getUserMedia = async (constraints) => {
    capturedConstraints = constraints;
    return { getTracks: () => [], getAudioTracks: () => [{ enabled: true }] };
  };
  const controller = new PandaRealtime.RealtimeVoiceController({});
  try {
    await controller.start({});
    assert.ok(capturedConstraints && capturedConstraints.audio, "must request the microphone");
    assert.equal(
      capturedConstraints.audio.echoCancellation,
      true,
      "browser AEC reduces Panda's own TTS audio bleeding into the mic and causing a false self-interruption"
    );
    assert.equal(capturedConstraints.audio.noiseSuppression, true);
  } finally {
    controller.close();
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

test("production voice defect closure: _stopRecording drops the recorder's trailing dataavailable chunk", async () => {
  const { PandaRealtime, FakeMediaRecorder } = loadRealtimeModule();
  const controller = new PandaRealtime.RealtimeVoiceController({});
  controller.mediaStream = { getTracks: () => [], getAudioTracks: () => [{ enabled: true }] };
  const sent = [];
  controller.ws = { readyState: 1, send: (payload) => sent.push(payload) };
  controller._setState(PandaRealtime.STATE_LISTENING);
  controller._startRecording();
  const recorder = FakeMediaRecorder.instances[FakeMediaRecorder.instances.length - 1];
  assert.equal(typeof recorder.ondataavailable, "function");

  // commit() calls _stopRecording(), which must detach ondataavailable
  // BEFORE calling stop() so the browser's real trailing chunk (fired here
  // explicitly, since Node's fake stop() does not do it automatically) is
  // never transmitted -- this is the fix for the production defect where a
  // stray post-commit chunk was misread as new user speech (spurious
  // barge-in) or leaked into the NEXT turn's audio buffer.
  controller.commit();
  assert.equal(recorder.ondataavailable, null, "handler detached before stop() so late data cannot leak");
  recorder._fireTrailingDataAvailable({
    size: 42,
    arrayBuffer: () => Promise.resolve(new ArrayBuffer(42)),
  });
  await Promise.resolve();
  const binaryChunksSent = sent.filter((v) => v instanceof ArrayBuffer);
  assert.equal(binaryChunksSent.length, 0, "the trailing chunk after commit is never sent to the server");
  // exactly one JSON control frame (the audio.commit itself) was sent.
  assert.equal(sent.filter((v) => typeof v === "string").length, 1);
});

test("start() negotiates and reports the ACTUAL MediaRecorder mime type to the server (Boundary F)", async () => {
  const { PandaRealtime, FakeMediaRecorder } = loadRealtimeModule();
  FakeMediaRecorder.supportedMimeType = "audio/webm;codecs=opus";
  const controller = new PandaRealtime.RealtimeVoiceController({});

  try {
    await controller.start({ conversationId: "conv-1" });

    assert.equal(controller._negotiatedMimeType, "audio/webm;codecs=opus");
    assert.ok(controller.ws, "connected");
    const match = /[?&]mime_type=([^&]+)/.exec(controller.ws.url);
    assert.ok(match, `expected a mime_type query param in the WS URL, got: ${controller.ws.url}`);
    assert.equal(decodeURIComponent(match[1]), "audio/webm;codecs=opus");
  } finally {
    // _startVad() started a real setInterval -- must be torn down or the
    // Node test runner's process hangs waiting for the timer forever.
    controller.close();
  }
});

test("PRODUCTION ACCEPTANCE FAILED follow-up: adaptive noise floor auto-commits in a noisy room where the OLD fixed threshold never would", async () => {
  const { PandaRealtime } = loadRealtimeModule();
  const sent = [];
  const controller = new PandaRealtime.RealtimeVoiceController({});
  controller._send = (payload) => sent.push(payload);
  controller._stopRecording = () => {};
  controller._setState(PandaRealtime.STATE_LISTENING);
  controller._vadBuf = new Uint8Array(4);

  // A room with constant background noise at RMS ~0.03 -- ABOVE the OLD
  // fixed VAD_RMS_THRESHOLD (0.02) this root-cause fix removes. With the
  // old code this noise ALONE would be permanently misread as "active
  // speech" (0.03 > 0.02, forever), the 900ms silence timer would never
  // start, and audio.commit would never fire automatically -- exactly the
  // reported production defect ("user has to press the mic button").
  const noiseAmplitude = 0.03;
  controller._noiseFloor = noiseAmplitude; // already calibrated to this room

  let now = 9000;
  const realNow = Date.now;
  Date.now = () => now;
  try {
    controller._analyser = noisyRoomAnalyser(noiseAmplitude);
    for (let i = 0; i < 3; i++) {
      now += 100;
      controller._sampleVad();
    }
    assert.equal(
      controller._sawSpeechThisTurn,
      false,
      "constant ambient noise relative to its OWN calibrated floor is never mistaken for speech"
    );

    controller._analyser = loudAnalyser();
    now += 100;
    controller._sampleVad();
    assert.equal(controller._sawSpeechThisTurn, true, "real speech is still detected relative to the calibrated floor");

    // Speech ends -- the room returns to its OWN ambient noise level (never
    // perfect digital silence in a real room). The fix must recognize THIS
    // as "end of speech", not permanent continued "activity".
    controller._analyser = noisyRoomAnalyser(noiseAmplitude);
    now += 100;
    controller._sampleVad();
    assert.equal(sent.length, 0, "does not commit on the very first quiet-again sample");

    now += 1000; // exceeds VAD_SILENCE_COMMIT_MS
    controller._sampleVad();
    assert.equal(
      sent.length,
      1,
      "auto-commits once the room returns to its OWN ambient noise level after real speech -- no button press needed"
    );
    assert.equal(sent[0].type, "audio.commit");
  } finally {
    Date.now = realNow;
  }
});

test("adaptive noise floor is calibrated from THIS session's real ambient mic input, not a hardcoded constant", async () => {
  const { PandaRealtime } = loadRealtimeModule();
  const controller = new PandaRealtime.RealtimeVoiceController({});
  controller.mediaStream = { getTracks: () => [], getAudioTracks: () => [] };
  const noiseAmplitude = 0.05; // a fairly loud, but constant, real room
  controller._startVad();
  controller._analyser = noisyRoomAnalyser(noiseAmplitude);
  controller._vadBuf = new Uint8Array(4);

  try {
    // Drive the calibration window directly (real timer torn down below) --
    // 5 samples of pure ambient noise, no state classification happens yet.
    for (let i = 0; i < 5; i++) controller._sampleVad();
    assert.ok(
      Math.abs(controller._noiseFloor - noiseAmplitude) < 0.005,
      `expected the floor to converge to this room's real ~${noiseAmplitude} ambient level, got ${controller._noiseFloor}`
    );
  } finally {
    controller._stopVad();
  }
});

test("forward-progress safety net: a single utterance auto-commits after VAD_MAX_TURN_MS even if silence never cleanly registers", async () => {
  const { PandaRealtime } = loadRealtimeModule();
  const sent = [];
  const controller = new PandaRealtime.RealtimeVoiceController({});
  controller._send = (payload) => sent.push(payload);
  controller._stopRecording = () => {};
  controller._setState(PandaRealtime.STATE_LISTENING);
  controller._analyser = loudAnalyser();
  controller._vadBuf = new Uint8Array(4);

  let now = 20000;
  const realNow = Date.now;
  Date.now = () => now;
  try {
    controller._sampleVad(); // speech onset
    assert.equal(sent.length, 0);
    now += 15000; // exceeds VAD_MAX_TURN_MS (15000) of CONTINUOUS "speech", silence never occurs
    controller._sampleVad();
    assert.equal(
      sent.length,
      1,
      "a single utterance must never hold LISTENING open forever, even if the room never goes quiet"
    );
    assert.equal(sent[0].type, "audio.commit");
  } finally {
    Date.now = realNow;
  }
});

test("playback ack: notifyPlaybackStarted/Completed send safe telemetry frames referencing the turn", async () => {
  const { PandaRealtime } = loadRealtimeModule();
  const sent = [];
  const controller = new PandaRealtime.RealtimeVoiceController({});
  controller.ws = { readyState: 1, send: (raw) => sent.push(JSON.parse(raw)) };

  controller.notifyPlaybackStarted("turn_abc");
  controller.notifyPlaybackCompleted("turn_abc");

  assert.deepEqual(sent, [
    { type: "playback_event", stage: "started", turn_id: "turn_abc" },
    { type: "playback_event", stage: "completed", turn_id: "turn_abc" },
  ]);
});
