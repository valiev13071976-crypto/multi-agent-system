/** Safe text handling — never inject raw HTML from backend/user content. */
(function (global) {
  function escapeHtml(text) {
    return String(text ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function setText(el, text) {
    el.textContent = String(text ?? "");
  }

  function renderMultiline(el, text) {
    el.textContent = "";
    String(text ?? "").split("\n").forEach((line, idx) => {
      if (idx) el.appendChild(document.createElement("br"));
      el.appendChild(document.createTextNode(line));
    });
  }

  global.PandaSanitize = { escapeHtml, setText, renderMultiline };
})(window);
