/** Centralized Panda product branding — single source for UI labels and logo path. */
(function (global) {
  const BRAND = Object.freeze({
    name: "Panda",
    productName: "Panda AI",
    subtitle: "Business Assistant",
    logoSrc: "/static/panda/assets/panda-logo.png",
    logoAlt: "Panda — логотип",
    favicon: "/static/panda/assets/panda-logo.png",
  });

  function renderLogo(root, options) {
    const opts = options || {};
    const wrap = document.createElement("div");
    wrap.className = "brand-mark" + (opts.className ? ` ${opts.className}` : "");

    const img = document.createElement("img");
    img.src = BRAND.logoSrc;
    img.alt = BRAND.logoAlt;
    img.className = "brand-logo";
    img.width = opts.size || 36;
    img.height = opts.size || 36;
    img.loading = "lazy";
    img.decoding = "async";
    img.onerror = function () {
      img.classList.add("hidden");
      if (!wrap.querySelector(".brand-text-fallback")) {
        const fb = document.createElement("span");
        fb.className = "brand-text-fallback";
        fb.textContent = BRAND.name;
        wrap.appendChild(fb);
      }
    };

    const text = document.createElement("div");
    text.className = "brand-text";
    const title = document.createElement("strong");
    title.className = "brand-title";
    title.textContent = opts.title || BRAND.productName;
    text.appendChild(title);
    if (opts.showSubtitle !== false) {
      const sub = document.createElement("span");
      sub.className = "brand-subtitle";
      sub.textContent = opts.subtitle || BRAND.subtitle;
      text.appendChild(sub);
    }

    wrap.appendChild(img);
    wrap.appendChild(text);
    if (root) root.appendChild(wrap);
    return wrap;
  }

  function applyDocumentBrand() {
    document.title = `${BRAND.productName} — ${BRAND.subtitle}`;
    let link = document.querySelector('link[rel="icon"]');
    if (!link) {
      link = document.createElement("link");
      link.rel = "icon";
      document.head.appendChild(link);
    }
    link.href = BRAND.favicon;
  }

  global.PandaBrand = { BRAND, renderLogo, applyDocumentBrand };
})(window);
