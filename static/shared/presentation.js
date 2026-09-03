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

  function isTechnicalSummary(text) {
    const raw = String(text || "");
    return TECHNICAL_PATTERNS.some((re) => re.test(raw));
  }

  function stripTechnicalLines(text) {
    const lines = String(text || "").split("\n");
    const kept = lines.filter((line) => !TECHNICAL_LINE.test(line.trim()));
    return kept.join("\n").trim();
  }

  function toUserFacingSummary(text, options) {
    const opts = options || {};
    const cleaned = stripTechnicalLines(text);
    if (cleaned) return cleaned;
    if (opts.conversational) return "Готово.";
    if (opts.business) return "Задача выполнена. Подробности доступны в разделе управления.";
    return "Готово.";
  }

  function shouldShowDiagnostics(roleContext) {
    return Boolean(roleContext && roleContext.isManagement);
  }

  global.PandaPresentation = {
    isTechnicalSummary,
    stripTechnicalLines,
    toUserFacingSummary,
    shouldShowDiagnostics,
  };
})(window);
