/** Block 4 — realtime voice transport client.
 *
 * Talks to /api/v1/realtime/ws (see realtime/router.py). Owns: microphone
 * capture (MediaRecorder), the WebSocket connection, and translation of the
 * canonical realtime event contract (realtime/events.py) into plain
 * callback hooks. Contains NO conversation-rendering/business logic --
 * app.js owns the timeline/state and decides what each callback means for
 * the UI, so there is exactly one conversation data model (Block 4.27: one
 * canonical contract, not a parallel voice-only conversation engine).
 */
(function (global) {
  const STATE_IDLE = "idle";
  const STATE_CONNECTING = "connecting";
  const STATE_LISTENING = "listening";
  const STATE_THINKING = "thinking";
  const STATE_SPEAKING = "speaking";
  const STATE_ERROR = "error";

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

  function wsUrl(conversationId, voiceId, sessionId) {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const params = new URLSearchParams();
    if (conversationId) params.set("conversation_id", conversationId);
    if (voiceId) params.set("voice_id", voiceId);
    if (sessionId) params.set("session_id", sessionId);
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
    }

    _setState(next) {
      if (this.state === next) return;
      this.state = next;
      if (this.handlers.onStateChange) this.handlers.onStateChange(next);
    }

    /** Starts a brand-new voice turn cycle: mic permission -> WS connect ->
     * automatic recording once the transport confirms session.connected. */
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
      this._connect(options.conversationId, options.voiceId);
    }

    _connect(conversationId, voiceId) {
      const ws = new WebSocket(wsUrl(conversationId, voiceId));
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
          if (this.state === STATE_SPEAKING) this._setState(STATE_LISTENING);
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

    _startRecording() {
      if (!this.mediaStream) return;
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
          this.recorder.stop();
        } catch (e) {
          /* already stopped */
        }
      }
      this.recorder = null;
    }

    /** User explicitly finished speaking (click mic while listening). */
    commit() {
      if (this.state !== STATE_LISTENING) return;
      this._stopRecording();
      this._send({ type: "audio.commit", client_turn_id: uuid() });
    }

    /** Click mic while Panda is thinking/speaking: stop Panda, start
     * listening for the next turn immediately (Block 4.13 barge-in). */
    interruptAndListen() {
      if (this.state !== STATE_THINKING && this.state !== STATE_SPEAKING) return;
      this._send({ type: "barge_in" });
      this._setState(STATE_LISTENING);
      this._startRecording();
    }

    selectVoice(voiceId) {
      this._send({ type: "voice.select", voice_id: voiceId });
    }

    _send(payload) {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(payload));
    }

    _teardownMic() {
      this._stopRecording();
      if (this.mediaStream) {
        this.mediaStream.getTracks().forEach((t) => t.stop());
        this.mediaStream = null;
      }
    }

    /** Fully end the voice session: release mic, close transport. */
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
