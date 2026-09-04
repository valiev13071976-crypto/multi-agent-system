/** Product presentation layer — strip workflow diagnostics from user-facing surfaces. */
(function (global) {
  const TECHNICAL_LINE = /^(Requested:|Findings:|Artifacts:|Published:|Fixture_mode:|Approved:|Waiting_approval:|Status:|Recipe:|Mode:|Trace ID:|workflow_id|execution_id|provider)/i;

  const TECHNICAL_PATTERNS = [
    /^Requested:\s/m,
    /^Findings:\s/m,
    /^Artifacts:\s/m,
    /^Published:\s/m,
    /^Fixture_mode:\s/m,
    /^Approved:\s/m,
    /^Waiting_approval:\s/m,
  ];

  const INTERNAL_MARKERS = [
    "синтез ответов экспертов без скрытого приоритета",
    "внешняя проверка фактов учитывается только при независимых источниках",
    "финальный анализ успешно сформирован",
    "использовать решение, подтвержденное большинством экспертов",
    "без скрытого приоритета provider",
  ];

  function isTechnicalSummary(text) {
    const raw = String(text || "");
    return TECHNICAL_PATTERNS.some((re) => re.test(raw));
  }

  function isInternalMetadata(text) {
    const raw = String(text || "").trim();
    if (!raw) return true;
    const low = raw.toLowerCase();
    return INTERNAL_MARKERS.some((m) => low.includes(m));
  }

  function stripTechnicalLines(text) {
    const lines = String(text || "").replace(/ \| /g, "\n").split("\n");
    const kept = lines.filter((line) => !TECHNICAL_LINE.test(line.trim()));
    return kept.join("\n").trim();
  }

  function selectCanonicalFinalAnswer(result) {
    const payload = result || {};
    const keys = ["final_answer", "answer", "text", "reply", "best_solution", "summary", "analysis"];
    for (let i = 0; i < keys.length; i += 1) {
      const raw = String(payload[keys[i]] || "").trim();
      if (!raw || isInternalMetadata(raw)) continue;
      const stripped = stripTechnicalLines(raw);
      if (stripped && !isInternalMetadata(stripped)) return stripped;
    }
    return "";
  }

  function toUserFacingSummary(text, options) {
    const opts = options || {};
    if (isInternalMetadata(text)) {
      return "";
    }
    const cleaned = stripTechnicalLines(text);
    if (cleaned && !isInternalMetadata(cleaned)) return cleaned;
    if (opts.conversational) return "";
    if (opts.business) return "Задача выполнена. Подробности доступны в разделе управления.";
    return "";
  }

  function shouldShowDiagnostics(roleContext) {
    return Boolean(roleContext && roleContext.isManagement);
  }

  global.PandaPresentation = {
    isTechnicalSummary,
    isInternalMetadata,
    stripTechnicalLines,
    selectCanonicalFinalAnswer,
    toUserFacingSummary,
    shouldShowDiagnostics,
  };
})(window);
