/** Block 4.28-4.30 — personalization/settings client + dialog controller.
 * Talks only to /api/v1/personalization (separate boundary from the
 * business-assistant conversation API, same auth model). */
(function (global) {
  const BASE = "/api/v1/personalization";

  function authHeaders() {
    const key = sessionStorage.getItem("panda_api_key") || "";
    return key ? { "X-API-Key": key } : {};
  }

  async function request(path, options) {
    const opts = options || {};
    const res = await fetch(`${BASE}${path}`, {
      ...opts,
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        ...authHeaders(),
        ...(opts.headers || {}),
      },
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      const detail = data.detail || data;
      const err = new Error(detail.message || res.statusText || "personalization_error");
      err.code = detail.code || "personalization_error";
      err.status = res.status;
      throw err;
    }
    if (res.status === 204) return {};
    const ct = res.headers.get("content-type") || "";
    if (ct.startsWith("audio/")) return res.blob();
    return res.json();
  }

  const client = {
    getPreferences() {
      return request("/preferences");
    },
    setPreferences(payload) {
      return request("/preferences", { method: "PUT", body: JSON.stringify(payload) });
    },
    listVoices() {
      return request("/voices");
    },
    previewVoice(voiceId) {
      return request(`/voices/${encodeURIComponent(voiceId)}/preview`, { method: "POST" });
    },
  };

  global.PandaPersonalizationApi = client;

  /** Dialog controller — wires the #personalization-dialog form to the API
   * above. Pure UI orchestration; app.js owns the show/hide entry point. */
  function createPersonalizationDialog() {
    const els = {
      dialog: document.getElementById("personalization-dialog"),
      style: document.getElementById("pz-style"),
      tone: document.getElementById("pz-tone"),
      length: document.getElementById("pz-length"),
      language: document.getElementById("pz-language"),
      voice: document.getElementById("pz-voice"),
      preview: document.getElementById("pz-voice-preview"),
      error: document.getElementById("personalization-error"),
      save: document.getElementById("personalization-save"),
      cancel: document.getElementById("personalization-cancel"),
    };
    if (!els.dialog) return null;

    let currentPreviewAudio = null;
    let onSaved = null;

    function show(el) { if (el) el.classList.remove("hidden"); }
    function hide(el) { if (el) el.classList.add("hidden"); }

    async function populateVoices(selected) {
      els.voice.innerHTML = "";
      let voices = [];
      try {
        voices = await client.listVoices();
      } catch (e) {
        voices = [];
      }
      voices.forEach((v) => {
        const opt = document.createElement("option");
        opt.value = v.voice_id;
        opt.textContent = v.label;
        els.voice.appendChild(opt);
      });
      if (selected) els.voice.value = selected;
    }

    async function open(callback) {
      onSaved = callback || null;
      els.error.textContent = "";
      els.save.disabled = true;
      try {
        const prefs = await client.getPreferences();
        await populateVoices(prefs.voice_id);
        els.style.value = prefs.style || "default";
        els.tone.value = prefs.tone || "";
        els.length.value = prefs.length || "balanced";
        els.language.value = prefs.language || "auto";
      } catch (e) {
        els.error.textContent = e.message || "Не удалось загрузить настройки";
      } finally {
        els.save.disabled = false;
      }
      show(els.dialog);
      els.dialog.removeAttribute("hidden");
    }

    function close() {
      hide(els.dialog);
      els.dialog.setAttribute("hidden", "");
      if (currentPreviewAudio) {
        currentPreviewAudio.pause();
        currentPreviewAudio = null;
      }
    }

    async function save() {
      els.error.textContent = "";
      els.save.disabled = true;
      try {
        const prefs = await client.setPreferences({
          style: els.style.value,
          tone: els.tone.value,
          length: els.length.value,
          language: els.language.value,
          voice_id: els.voice.value,
        });
        close();
        if (onSaved) onSaved(prefs);
      } catch (e) {
        els.error.textContent = e.message || "Не удалось сохранить настройки";
      } finally {
        els.save.disabled = false;
      }
    }

    async function preview() {
      const voiceId = els.voice.value;
      if (!voiceId) return;
      els.preview.disabled = true;
      try {
        const blob = await client.previewVoice(voiceId);
        const url = URL.createObjectURL(blob);
        if (currentPreviewAudio) currentPreviewAudio.pause();
        currentPreviewAudio = new Audio(url);
        currentPreviewAudio.play().catch(() => {});
      } catch (e) {
        els.error.textContent = e.message || "Не удалось прослушать голос";
      } finally {
        els.preview.disabled = false;
      }
    }

    els.save.onclick = () => { save(); };
    els.cancel.onclick = () => { close(); };
    els.preview.onclick = () => { preview(); };
    els.dialog.addEventListener("click", (e) => {
      if (e.target === els.dialog) close();
    });

    return { open, close };
  }

  global.PandaPersonalizationDialog = { create: createPersonalizationDialog };
})(window);
