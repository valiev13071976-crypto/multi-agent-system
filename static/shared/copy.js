/** Russian product copy and human-readable error mapping. */
(function (global) {
  const STATUS_LABELS = {
    RECEIVED: "Принято",
    VALIDATING: "Проверка…",
    PLANNING: "Обрабатываю запрос…",
    QUEUED: "В очереди…",
    RUNNING: "Обрабатываю запрос…",
    RESUMING: "Продолжаю выполнение…",
    WAITING_FOR_APPROVAL: "Ожидает вашего подтверждения",
    COMPLETED: "Готово",
    FAILED: "Ошибка",
    REJECTED: "Отклонено",
    CANCELLED: "Отменено",
    BLOCKED: "Заблокировано",
  };

    const USER_PROGRESS = "Обрабатываю запрос…";
    const USER_THINKING = "Думаю…";
    const MISSING_FINAL_ANSWER = "Panda не смогла сформировать ответ. Попробуйте ещё раз.";

  const ERROR_MAP = {
    BAA_AUTH_FAILED: "Сессия недействительна. Войдите снова.",
    BAA_ACCESS_DENIED: "У вас нет доступа к этой функции.",
    BAA_NOT_FOUND: "Запрос не найден.",
    BAA_APPROVAL_STALE: "Подтверждение устарело — обновите предпросмотр.",
    BAA_INVALID_STATE: "Это действие недоступно в текущем состоянии.",
    BAA_IDEMPOTENCY_CONFLICT: "Конфликт повторной отправки.",
    BAA_PROVIDER_UNAVAILABLE: "Внешний сервис временно недоступен.",
    BAA_INTEGRATION_UNAVAILABLE: "Интеграция пока не настроена.",
    BAA_CONVERSATION_UNAVAILABLE: "Panda временно не может обработать запрос. Попробуйте позже.",
    BAA_MISSING_FINAL_ANSWER: "Panda не смогла сформировать ответ. Попробуйте ещё раз.",
    UNAUTHORIZED: "Сессия недействительна. Войдите снова.",
    request_failed: "Произошла ошибка. Попробуйте ещё раз.",
  };

  function statusLabel(status) {
    return STATUS_LABELS[status] || USER_PROGRESS;
  }

  function userFacingStatus(status) {
    if (status === "PLANNING" || status === "RUNNING" || status === "RESUMING" || status === "QUEUED") {
      return USER_PROGRESS;
    }
    if (status === "VALIDATING" || status === "RECEIVED") return USER_THINKING;
    return statusLabel(status);
  }

  function mapError(err) {
    if (!err) return "Произошла ошибка. Попробуйте ещё раз.";
    const code = err.code || err.message;
    if (ERROR_MAP[code]) return ERROR_MAP[code];
    if (err.status === 401 || err.status === 403) return ERROR_MAP.BAA_ACCESS_DENIED;
    if (err.status === 429) return "Слишком много запросов. Попробуйте немного позже.";
    if (err.status >= 500) return "Произошла ошибка. Попробуйте ещё раз.";
    const msg = String(err.message || "");
    if (/traceback|exception|sql|filesystem|provider route/i.test(msg)) {
      return "Произошла ошибка. Попробуйте ещё раз.";
    }
    return msg || ERROR_MAP.request_failed;
  }

  // Block 4 voice-mode defect closure: realtime session error codes
  // (realtime/errors.py) are internal wire identifiers, never user-facing
  // copy -- mapping them here (the ONE canonical Russian-copy module,
  // reused by realtime.js/app.js) instead of leaking raw codes like
  // "rt_audio_empty" into the composer error text. Codes in
  // REALTIME_SILENT_CODES are expected/recoverable in the continuous voice
  // loop (e.g. an auto-detected end-of-turn with no real speech in the
  // buffer) -- they resume listening silently, never alarming the user.
  const REALTIME_SILENT_CODES = new Set(["rt_audio_empty", "rt_stt_empty_transcript"]);
  const REALTIME_ERROR_MAP = {
    mic_permission_denied: "Доступ к микрофону запрещён. Разрешите доступ в настройках браузера.",
    device_unavailable: "Микрофон недоступен. Проверьте устройство и повторите попытку.",
    recorder_unavailable: "Запись голоса недоступна в этом браузере.",
    transport_error: "Голосовое соединение прервано. Пробуем восстановить.",
    rt_stt_failed: "Не удалось распознать речь. Попробуйте сказать ещё раз.",
    rt_tts_failed: "Не удалось озвучить ответ, но текст ответа доступен выше.",
    rt_conversation_unavailable: "Panda временно не может обработать запрос. Попробуйте позже.",
  };

  function isSilentRealtimeCode(code) {
    return REALTIME_SILENT_CODES.has(code);
  }

  function realtimeErrorText(code, fallbackMessage) {
    if (isSilentRealtimeCode(code)) return "";
    if (REALTIME_ERROR_MAP[code]) return REALTIME_ERROR_MAP[code];
    const msg = String(fallbackMessage || "");
    // Never surface a bare internal wire code (e.g. an unmapped "rt_*"
    // identifier) as if it were human copy.
    if (!msg || msg === code || /^rt_[a-z_]+$/.test(msg)) {
      return "Ошибка голосового режима. Попробуйте ещё раз.";
    }
    return msg;
  }

  global.PandaCopy = {
    STATUS_LABELS,
    USER_PROGRESS,
    USER_THINKING,
    MISSING_FINAL_ANSWER,
    statusLabel,
    userFacingStatus,
    mapError,
    isSilentRealtimeCode,
    realtimeErrorText,
  };
})(window);
