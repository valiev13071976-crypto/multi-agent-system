/** Block 4 — realtime voice transport client.
 *
 * Talks to /api/v1/realtime/ws (see realtime/router.py). Owns: microphone
 * capture (MediaRecorder), voice-activity detection for the CONTINUOUS
 * conversation loop below, the WebSocket connection, and translation of the
 * canonical realtime event contract (realtime/events.py) into plain
 * callback hooks. Contains NO conversation-rendering/business logic --
 * app.js owns the timeline/state and decides what each callback means for
 * the UI, so there is exactly one conversation data model (Block 4.27: one
 * canonical contract, not a parallel voice-only conversation engine).
 *
 * ChatGPT-voice-mode parity defect closure: voice mode is a CONTINUOUS
 * conversation, not "record once, stop, click again for every turn". Once
 * started, the controller keeps listening -> auto-committing ->
 * auto-resuming-listening after every assistant turn, and auto-detects
 * barge-in while Panda is thinking/speaking, using a lightweight real
 * signal-energy VAD (Web Audio AnalyserNode) on the raw microphone stream
 * -- never a fabricated/fake animation disconnected from actual mic input.
 * A manual tap on the voice orb (see app.js) remains available as an
 * explicit fallback (commit-now / barge-in-now), because this sandboxed
 * environment has no real microphone hardware to calibrate VAD thresholds
 * against production speech/noise levels -- see the Block 4 delivery
 * report for the honestly-unproven item this implies.
 */
(function (global) {
  const STATE_IDLE = "idle";
  const STATE_CONNECTING = "connecting";
  const STATE_LISTENING = "listening";
  const STATE_THINKING = "thinking";
  const STATE_SPEAKING = "speaking";
  const STATE_ERROR = "error";

  // VAD tuning (Block 4 voice-mode continuity fix). Deliberately
  // conservative defaults; a manual orb tap always works regardless of
  // whether these thresholds are well-calibrated for a given mic/room.
  const VAD_SAMPLE_MS = 100;
  const VAD_RMS_THRESHOLD = 0.02;
  const VAD_SILENCE_COMMIT_MS = 900; // sustained silence after speech -> auto end-of-turn
  const VAD_BARGE_IN_MS = 180; // sustained speech while Panda thinks/speaks -> auto barge-in

  function isSupported() {
    return Boolean(
      global.navigator &&
        navigator.mediaDevices &&
        typeof navigator.mediaDevices.getUserMedia === "function" &&
        global.WebSocket &&
        global.MediaRecorder
    );
  }

  function pickMimeType() {
    if (!global.MediaRecorder || typeof MediaRecorder.isTypeSupported !== "function") return "";
    const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/ogg"];
    for (const c of candidates) {
      if (MediaRecorder.isTypeSupported(c)) return c;
    }
    return "";
  }

  function wsUrl(conversationId, voiceId, sessionId, mimeType) {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const params = new URLSearchParams();
    if (conversationId) params.set("conversation_id", conversationId);
    if (voiceId) params.set("voice_id", voiceId);
    if (sessionId) params.set("session_id", sessionId);
    // Production voice defect closure Boundary F: tell the server the
    // ACTUAL container/codec MediaRecorder negotiated (pickMimeType()
    // below) so the STT call server-side never assumes wav for audio that
    // is really webm/opus.
    if (mimeType) params.set("mime_type", mimeType);
    // Block 4.38: browsers cannot set custom headers on a WebSocket
    // handshake -- a human session (panda_session cookie) is sent
    // automatically by the browser same-origin, needing no query param at
    // all; a workspace API key (sessionStorage-only, never persisted)
    // still needs this fallback, verified server-side through the exact
    // same AuthService as every HTTP request.
    const apiKey = sessionStorage.getItem("panda_api_key") || "";
    if (apiKey) params.set("api_key", apiKey);
    const qs = params.toString();
    return `${proto}//${location.host}/api/v1/realtime/ws${qs ? `?${qs}` : ""}`;
  }

  function uuid() {
    return global.crypto && global.crypto.randomUUID
      ? global.crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  class RealtimeVoiceController {
    constructor(handlers) {
      this.handlers = handlers || {};
      this.ws = null;
      this.mediaStream = null;
      this.recorder = null;
      this.state = STATE_IDLE;
      this.sessionId = null;
      this.conversationId = null;
      this.voiceId = null;
      this._pendingAudioTurnId = null;
      this._audioChunks = [];
      this._negotiatedMimeType = "";
      this._muted = false;
      this._audioCtx = null;
      this._analyser = null;
      this._vadBuf = null;
      this._vadTimer = null;
      this._sawSpeechThisTurn = false;
      this._vadSilenceSince = null;
      this._vadSpeechSince = null;
    }

    get isMuted() {
      return this._muted;
    }

    _setState(next) {
      if (this.state === next) return;
      this.state = next;
      if (this.handlers.onStateChange) this.handlers.onStateChange(next);
    }

    /** Starts a brand-new CONTINUOUS voice conversation: mic permission ->
     * WS connect -> automatic listen/commit/respond/listen loop, until
     * close() is called explicitly (voice-mode exit). */
    async start(opts) {
      const options = opts || {};
      if (this.state !== STATE_IDLE && this.state !== STATE_ERROR) return;
      this._setState(STATE_CONNECTING);
      try {
        this.mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      } catch (e) {
        this._setState(STATE_ERROR);
        if (this.handlers.onError) {
          this.handlers.onError({ code: "mic_permission_denied", message: String((e && e.message) || e) });
        }
        this._setState(STATE_IDLE);
        return;
      }
      this._startVad();
      this._negotiatedMimeType = pickMimeType();
      this._connect(options.conversationId, options.voiceId);
    }

    _connect(conversationId, voiceId) {
      const ws = new WebSocket(wsUrl(conversationId, voiceId, null, this._negotiatedMimeType));
      ws.binaryType = "arraybuffer";
      this.ws = ws;
      ws.onmessage = (evt) => {
        if (typeof evt.data === "string") this._onTextFrame(evt.data);
        else this._onBinaryFrame(evt.data);
      };
      ws.onerror = () => {
        if (this.handlers.onError) this.handlers.onError({ code: "transport_error", message: "Соединение прервано" });
      };
      ws.onclose = () => {
        this._teardownMic();
        this._setState(STATE_IDLE);
        if (this.handlers.onSessionClosed) this.handlers.onSessionClosed({ reason: "transport_closed" });
      };
    }

    _onTextFrame(raw) {
      let payload;
      try {
        payload = JSON.parse(raw);
      } catch (e) {
        return;
      }
      if (!payload || payload.kind !== "event") return;
      const type = payload.type;
      const data = payload.data || {};
      const turnId = payload.turn_id || "";
      const h = this.handlers;
      switch (type) {
        case "session.started":
          this.sessionId = payload.session_id;
          this.conversationId = data.conversation_id;
          this.voiceId = data.voice_id;
          if (h.onSessionStarted) h.onSessionStarted({ conversationId: data.conversation_id, voiceId: data.voice_id });
          break;
        case "session.connected":
          this._sawSpeechThisTurn = false;
          this._setState(STATE_LISTENING);
          this._startRecording();
          break;
        case "user.transcript.partial":
          if (h.onPartialTranscript) h.onPartialTranscript(data.text || "");
          break;
        case "user.turn.committed":
          this._setState(STATE_THINKING);
          if (h.onUserTurnCommitted) h.onUserTurnCommitted({ turnId, text: data.text || "", modality: data.modality || "voice" });
          break;
        case "assistant.status":
          if (h.onStatus) h.onStatus(data.status || "");
          break;
        case "assistant.text.delta":
          if (h.onAssistantTextDelta) h.onAssistantTextDelta({ turnId, delta: data.delta || "" });
          break;
        case "assistant.text.completed":
          if (h.onAssistantTextCompleted) h.onAssistantTextCompleted({ turnId, text: data.text || "" });
          break;
        case "assistant.audio.delta":
          this._setState(STATE_SPEAKING);
          this._pendingAudioTurnId = turnId;
          break;
        case "assistant.audio.completed":
          this._finalizeAudio(turnId);
          // Continuous voice mode (ChatGPT-parity defect closure): Panda
          // automatically returns to listening -- the user never presses a
          // button to start the next turn.
          this._resumeListening(turnId);
          break;
        case "tool.started":
          if (h.onToolStarted) h.onToolStarted({ turnId });
          break;
        case "tool.completed":
          if (h.onToolCompleted) h.onToolCompleted({ turnId, artifactCount: data.artifact_count || 0 });
          break;
        case "interruption":
          this._audioChunks = [];
          this._pendingAudioTurnId = null;
          if (h.onInterruption) h.onInterruption({ turnId });
          this._setState(STATE_LISTENING);
          break;
        case "error":
          if (h.onError) h.onError({ code: data.code, message: data.message });
          // Every in-turn realtime error is server-side recoverable (the
          // canonical session state machine always lands back on
          // LISTENING -- realtime/state_machine.py TRANSITIONS). Mirror
          // that locally so the continuous loop never stalls waiting for
          // a turn-completion event that will never arrive.
          if (this.state !== STATE_IDLE && this.state !== STATE_CONNECTING) {
            this._resumeListening();
          }
          break;
        case "session.closed":
          if (h.onSessionClosed) h.onSessionClosed({ reason: data.reason });
          break;
        default:
          break;
      }
    }

    _onBinaryFrame(buf) {
      if (this._pendingAudioTurnId === null) return;
      this._audioChunks.push(buf);
    }

    _finalizeAudio(turnId) {
      const chunks = this._audioChunks;
      this._audioChunks = [];
      this._pendingAudioTurnId = null;
      if (!chunks.length) return;
      const blob = new Blob(chunks, { type: "audio/mpeg" });
      const url = URL.createObjectURL(blob);
      if (this.handlers.onAssistantAudio) this.handlers.onAssistantAudio({ turnId, url });
    }

    /** Re-arms listening + microphone capture for the next turn without any
     * user action -- the heart of the continuous voice-conversation loop. */
    _resumeListening(turnId) {
      this._sawSpeechThisTurn = false;
      this._vadSilenceSince = null;
      this._vadSpeechSince = null;
      this._setState(STATE_LISTENING);
      if (!this._muted) this._startRecording();
      // DEFECT B latency acceptance: closes the server-side per-turn
      // latency timeline (speech_start .. listening_resumed) with the one
      // stage only the browser can know -- when it actually re-armed the
      // mic for the next turn.
      if (turnId) this.notifyListeningResumed(turnId);
    }

    _startRecording() {
      if (!this.mediaStream) return;
      if (this.recorder && this.recorder.state === "recording") return;
      const mimeType = pickMimeType();
      let recorder;
      try {
        recorder = mimeType ? new MediaRecorder(this.mediaStream, { mimeType }) : new MediaRecorder(this.mediaStream);
      } catch (e) {
        try {
          recorder = new MediaRecorder(this.mediaStream);
        } catch (e2) {
          if (this.handlers.onError) this.handlers.onError({ code: "recorder_unavailable", message: String(e2) });
          return;
        }
      }
      this.recorder = recorder;
      recorder.ondataavailable = (evt) => {
        if (!evt.data || !evt.data.size || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;
        evt.data.arrayBuffer().then((buf) => {
          if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(buf);
        });
      };
      recorder.start(250);
    }

    _stopRecording() {
      if (this.recorder && this.recorder.state !== "inactive") {
        try {
          // Production voice defect closure (duplicate/stray-audio root
          // cause): MediaRecorder.stop() asynchronously fires one final
          // "dataavailable" AFTER this call returns. If that trailing
          // chunk is still sent to the server after we've already sent
          // audio.commit for THIS turn, it lands in the server's freshly-
          // cleared audio_buffer for the NEXT turn and (worse) can look
          // like the user started speaking again while Panda is
          // thinking/speaking, triggering a spurious barge-in. Detaching
          // the handler before stop() drops that trailing chunk instead of
          // transmitting it -- no data loss for the CURRENT turn (its
          // bytes were already sent via prior ondataavailable calls during
          // recorder.start(250) slicing).
          this.recorder.ondataavailable = null;
          this.recorder.stop();
        } catch (e) {
          /* already stopped */
        }
      }
      this.recorder = null;
    }

    // --- voice-activity detection (real mic-energy signal, not fabricated) --

    _startVad() {
      if (!this.mediaStream) return;
      const Ctx = global.AudioContext || global.webkitAudioContext;
      if (!Ctx) return; // progressive enhancement -- manual orb tap still works
      try {
        this._audioCtx = new Ctx();
        const source = this._audioCtx.createMediaStreamSource(this.mediaStream);
        this._analyser = this._audioCtx.createAnalyser();
        this._analyser.fftSize = 512;
        source.connect(this._analyser);
        this._vadBuf = new Uint8Array(this._analyser.fftSize);
      } catch (e) {
        this._analyser = null;
        return;
      }
      this._vadTimer = global.setInterval(() => this._sampleVad(), VAD_SAMPLE_MS);
    }

    _stopVad() {
      if (this._vadTimer) {
        global.clearInterval(this._vadTimer);
        this._vadTimer = null;
      }
      if (this._audioCtx) {
        try {
          this._audioCtx.close();
        } catch (e) {
          /* already closed */
        }
        this._audioCtx = null;
      }
      this._analyser = null;
      this._vadSilenceSince = null;
      this._vadSpeechSince = null;
    }

    _sampleVad() {
      if (!this._analyser || this._muted) return;
      this._analyser.getByteTimeDomainData(this._vadBuf);
      let sumSquares = 0;
      for (let i = 0; i < this._vadBuf.length; i++) {
        const v = (this._vadBuf[i] - 128) / 128;
        sumSquares += v * v;
      }
      const rms = Math.sqrt(sumSquares / this._vadBuf.length);
      const active = rms > VAD_RMS_THRESHOLD;
      const now = Date.now();

      if (this.state === STATE_LISTENING) {
        if (this.handlers.onVoiceActivity) this.handlers.onVoiceActivity(active);
        if (active) {
          this._sawSpeechThisTurn = true;
          this._vadSilenceSince = null;
          return;
        }
        if (!this._sawSpeechThisTurn) return;
        if (this._vadSilenceSince === null) {
          this._vadSilenceSince = now;
          return;
        }
        if (now - this._vadSilenceSince >= VAD_SILENCE_COMMIT_MS) {
          this.commit();
        }
        return;
      }

      if (this.state === STATE_THINKING || this.state === STATE_SPEAKING) {
        if (!active) {
          this._vadSpeechSince = null;
          return;
        }
        if (this._vadSpeechSince === null) {
          this._vadSpeechSince = now;
          return;
        }
        if (now - this._vadSpeechSince >= VAD_BARGE_IN_MS) {
          this.interruptAndListen();
        }
        return;
      }

      this._vadSilenceSince = null;
      this._vadSpeechSince = null;
    }

    /** Ends the current user turn -- called automatically by VAD once
     * sustained silence follows real detected speech, or manually via a
     * tap on the voice orb (app.js) as an explicit fallback. */
    commit() {
      if (this.state !== STATE_LISTENING) return;
      this._stopRecording();
      this._sawSpeechThisTurn = false;
      this._vadSilenceSince = null;
      this._send({ type: "audio.commit", client_turn_id: uuid() });
    }

    /** Stops Panda and starts listening for the next turn immediately
     * (Block 4.13 barge-in) -- called automatically by VAD once sustained
     * speech is detected while Panda is thinking/speaking, or manually via
     * a tap on the voice orb as an explicit fallback. */
    interruptAndListen() {
      if (this.state !== STATE_THINKING && this.state !== STATE_SPEAKING) return;
      this._send({ type: "barge_in" });
      this._vadSpeechSince = null;
      this._sawSpeechThisTurn = true; // the interrupting utterance is already under way
      this._setState(STATE_LISTENING);
      if (!this._muted) this._startRecording();
    }

    /** Pauses the user's microphone capture without ending the voice
     * session (ChatGPT-parity: mute != exit). The WS session, conversation
     * context, and Panda's ability to keep speaking are all unaffected. */
    mute() {
      if (this._muted) return;
      this._muted = true;
      if (this.mediaStream) this.mediaStream.getAudioTracks().forEach((t) => (t.enabled = false));
      this._stopRecording();
      this._sawSpeechThisTurn = false;
      this._vadSilenceSince = null;
      this._vadSpeechSince = null;
      if (this.handlers.onMuteChange) this.handlers.onMuteChange(true);
    }

    /** Resumes microphone capture after mute(); if still listening for the
     * current turn, recording restarts immediately. */
    unmute() {
      if (!this._muted) return;
      this._muted = false;
      if (this.mediaStream) this.mediaStream.getAudioTracks().forEach((t) => (t.enabled = true));
      if (this.state === STATE_LISTENING) this._startRecording();
      if (this.handlers.onMuteChange) this.handlers.onMuteChange(false);
    }

    selectVoice(voiceId) {
      this._send({ type: "voice.select", voice_id: voiceId });
    }

    /** Playback telemetry ack (section 11/20): app.js calls these from the
     * <audio> element's real "play"/"ended" DOM events so a production
     * failure between "server sent TTS audio" and "user actually heard it"
     * is visible server-side without another Cursor round. Never blocks or
     * affects local playback if the socket happens to be down. */
    notifyPlaybackStarted(turnId) {
      this._send({ type: "playback_event", stage: "started", turn_id: turnId || "" });
    }

    notifyPlaybackCompleted(turnId) {
      this._send({ type: "playback_event", stage: "completed", turn_id: turnId || "" });
    }

    notifyListeningResumed(turnId) {
      this._send({ type: "playback_event", stage: "listening_resumed", turn_id: turnId || "" });
    }

    _send(payload) {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(payload));
    }

    _teardownMic() {
      this._stopRecording();
      this._stopVad();
      if (this.mediaStream) {
        this.mediaStream.getTracks().forEach((t) => t.stop());
        this.mediaStream = null;
      }
      this._muted = false;
    }

    /** Fully end the voice session (ChatGPT-parity exit, NOT mute): release
     * mic, close transport, stop any Panda audio, return to text composer. */
    close() {
      this._send({ type: "session.close" });
      this._teardownMic();
      if (this.ws) {
        try {
          this.ws.close();
        } catch (e) {
          /* already closed */
        }
        this.ws = null;
      }
      this._audioChunks = [];
      this._pendingAudioTurnId = null;
      this._setState(STATE_IDLE);
    }
  }

  global.PandaRealtime = {
    RealtimeVoiceController,
    isSupported,
    STATE_IDLE,
    STATE_CONNECTING,
    STATE_LISTENING,
    STATE_THINKING,
    STATE_SPEAKING,
    STATE_ERROR,
  };
})(window);
