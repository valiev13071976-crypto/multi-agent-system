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

  // Reference-parity closure: the Download control must render a real
  // tray-style download icon, never a Unicode arrow glyph ("⬇"/"⇩"), whose
  // appearance is inconsistent across OS/browser emoji fonts. Built via
  // createElementNS (SVG namespace) rather than innerHTML, consistent with
  // this file's "never inject raw HTML" invariant -- every node here is
  // constructed programmatically, never parsed from a string. stroke uses
  // currentColor so the icon always matches .msg-image-download-btn's own
  // `color` (see panda.css) with no separate color to keep in sync.
  function createDownloadIcon() {
    const svgNs = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNs, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    const stem = document.createElementNS(svgNs, "path");
    stem.setAttribute("d", "M12 3v11");
    const arrowhead = document.createElementNS(svgNs, "path");
    arrowhead.setAttribute("d", "M7 10l5 5 5-5");
    const tray = document.createElementNS(svgNs, "path");
    tray.setAttribute("d", "M5 20h14");
    svg.appendChild(stem);
    svg.appendChild(arrowhead);
    svg.appendChild(tray);
    return svg;
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
        } else if (/^!\[[^\]]*]\((\/[^)]+|https:\/\/[^)]+)\)$/.test(trimmed)) {
          const match = trimmed.match(/^!\[([^\]]*)]\(([^)]+)\)$/);
          const url = match[2];
          const wrap = document.createElement("span");
          wrap.className = "msg-image-wrap";
          const img = document.createElement("img");
          img.className = "msg-image";
          img.alt = match[1] || "";
          img.src = url;
          img.dataset.fullUrl = url;
          // Block 3.5.8: derive the explicit-download variant of whichever
          // image endpoint produced this URL (existing /media/{id} inline
          // rendering, or the new canonical /artifacts/{id}/view) without
          // changing the URL this <img> actually loads.
          img.dataset.downloadUrl = url.includes("/artifacts/") && url.endsWith("/view")
            ? url.replace(/\/view$/, "/download")
            : url + (url.includes("?") ? "&" : "?") + "download=1";
          wrap.appendChild(img);
          // Production acceptance defect closure / final ChatGPT 1:1 UX
          // closure: every generated image is delivered through the
          // canonical /artifacts/{id}/view route (see
          // conversation_gateway._invoke_tool), so its artifact_id is
          // recoverable straight from the URL -- no extra message field, no
          // extra request. Edit/Download render as overlay controls INSIDE
          // the image itself (bottom-left / bottom-right), matching the
          // ChatGPT reference composition, without requiring the lightbox
          // to be opened first. Images without a recoverable artifact_id
          // (e.g. any legacy /media/{version_id} link) render exactly as
          // before, unchanged.
          const artifactMatch = url.match(/\/artifacts\/([^/]+)\/view(?:[/?]|$)/);
          if (artifactMatch) {
            const artifactId = decodeURIComponent(artifactMatch[1]);
            img.dataset.artifactId = artifactId;
            wrap.classList.add("has-actions");
            // Edit: labelled control (never icon-only -- ChatGPT reference
            // shows visible "Редактировать" text), bottom-left overlay.
            const editBtn = document.createElement("button");
            editBtn.type = "button";
            editBtn.className = "msg-image-overlay msg-image-edit-btn";
            editBtn.dataset.artifactId = artifactId;
            editBtn.title = "Редактировать изображение";
            editBtn.setAttribute("aria-label", "Редактировать изображение");
            editBtn.textContent = "Редактировать";
            wrap.appendChild(editBtn);
            // Download: icon-only circular overlay, bottom-right.
            const downloadBtn = document.createElement("a");
            downloadBtn.className = "msg-image-overlay msg-image-download-btn";
            downloadBtn.href = img.dataset.downloadUrl;
            downloadBtn.setAttribute("download", "");
            downloadBtn.title = "Скачать изображение";
            downloadBtn.setAttribute("aria-label", "Скачать изображение");
            downloadBtn.appendChild(createDownloadIcon());
            wrap.appendChild(downloadBtn);
          }
          el.appendChild(wrap);
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
