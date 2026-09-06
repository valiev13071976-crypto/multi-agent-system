/**
 * Deterministic, offline DOM-execution probe for Panda chat frontend rendering.
 *
 * Loads the ACTUAL production static/panda/js/sanitize.js and components.js
 * source files (unmodified) into a minimal hand-rolled DOM stub, invokes the
 * real exported rendering functions, and dumps a structural summary as JSON
 * so a Python test can assert on it (no jsdom/npm dependency required; no
 * network access; no browser).
 *
 * Usage: node render_probe.js <repoRoot> < cases.json > results.json
 */
"use strict";
const fs = require("fs");
const path = require("path");

const repoRoot = process.argv[2];
if (!repoRoot) {
  console.error("usage: node render_probe.js <repoRoot>");
  process.exit(2);
}

class NodeStub {
  constructor(tagName) {
    this.tagName = String(tagName || "").toLowerCase();
    this.children = [];
    this.attrs = {};
    this.dataset = {};
    this.classList = {
      add() {},
      remove() {},
      contains() {
        return false;
      },
    };
    this._className = "";
  }

  get nodeType() {
    return 1;
  }

  set className(v) {
    this._className = String(v ?? "");
  }

  get className() {
    return this._className;
  }

  appendChild(child) {
    this.children.push(child);
    child.parentNode = this;
    return child;
  }

  setAttribute(name, value) {
    this.attrs[name] = String(value);
  }

  getAttribute(name) {
    return this.attrs[name];
  }

  set textContent(value) {
    this.children = [];
    const s = value == null ? "" : String(value);
    if (s) this.children.push(new TextNodeStub(s));
  }

  get textContent() {
    return this.children.map((c) => c.textContent || "").join("");
  }
}

class TextNodeStub {
  constructor(text) {
    this.textContent = String(text ?? "");
  }

  get nodeType() {
    return 3;
  }
}

class DocumentFragmentStub extends NodeStub {
  constructor() {
    super("#fragment");
  }
}

const documentStub = {
  createElement(tag) {
    return new NodeStub(tag);
  },
  createTextNode(text) {
    return new TextNodeStub(text);
  },
  createDocumentFragment() {
    return new DocumentFragmentStub();
  },
};

global.window = global;
global.document = documentStub;

function loadScript(relPath) {
  const full = path.join(repoRoot, relPath);
  const src = fs.readFileSync(full, "utf8");
  // eslint-disable-next-line no-eval
  eval(src);
}

// PandaComponents destructures global.PandaPresentation at load time; a stub
// object is sufficient since renderMessage/renderArtifacts never call into it.
global.PandaPresentation = { isInternalMetadata: () => false };

loadScript("static/panda/js/sanitize.js");
loadScript("static/panda/js/components.js");

function serialize(node) {
  const out = { imgs: [], anchors: [], texts: [], pres: [] };
  function walk(n) {
    if (!n) return;
    if (n.nodeType === 3) {
      if (n.textContent) out.texts.push(n.textContent);
      return;
    }
    if (n.tagName === "img") {
      out.imgs.push({ src: n.src, alt: n.alt });
    }
    if (n.tagName === "a") {
      out.anchors.push({ href: n.href, text: n.textContent });
    }
    if (n.tagName === "pre") {
      out.pres.push(n.textContent);
    }
    (n.children || []).forEach(walk);
  }
  walk(node);
  return out;
}

const raw = fs.readFileSync(0, "utf8");
const input = JSON.parse(raw || "{}");
const results = [];

for (const c of input.cases || []) {
  try {
    let node;
    if (c.fn === "renderRichText") {
      const el = documentStub.createElement("div");
      global.PandaSanitize.renderRichText(el, c.args.text);
      node = el;
    } else if (c.fn === "renderMessage") {
      node = global.PandaComponents.renderMessage(c.args.role, c.args.content, c.args.meta || null);
    } else if (c.fn === "renderArtifacts") {
      node = global.PandaComponents.renderArtifacts(c.args.artifacts);
    } else {
      throw new Error("unknown fn " + c.fn);
    }
    results.push({ name: c.name, ok: true, ...serialize(node) });
  } catch (e) {
    results.push({ name: c.name, ok: false, error: String((e && e.stack) || e) });
  }
}

process.stdout.write(JSON.stringify({ results }));
