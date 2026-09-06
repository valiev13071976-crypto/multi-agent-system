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
          // Production acceptance defect closure: every generated image is
          // now delivered through the canonical /artifacts/{id}/view route
          // (see conversation_gateway._invoke_tool), so its artifact_id is
          // recoverable straight from the URL -- no extra message field, no
          // extra request. Direct Edit/Download actions render right below
          // the image itself, without requiring the lightbox to be opened
          // first (ChatGPT-like model). Images without a recoverable
          // artifact_id (e.g. any legacy /media/{version_id} link) render
          // exactly as before, unchanged.
          const artifactMatch = url.match(/\/artifacts\/([^/]+)\/view(?:[/?]|$)/);
          if (artifactMatch) {
            const artifactId = decodeURIComponent(artifactMatch[1]);
            img.dataset.artifactId = artifactId;
            wrap.classList.add("has-actions");
            const actions = document.createElement("div");
            actions.className = "msg-image-actions";
            const editBtn = document.createElement("button");
            editBtn.type = "button";
            editBtn.className = "msg-image-action msg-image-edit-btn";
            editBtn.dataset.artifactId = artifactId;
            editBtn.textContent = "✏️ Редактировать";
            actions.appendChild(editBtn);
            const downloadBtn = document.createElement("a");
            downloadBtn.className = "msg-image-action msg-image-download-btn";
            downloadBtn.href = img.dataset.downloadUrl;
            downloadBtn.setAttribute("download", "");
            downloadBtn.textContent = "⬇️ Скачать";
            actions.appendChild(downloadBtn);
            wrap.appendChild(actions);
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
