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

  global.PandaCopy = {
    STATUS_LABELS,
    USER_PROGRESS,
    USER_THINKING,
    statusLabel,
    userFacingStatus,
    mapError,
  };
})(window);
