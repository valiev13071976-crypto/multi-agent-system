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

  function renderRichText(el, text) {
    el.textContent = "";
    const blocks = String(text ?? "").split(/```/);
    blocks.forEach((block, idx) => {
      if (idx % 2 === 1) {
        const pre = document.createElement("pre");
        pre.textContent = block.replace(/^\w*\n/, "");
        el.appendChild(pre);
        return;
      }
      block.split("\n").forEach((line, lineIdx, arr) => {
        const trimmed = line.trim();
        if (/^[-*]\s+/.test(trimmed)) {
          const li = document.createElement("div");
          li.textContent = trimmed.replace(/^[-*]\s+/, "• ");
          el.appendChild(li);
        } else if (/^https?:\/\//i.test(trimmed)) {
          const a = document.createElement("a");
          a.href = trimmed;
          a.textContent = trimmed;
          a.rel = "noopener noreferrer";
          a.target = "_blank";
          el.appendChild(a);
        } else {
          if (lineIdx) el.appendChild(document.createElement("br"));
          el.appendChild(document.createTextNode(line));
        }
        if (lineIdx === arr.length - 1 && idx < blocks.length - 1 && block) {
          el.appendChild(document.createElement("br"));
        }
      });
    });
  }

  global.PandaSanitize = { escapeHtml, setText, renderMultiline, renderRichText };
})(window);
