/* foreman frontend: terminal-adjacent transcript, no chat bubbles. Vanilla JS. */
(function () {
  "use strict";

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // --- markdown (assistant text) --------------------------------------------
  let markedReady = false;
  function setupMarked() {
    if (markedReady || typeof marked === "undefined") return;
    const renderer = new marked.Renderer();
    // Drop raw HTML by escaping it to visible text (no HTML injection).
    renderer.html = function (token) {
      const raw = typeof token === "string" ? token : token.text || "";
      return escapeHtml(raw);
    };
    marked.setOptions({ renderer: renderer, breaks: true, gfm: true, headerIds: false, mangle: false });
    markedReady = true;
  }
  function renderMarkdown(text) {
    setupMarked();
    if (typeof marked === "undefined") return escapeHtml(text);
    try {
      return marked.parse(text || "");
    } catch (_e) {
      return escapeHtml(text);
    }
  }

  // --- lazy vendored renderers (highlight.js / KaTeX / mermaid) --------------
  // Each library is a heavy vendored asset, so we inject it only the first time
  // matching content actually appears. Loaders are memoized by URL.
  const _assetPromises = {};
  function loadScript(src) {
    if (_assetPromises[src]) return _assetPromises[src];
    _assetPromises[src] = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = src;
      s.onload = () => resolve();
      s.onerror = () => reject(new Error("failed to load " + src));
      document.head.appendChild(s);
    });
    return _assetPromises[src];
  }
  function loadStyle(href) {
    if (_assetPromises["css:" + href]) return;
    _assetPromises["css:" + href] = true;
    const l = document.createElement("link");
    l.rel = "stylesheet";
    l.href = href;
    document.head.appendChild(l);
  }

  // Warm the render libs during idle right after page load, so by the time the
  // user scrolls to code/math/diagram content the lib is already cached and
  // rendering is instant (non-blocking; loaders are memoized). Combined with the
  // server's immutable cache headers this is a one-time cost per device.
  let _prefetchedRenderLibs = false;
  function prefetchRenderLibs() {
    if (_prefetchedRenderLibs) return;
    _prefetchedRenderLibs = true;
    loadStyle("/static/vendor/highlight-atom-one-dark.min.css");
    loadScript("/static/vendor/highlight.min.js").catch(() => {});
    loadStyle("/static/vendor/katex/katex.min.css");
    loadScript("/static/vendor/katex/katex.min.js")
      .then(() => loadScript("/static/vendor/katex/auto-render.min.js"))
      .catch(() => {});
    loadScript("/static/vendor/mermaid.min.js").catch(() => {});
  }
  function schedulePrefetch() {
    if (window.requestIdleCallback) window.requestIdleCallback(prefetchRenderLibs, { timeout: 3000 });
    else setTimeout(prefetchRenderLibs, 1500);
  }

  // ```mermaid fences -> rendered diagrams. Must run BEFORE syntax highlighting
  // so the mermaid source isn't highlighted as code.
  function renderMermaidIn(container) {
    const codes = container.querySelectorAll("code.language-mermaid");
    if (!codes.length) return;
    const divs = [];
    codes.forEach((code) => {
      const host = code.closest("pre") || code;
      const div = document.createElement("div");
      div.className = "mermaid";
      div.textContent = code.textContent;
      host.replaceWith(div);
      divs.push(div);
    });
    loadScript("/static/vendor/mermaid.min.js")
      .then(() => {
        if (!window._foremanMermaidInit) {
          window.mermaid.initialize({ startOnLoad: false, theme: "dark", securityLevel: "strict" });
          window._foremanMermaidInit = true;
        }
        return window.mermaid.run({ nodes: divs });
      })
      .catch(() => {
        divs.forEach((d) => { if (!d.querySelector("svg")) d.classList.add("mermaid-error"); });
      });
  }

  function highlightCodeIn(container) {
    const blocks = container.querySelectorAll("pre code");
    if (!blocks.length) return;
    loadStyle("/static/vendor/highlight-atom-one-dark.min.css");
    loadScript("/static/vendor/highlight.min.js")
      .then(() => {
        blocks.forEach((b) => {
          if (b.dataset.highlighted) return;
          try { window.hljs.highlightElement(b); } catch (_e) { /* ignore */ }
        });
      })
      .catch(() => {});
  }

  // Only load KaTeX when the text looks like real math -- a $$..$$ / \(..\) / \[..\]
  // block, or a $..$ span that contains a LaTeX-ish char -- so plain "$5 and $10"
  // currency never triggers it (and auto-render's balanced matching does the rest).
  const _MATH_SIGNAL = /\$\$[\s\S]+?\$\$|\\\(|\\\[|\$[^$\n]*[\\^_{}][^$\n]*\$/;
  function renderMathIn(container) {
    if (!_MATH_SIGNAL.test(container.textContent || "")) return;
    loadStyle("/static/vendor/katex/katex.min.css");
    loadScript("/static/vendor/katex/katex.min.js")
      .then(() => loadScript("/static/vendor/katex/auto-render.min.js"))
      .then(() => {
        try {
          window.renderMathInElement(container, {
            delimiters: [
              { left: "$$", right: "$$", display: true },
              { left: "\\[", right: "\\]", display: true },
              { left: "\\(", right: "\\)", display: false },
              { left: "$", right: "$", display: false },
            ],
            throwOnError: false,
            ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
          });
        } catch (_e) { /* ignore */ }
      })
      .catch(() => {});
  }

  // Make every link in assistant markdown open safely in a new tab.
  function fixLinksIn(container) {
    container.querySelectorAll("a[href]").forEach((a) => {
      a.target = "_blank";
      a.rel = "noopener noreferrer";
    });
  }

  // Rich-render a rendered assistant markdown container: mermaid, then syntax
  // highlight, then math, then safe links.
  function enrichAssistant(container) {
    renderMermaidIn(container);
    highlightCodeIn(container);
    renderMathIn(container);
    fixLinksIn(container);
  }

  // Append text to an element, turning bare URLs into safe links (for plain-text
  // tool output). Cheap: one regex split, text nodes + anchors, no HTML parsing.
  const _URL_RE = /(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;
  function appendLinkified(parent, text) {
    const parts = String(text == null ? "" : text).split(_URL_RE);
    for (let i = 0; i < parts.length; i++) {
      if (i % 2 === 1) {
        const a = document.createElement("a");
        a.className = "autolink";
        a.href = parts[i];
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.textContent = parts[i];
        parent.appendChild(a);
      } else if (parts[i]) {
        parent.appendChild(document.createTextNode(parts[i]));
      }
    }
  }

  // --- connection status ----------------------------------------------------
  // Also colours the top-bar liveness dot: green=live, red=offline/closed/error,
  // amber(pulsing)=anything in between (connecting/reconnecting/polling).
  function setConn(state) {
    const c = document.getElementById("conn");
    if (!c) return;
    c.textContent = state;
    const live = state === "live";
    const off = state === "offline" || state === "closed" || state === "error";
    c.classList.toggle("live", live);
    c.classList.toggle("offline", off);
    c.classList.toggle("reconnecting", !live && !off);
  }

  // --- live agent state -> tab title ----------------------------------------
  // Prefix the tab title with the agent's live state so background tabs show
  // status at a glance. Mapping: blocked (dialog/permissions) -> NEEDS INPUT,
  // busy/RUNNING -> WORKING, otherwise (running & idle) -> WAITING.
  function statusTitle(d, base) {
    let prefix = "";
    if (d && d.running) {
      if (d.blocked) prefix = "[NEEDS INPUT] ";
      else if (d.busy) prefix = "[WORKING] ";
      else prefix = "[WAITING] ";
    }
    return prefix + base;
  }

  // Poll an agent's input-state and call onState(d) each tick, at a steady rate
  // (faster in a short burst right after a send).
  function installStatePolling(agentName, onState) {
    let timer = null;
    let burstUntil = 0;
    function tick() {
      fetch("/api/agents/" + encodeURIComponent(agentName) + "/input-state")
        .then((r) => r.json())
        .then(onState)
        .catch(() => {});
    }
    function interval() {
      return Date.now() < burstUntil ? 800 : 4000;
    }
    function schedule() {
      if (timer) clearInterval(timer);
      timer = setInterval(tick, interval());
    }
    // After a send, poll input-state rapidly for a short window so the composer's
    // working/blocked state (and any API-key/permission dialog) shows up fast.
    function burst() {
      burstUntil = Date.now() + 12000;
      tick();
      schedule();
      setTimeout(schedule, 12000);
    }
    tick();
    schedule();
    return { burst: burst };
  }

  // ==========================================================================
  // Index page: agent list
  // ==========================================================================
  function initIndex() {
    document.title = "foreman — home";
    const listEl = document.getElementById("list");

    function stateClass(state) {
      return String(state || "").toLowerCase();
    }
    function relTime(iso) {
      if (!iso) return "";
      const d = new Date(iso);
      const secs = Math.max(0, (Date.now() - d.getTime()) / 1000);
      if (secs < 60) return Math.floor(secs) + "s ago";
      if (secs < 3600) return Math.floor(secs / 60) + "m ago";
      if (secs < 86400) return Math.floor(secs / 3600) + "h ago";
      return Math.floor(secs / 86400) + "d ago";
    }
    function cardEl(a) {
      const card = el("a", "agent-card");
      card.href = "/a/" + encodeURIComponent(a.name);
      const row1 = el("div", "row1");
      row1.appendChild(el("span", "name", a.name));
      row1.appendChild(el("span", "chip " + stateClass(a.state), a.state));
      card.appendChild(row1);
      const row2 = el("div", "row2");
      row2.appendChild(el("span", null, a.type));
      row2.appendChild(el("span", null, a.host_name + " · " + a.provider));
      if (a.activity_time) row2.appendChild(el("span", null, relTime(a.activity_time)));
      // A plain shell on this host (VS-Code-Remote style). The card is itself an
      // <a>, so this is a span that navigates on click and stops the card link.
      const shell = el("span", "host-shell", "shell ›");
      shell.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        location.href = "/h/" + encodeURIComponent(a.host_name) + "/terminal";
      });
      row2.appendChild(shell);
      card.appendChild(row2);
      return card;
    }
    function renderAll(agents) {
      listEl.innerHTML = "";
      if (!agents.length) {
        listEl.appendChild(el("div", "empty", "no agents"));
        return;
      }
      agents.forEach((a) => listEl.appendChild(cardEl(a)));
    }

    let pollTimer = null;
    function startPolling() {
      if (pollTimer) return;
      const tick = () =>
        fetch("/api/agents")
          .then((r) => r.json())
          .then((d) => renderAll(d.agents || []))
          .catch(() => {});
      tick();
      pollTimer = setInterval(tick, 5000);
      setConn("polling");
    }
    function stopPolling() {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    }

    // One long-lived SSE that stays open; each message is a full snapshot the
    // registry pushes whenever the live-agent set changes.
    let es = null;
    function connect() {
      if (typeof EventSource === "undefined") { startPolling(); return; }
      if (es) return;
      es = new EventSource("/api/agents/stream");
      es.onopen = () => { stopPolling(); setConn("live"); };
      es.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch (_e) { return; }
        if (msg.type === "snapshot") renderAll(msg.agents || []);
      };
      es.onerror = () => { setConn("reconnecting"); startPolling(); };
    }
    connect();
  }

  // ==========================================================================
  // Agent page: transcript + composer
  // ==========================================================================
  function agentNameFromPath() {
    const m = location.pathname.match(/^\/a\/(.+)$/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function initAgent() {
    const name = agentNameFromPath();
    document.getElementById("agent-name").textContent = name;
    document.title = name + " — chat";
    const termlink = document.getElementById("termlink");
    if (termlink) termlink.href = "/a/" + encodeURIComponent(name) + "/terminal";
    const tEl = document.getElementById("transcript");
    const composer = document.getElementById("composer");
    const toolBodies = new Map(); // tool_call_id -> {body, toolName, resolved}
    const pendingResults = new Map(); // tool_call_id -> result event (arrived early)
    // Optimistic send: on send we render the message bubble immediately (class
    // "pending") and clear the composer at once, so the send feels instant. The
    // transcript echo later confirms it IN PLACE (pending class removed) rather
    // than duplicating, since userBubbles dedups by content-key. A failed POST
    // rolls the bubble back and restores the text. userBubbles also flips a
    // queued_command (purple) to a delivered user_message (green) in place.
    const userBubbles = new Map(); // content-key -> { node, queued, pending }
    let pendingSend = null; // text of a sent message awaiting its transcript echo
    let pendingSendTimer = null; // safety: confirm the pending bubble if echo never matches
    // The upload token: "[FILE: ./chat_uploads/<uuid>.<ext>]". Captured group 1
    // is the stored name. Used to render chips in the transcript and to delete a
    // whole token on backspace. Build a fresh RegExp per use to reset lastIndex.
    const FILE_TOKEN_SRC = "\\[FILE:\\s*\\./chat_uploads/([A-Za-z0-9._-]+)\\]";
    const IMAGE_EXTS = { png: 1, jpg: 1, jpeg: 1, gif: 1, webp: 1, bmp: 1, svg: 1 };

    // The middle column (.wrap) is the scroll container now (the shell is a
    // fixed-height flex column), so scrolling is measured on it, not the window.
    const scroller = document.querySelector(".wrap") || document.scrollingElement || document.documentElement;
    // Stick-to-bottom: follow new content ONLY while the user is at the bottom.
    // Scrolling up detaches (they're reading history); scrolling back down to the
    // bottom re-attaches. Starts attached, so a freshly opened chat lands on the
    // newest message.
    let stick = true;
    function atBottom() {
      return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
    }
    function scrollToBottom() { scroller.scrollTop = scroller.scrollHeight; }
    function scrollDown(force) {
      if (force) stick = true;
      if (stick) scrollToBottom();
    }
    scroller.addEventListener("scroll", () => { stick = atBottom(); }, { passive: true });
    // Follow late-growing content too -- streaming assistant text, and images /
    // mermaid / katex that finish loading after their event first rendered --
    // whenever we're still attached to the bottom.
    if (typeof ResizeObserver !== "undefined") {
      new ResizeObserver(() => { if (stick) scrollToBottom(); }).observe(tEl);
    }

    // ---- diff rendering (client-side, no lib) ----
    function diffBlock(toolName, input) {
      const wrap = el("div", "diff");
      const file = input.file_path || input.notebook_path || "";
      wrap.appendChild(el("div", "dhead", file || toolName));
      function addLines(text, cls) {
        String(text == null ? "" : text)
          .split("\n")
          .forEach((line) => {
            const d = el("span", "dline " + cls);
            d.textContent = (cls === "add" ? "+ " : cls === "del" ? "- " : "  ") + line;
            wrap.appendChild(d);
          });
      }
      if (toolName === "Write") {
        addLines(input.content, "add");
      } else if (toolName === "MultiEdit" && Array.isArray(input.edits)) {
        input.edits.forEach((e, i) => {
          if (i > 0) wrap.appendChild(el("span", "dline ctx", ""));
          addLines(e.old_string, "del");
          addLines(e.new_string, "add");
        });
      } else {
        // Edit / NotebookEdit
        addLines(input.old_string, "del");
        addLines(input.new_string != null ? input.new_string : input.new_source, "add");
      }
      return wrap;
    }

    const DIFF_TOOLS = { Edit: 1, Write: 1, MultiEdit: 1, NotebookEdit: 1 };

    function toolCallEl(tc) {
      const details = el("details", "tool");
      const summary = el("summary");
      summary.appendChild(el("span", "tname", tc.tool_name));
      const arg = argSummary(tc);
      if (arg) summary.appendChild(el("span", "targ", arg));
      details.appendChild(summary);
      const body = el("div", "body");
      details.appendChild(body);

      // Collapsed by default (tools + diffs); one tap expands. Assistant prose
      // stays expanded -- only tool calls/results and framework lines collapse.
      if (DIFF_TOOLS[tc.tool_name] && tc.input_full) {
        body.appendChild(diffBlock(tc.tool_name, tc.input_full));
      } else if (tc.input_full) {
        const pre = el("pre");
        pre.textContent = prettyInput(tc.input_full);
        body.appendChild(pre);
      }
      toolBodies.set(tc.tool_call_id, { body: body, toolName: tc.tool_name, details: details });
      // A result that arrived before its call.
      if (pendingResults.has(tc.tool_call_id)) {
        attachResult(pendingResults.get(tc.tool_call_id));
        pendingResults.delete(tc.tool_call_id);
      }
      return details;
    }

    function argSummary(tc) {
      const inp = tc.input_full || {};
      if (inp.file_path) return inp.file_path;
      if (inp.command) return String(inp.command).split("\n")[0];
      if (inp.description) return inp.description;
      if (inp.pattern) return inp.pattern;
      if (inp.path) return inp.path;
      return tc.input_preview || "";
    }
    function prettyInput(inp) {
      if (typeof inp === "string") return inp;
      if (inp.command) return inp.command;
      try { return JSON.stringify(inp, null, 2); } catch (_e) { return String(inp); }
    }

    // Source for a transcript image: inline base64 when the server left it in the
    // event, else the /timage endpoint (large images are served by reference).
    function imageSrc(img) {
      if (img.data) return "data:" + (img.media_type || "image/png") + ";base64," + img.data;
      return "/api/agents/" + encodeURIComponent(name) + "/timage/" + encodeURIComponent(img.id || "");
    }
    // A transcript image (from a tool result or a pasted message): inline, tap to
    // expand via the same overlay as upload thumbnails.
    function toolImageEl(img) {
      const src = imageSrc(img);
      const image = el("img", "tool-image");
      image.src = src;
      image.loading = "lazy";
      image.alt = "image";
      image.addEventListener("click", function () { openLightbox(src); });
      return image;
    }

    function attachResult(ev) {
      const entry = toolBodies.get(ev.tool_call_id);
      const out = ev.output || "";
      if (out) {
        const pre = el("pre", "result" + (ev.is_error ? " error" : ""));
        appendLinkified(pre, out);
        // Long outputs collapse behind a summary; short ones show inline.
        if (out.length > 1200) {
          const det = el("details", "tool");
          const sm = el("summary");
          sm.appendChild(el("span", "tname", "output"));
          sm.appendChild(el("span", "targ", out.length + " chars"));
          det.appendChild(sm);
          const b = el("div", "body");
          b.appendChild(pre);
          det.appendChild(b);
          if (entry) entry.body.appendChild(det);
        } else if (entry) {
          entry.body.appendChild(pre);
        }
      }
      // Inline any images the tool returned (image-only results have empty output).
      if (entry && ev.images && ev.images.length) {
        ev.images.forEach(function (img) { entry.body.appendChild(toolImageEl(img)); });
      }
    }

    // "Working" indicator: a pulsing dot that also anchors the queue -- accepted
    // messages sit ABOVE it, still-queued messages BELOW it (mirrors the terminal).
    // Driven SOLELY by mngr's live state (RUNNING == working) -- the SAME
    // authoritative signal the homescreen chips use (is_busy_state, set by claude's
    // own UserPromptSubmit/Stop hooks). No transcript-tail guessing, so no "dot on
    // when idle" / "dot stuck after interrupt" mismatches. `busy` arrives from the
    // input-state poll (0.8s bursts after a send, 4s at rest).
    let working = false;
    const escBtn = document.getElementById("esc");
    const workingEl = el("div", "working");
    workingEl.hidden = true;
    workingEl.appendChild(el("span", "dot"));
    workingEl.appendChild(el("span", null, "working…"));
    // The dot lives permanently in the transcript as a fixed anchor: main content
    // is inserted BEFORE it, queued bubbles are appended AFTER it. Just toggle its
    // visibility; never move it.
    tEl.appendChild(workingEl);

    // mngr's busy flag is the whole story. null/undefined (no state yet) leaves it
    // unchanged; true/false set it directly.
    function applyMngrBusy(busy) {
      if (busy !== true && busy !== false) return;
      if (busy === working) return;
      working = busy;
      refreshWorking();
    }
    function refreshWorking() {
      // BLOCKED (a ❯ dialog / mngr PERMISSIONS) beats "working" -- never show the
      // dot while blocked. The dot stays put (fixed anchor); only visibility flips.
      workingEl.hidden = !working || blocked;
      if (escBtn) escBtn.disabled = false; // always usable; Escape is harmless
    }

    // A framework/meta record (a /command, its stdout, or an isMeta line):
    // a dim, collapsed one-liner -- never a user/assistant bubble.
    function frameworkEl(ev) {
      const details = el("details", "framework");
      const summary = el("summary");
      summary.appendChild(el("span", "fw-tag", "framework"));
      summary.appendChild(el("span", "fw-label", ev.label || ""));
      details.appendChild(summary);
      const body = el("div", "fw-body");
      const pre = el("pre");
      pre.textContent = ev.detail || ev.label || "";
      body.appendChild(pre);
      details.appendChild(body);
      return details;
    }

    // Full-screen image overlay (tap a transcript image thumbnail to expand).
    function openLightbox(url) {
      const ov = el("div", "lightbox");
      const img = el("img");
      img.src = url;
      ov.appendChild(img);
      ov.addEventListener("click", function () { ov.remove(); });
      document.body.appendChild(ov);
    }

    // A [FILE: ...] token rendered as a chip: image -> inline thumbnail (tap to
    // expand); other files -> an icon+name link to the serve endpoint.
    function fileChip(storedName) {
      const url = "/api/agents/" + encodeURIComponent(name) + "/upload/" + encodeURIComponent(storedName);
      const ext = (storedName.split(".").pop() || "").toLowerCase();
      if (IMAGE_EXTS[ext]) {
        const wrap = el("span", "file-chip img-chip");
        const img = el("img");
        img.src = url;
        img.alt = storedName;
        img.loading = "lazy";
        img.addEventListener("click", function () { openLightbox(url); });
        img.addEventListener("error", function () {
          wrap.className = "file-chip missing";
          wrap.textContent = "[missing file]";
        });
        wrap.appendChild(img);
        return wrap;
      }
      const a = el("a", "file-chip file-doc");
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener";
      a.appendChild(el("span", "fc-icon", "FILE"));
      a.appendChild(el("span", "fc-name", storedName));
      return a;
    }

    // Render user text, replacing [FILE: ...] tokens with chips (text in between
    // is preserved). Used for both delivered and queued user messages.
    function renderUserContent(container, text) {
      const re = new RegExp(FILE_TOKEN_SRC, "g");
      let last = 0;
      let m;
      while ((m = re.exec(text)) !== null) {
        if (m.index > last) container.appendChild(document.createTextNode(text.slice(last, m.index)));
        container.appendChild(fileChip(m[1]));
        last = m.index + m[0].length;
      }
      if (last < text.length) container.appendChild(document.createTextNode(text.slice(last)));
    }

    // Position relative to the working dot: main content goes ABOVE it (normal
    // history / current turn), queued messages go BELOW it (still in the queue).
    function addMain(node) { tEl.insertBefore(node, workingEl); }
    function addQueued(node) { tEl.appendChild(node); }
    // Queued (dark green, below dot) -> accepted (full green, above dot), in place.
    function promoteBubble(rec) {
      if (!rec || !rec.queued) return;
      rec.node.classList.remove("queued");
      rec.queued = false;
      tEl.insertBefore(rec.node, workingEl); // move above the dot into history
    }

    function renderEvent(ev) {
      if (ev.type === "user_message") {
        const key = (ev.content || "").trim();
        // The transcript is the source of truth. The instant the message we sent
        // shows up here (as an enqueue OR a delivered turn) it has "landed" --
        // clear the box and unlock the composer for the next one.
        if (pendingSend !== null && key === pendingSend) confirmSend();
        const existing = userBubbles.get(key);
        if (ev.queued) {
          // Still in claude's queue -> dark-green bubble BELOW the dot.
          if (existing) {
            // Re-assertion (e.g. the late queued_command attachment for the same
            // text): only merge images it carries; never un-promote an accepted one.
            if (existing.queued && ev.images && ev.images.length) {
              ev.images.forEach((img) => existing.node.appendChild(toolImageEl(img)));
            }
          } else {
            const e = el("div", "entry user queued");
            renderUserContent(e, ev.content || "");
            if (ev.images && ev.images.length) ev.images.forEach((img) => e.appendChild(toolImageEl(img)));
            addQueued(e);
            userBubbles.set(key, { node: e, queued: true });
          }
        } else if (existing) {
          // The delivered turn for a message we already showed queued (the
          // interrupt/Esc path writes such a line) -> promote in place.
          promoteBubble(existing);
        } else {
          // A normal delivered turn (sent while idle) -> full-green, above the dot.
          const e = el("div", "entry user");
          renderUserContent(e, ev.content || "");
          if (ev.images && ev.images.length) ev.images.forEach((img) => e.appendChild(toolImageEl(img)));
          addMain(e);
          userBubbles.set(key, { node: e, queued: false });
        }
      } else if (ev.type === "queue_accepted") {
        // claude pulled this message off the queue (turn boundary or interrupt).
        promoteBubble(userBubbles.get(ev.key));
      } else if (ev.type === "queue_removed") {
        // The user yanked the queue back to edit (popAll) -> drop the bubble.
        const rec = userBubbles.get(ev.key);
        if (rec && rec.queued) { rec.node.remove(); userBubbles.delete(ev.key); }
      } else if (ev.type === "framework_message") {
        addMain(frameworkEl(ev));
      } else if (ev.type === "assistant_message") {
        const e = el("div", "entry assistant");
        if (ev.text && ev.text.trim()) {
          const md = el("div", "assistant-md");
          md.innerHTML = renderMarkdown(ev.text);
          enrichAssistant(md); // syntax highlight, math, mermaid, safe links
          e.appendChild(md);
        }
        (ev.tool_calls || []).forEach((tc) => e.appendChild(toolCallEl(tc)));
        if (e.childNodes.length) addMain(e);
      } else if (ev.type === "tool_result") {
        if (toolBodies.has(ev.tool_call_id)) attachResult(ev);
        else pendingResults.set(ev.tool_call_id, ev);
      }
      scrollDown();
    }

    function setStatus(text) {
      let s = document.getElementById("stat");
      if (!s) { s = el("div", "status-line"); s.id = "stat"; addMain(s); }
      s.textContent = text;
    }
    function clearStatus() {
      const s = document.getElementById("stat");
      if (s) s.remove();
    }

    // ---- transcript SSE ----
    // A long-lived connection that stays open. On an automatic EventSource
    // reconnect the server re-backfills from the start; every event carries an
    // event_id, so we dedup client-side and only render what we haven't seen.
    const seenEventIds = new Set();
    let es = null;
    // ---- liveness ----
    // The server pushes a heartbeat event every ~5s. We time them: if the stream
    // goes quiet the browser may not notice a dead socket, so a watchdog flips the
    // indicator to amber/red and force-reconnects, and LOCKS the composer so
    // nothing is ever sent into a dead stream. The transcript keeps showing its
    // last-known state meanwhile.
    let lastMsgAt = Date.now();
    let connLive = false;
    let lastReconnectAt = 0;
    const STALE_MS = 14000; // ~3 missed heartbeats -> stream is stale
    const OFFLINE_MS = 30000; // still nothing -> declare offline

    function setConnState(state) {
      setConn(state); // top-bar dot colour + text
      connLive = state === "live";
      composer.classList.toggle("offline", !connLive);
    }
    function markLive() {
      lastMsgAt = Date.now();
      if (!connLive) setConnState("live");
    }
    function forceReconnect() {
      const now = Date.now();
      if (now - lastReconnectAt < 5000) return; // don't storm reconnects
      lastReconnectAt = now;
      if (es) { try { es.close(); } catch (_e) {} es = null; }
      connect(true);
    }

    function connect(isReconnect) {
      if (typeof EventSource === "undefined") {
        setStatus("Live transcript needs EventSource support.");
        composer.hidden = false;
        return;
      }
      if (es) return; // already connected
      if (!isReconnect) setStatus("loading transcript…");
      es = new EventSource("/api/agents/" + encodeURIComponent(name) + "/transcript");
      es.onopen = () => markLive();
      es.onmessage = (raw) => {
        markLive(); // any frame (incl. heartbeat) proves the stream is alive
        let msg;
        try { msg = JSON.parse(raw.data); } catch (_e) { return; }
        if (msg.type === "heartbeat") return; // liveness only, nothing to render
        if (msg.type === "event") {
          const id = msg.event && msg.event.event_id;
          if (id) {
            if (seenEventIds.has(id)) return; // already rendered (dedup re-backfill)
            seenEventIds.add(id);
          }
          renderEvent(msg.event);
        } else if (msg.type === "backfill_complete") {
          clearStatus();
          composer.hidden = false;
          scrollDown(true);
        } else if (msg.type === "unsupported") {
          clearStatus();
          addMain(el("div", "unsupported", "No transcript for agent type '" + msg.agent_type + "'."));
        } else if (msg.type === "error") {
          setStatus(msg.message || "error");
          composer.hidden = false;
        }
      };
      es.onerror = () => { if (connLive) setConnState("reconnecting"); };
    }
    connect(false);
    setInterval(() => {
      const gap = Date.now() - lastMsgAt;
      if (gap < STALE_MS) { if (!connLive) setConnState("live"); return; }
      setConnState(gap < OFFLINE_MS ? "reconnecting" : "offline");
      forceReconnect();
    }, 3000);
    schedulePrefetch(); // warm highlight/katex/mermaid during idle

    // ---- composer ----
    const input = document.getElementById("input");
    const sendBtn = document.getElementById("send");
    const sendErr = document.getElementById("send-error");
    const composerBlocked = document.getElementById("composer-blocked");
    const blockedTermlink = document.getElementById("blocked-termlink");
    const termUrl = "/a/" + encodeURIComponent(name) + "/terminal";
    if (blockedTermlink) blockedTermlink.href = termUrl;

    // ---- blocking-dialog state: one generic greyed state, point at terminal ----
    let blocked = false;
    function setBlocked() {
      blocked = true;
      composer.classList.add("blocked");
      if (composerBlocked) composerBlocked.hidden = false;
      refreshWorking(); // BLOCKED hides the dot immediately, not on the next event
    }
    function clearBlocked() {
      blocked = false;
      composer.classList.remove("blocked");
      if (composerBlocked) composerBlocked.hidden = true;
      refreshWorking(); // unblocking may reveal the dot again if still working
    }

    // Poll input-state: drives the composer's blocked/working UI and the tab
    // title. Keeps polling (slower) while hidden so the title stays live in a
    // background tab. Each poll is a tmux pane capture over SSH.
    const statePoll = installStatePolling(name, (d) => {
      document.title = statusTitle(d, name + " — chat");
      if (d.blocked) setBlocked(); else clearBlocked();
      applyMngrBusy(d.busy);
    });

    // Grow with the text up to ~5 lines, then stop and let the textarea scroll --
    // on both desktop and phone (line-count cap, not a viewport fraction).
    function autoGrow() {
      input.style.height = "auto";
      const cs = getComputedStyle(input);
      const line = parseFloat(cs.lineHeight) || 21;
      const pad = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
      const maxH = line * 5 + pad;
      input.style.height = Math.min(input.scrollHeight, maxH) + "px";
      input.style.overflowY = input.scrollHeight > maxH ? "auto" : "hidden";
    }
    input.addEventListener("input", autoGrow);

    function showError(text) {
      sendErr.hidden = false;
      sendErr.innerHTML = escapeHtml(text) + '  ·  <a href="#" id="retry">dismiss</a>';
      const r = document.getElementById("retry");
      if (r) r.onclick = (e) => { e.preventDefault(); sendErr.hidden = true; };
    }

    // ---- send lifecycle (the transcript is the source of truth) ----
    // We do NOT optimistically draw a bubble. On send the message stays in the
    // textbox and the whole input region locks ("awaiting") until the transcript
    // echoes it back -- queued (purple) or delivered (green). Only then do we
    // clear the box. So a bubble exists only once claude has actually recorded the
    // message: no guessing, no colour flip-flop.
    function setAwaiting(on) {
      composer.classList.toggle("awaiting", on);
      input.disabled = on;
      sendBtn.disabled = on;
    }
    // Message landed in the transcript: clear the box + unlock for the next one.
    function confirmSend() {
      if (pendingSendTimer) { clearTimeout(pendingSendTimer); pendingSendTimer = null; }
      pendingSend = null;
      input.value = "";
      setAwaiting(false);
      autoGrow();
      clearUploads();
      input.focus();
    }
    // Give the input back WITHOUT clearing (failed POST): keep the typed text so
    // the user can fix and retry, and restore usability.
    function releaseSend() {
      if (pendingSendTimer) { clearTimeout(pendingSendTimer); pendingSendTimer = null; }
      pendingSend = null;
      setAwaiting(false);
      input.focus();
    }

    function send() {
      if (composer.classList.contains("awaiting")) return; // already waiting on one
      if (!connLive) { showError("not connected — waiting to reconnect…"); return; }
      const msg = input.value.trim();
      if (!msg) return;
      sendErr.hidden = true;
      // Lock the input region and keep the text visible while we wait for the
      // transcript to confirm the message actually landed (confirmSend, above).
      pendingSend = msg;
      setAwaiting(true);
      if (pendingSendTimer) clearTimeout(pendingSendTimer);
      fetch("/api/agents/" + encodeURIComponent(name) + "/message", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: msg }),
      })
        .then((r) => r.json().then((d) => ({ ok: r.ok, d: d })))
        .then(({ ok, d }) => {
          if (ok && d.ok) {
            clearBlocked();
            statePoll.burst(); // fast input-state polling so working/blocked reacts
            // Keep waiting for the transcript echo. Safety net: if it never matches
            // (content normalized, or an agent type with no live transcript), clear
            // anyway after a grace period so we don't stay locked forever.
            if (pendingSendTimer) clearTimeout(pendingSendTimer);
            pendingSendTimer = setTimeout(() => { if (pendingSend === msg) confirmSend(); }, 15000);
          } else {
            const err = (d && d.error) || "send failed — open the terminal to resolve any prompt.";
            releaseSend();
            showError(err);
            // A failed send usually means a blocking dialog ate the paste; flip
            // to the greyed state immediately (the next poll re-confirms/clears).
            setBlocked();
          }
        })
        .catch((e) => {
          releaseSend();
          showError("network error: " + e);
        });
    }

    sendBtn.addEventListener("click", send);

    function sendInterrupt() {
      if (!escBtn) return;
      escBtn.disabled = true;
      sendErr.hidden = true;
      fetch("/api/agents/" + encodeURIComponent(name) + "/interrupt", { method: "POST" })
        .then((r) => r.json().then((d) => ({ ok: r.ok, d: d })))
        .then(({ ok, d }) => {
          if (!(ok && d.ok)) showError((d && d.error) || "interrupt failed");
        })
        .catch((e) => showError("network error: " + e))
        .finally(() => { escBtn.disabled = false; });
    }
    if (escBtn) escBtn.addEventListener("click", sendInterrupt);

    input.addEventListener("keydown", (e) => {
      // A backspace/forward-delete adjacent to a [FILE: ...] token removes the
      // whole token (and its file) in one keystroke -- only for a collapsed caret;
      // a range selection deletes normally.
      if ((e.key === "Backspace" || e.key === "Delete") && input.selectionStart === input.selectionEnd) {
        if (maybeDeleteToken(e.key === "Backspace")) {
          e.preventDefault();
          return;
        }
      }
      // Enter sends; Shift+Enter newline. On touch keyboards Enter inserts a
      // newline (no reliable modifier), so rely on the send button there.
      if (e.key === "Enter" && !e.shiftKey && !isTouch()) {
        e.preventDefault();
        send();
      }
    });
    function isTouch() {
      return "ontouchstart" in window || navigator.maxTouchPoints > 0;
    }

    // ---- attachments / uploads ----
    // On paste-image or file-attach we generate the uuid client-side, drop a
    // literal [FILE: ./chat_uploads/<uuid>.<ext>] token at the cursor (so the
    // path sits where the image belongs in the message; Claude Code reads such
    // paths natively), and upload to the agent's workdir in the background.
    const uploadStrip = document.getElementById("upload-strip");
    const attachBtn = document.getElementById("attach");
    const fileInput = document.getElementById("file-input");
    const uploads = new Map(); // storedName -> { chip, token, objectUrl }
    const UPLOAD_DIR = "./chat_uploads";
    const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;

    function newUuid() {
      if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
      return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
        const r = (Math.random() * 16) | 0;
        return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
      });
    }
    function extFor(file) {
      const name = file.name || "";
      const dot = name.lastIndexOf(".");
      let ext = dot > 0 ? name.slice(dot + 1) : "";
      if (!ext && file.type) ext = file.type.split("/").pop();
      ext = (ext || "bin").toLowerCase().replace(/[^a-z0-9]/g, "");
      return ext || "bin";
    }
    function insertAtCursor(text) {
      const s = input.selectionStart != null ? input.selectionStart : input.value.length;
      const e = input.selectionEnd != null ? input.selectionEnd : input.value.length;
      const v = input.value;
      input.value = v.slice(0, s) + text + v.slice(e);
      const pos = s + text.length;
      try { input.setSelectionRange(pos, pos); } catch (_e) {}
      autoGrow();
    }
    function removeTokenFromInput(token) {
      input.value = input.value.replace(token + " ", "").replace(token, "");
      autoGrow();
    }
    function showStrip() { uploadStrip.hidden = uploads.size === 0; }

    function uploadFile(file) {
      if (file.size > MAX_UPLOAD_BYTES) { showError("file too large (max 25MB): " + (file.name || "")); return; }
      const storedName = newUuid() + "." + extFor(file);
      const token = "[FILE: " + UPLOAD_DIR + "/" + storedName + "]";
      insertAtCursor(token + " ");

      const chip = el("div", "upload-chip uploading");
      let objectUrl = null;
      if ((file.type || "").indexOf("image/") === 0) {
        objectUrl = URL.createObjectURL(file);
        const img = el("img");
        img.src = objectUrl;
        chip.appendChild(img);
      } else {
        chip.appendChild(el("div", "file-icon", (file.name || storedName).slice(0, 18)));
      }
      // Live progress overlay: a real bar from XHR upload.onprogress, then a
      // "finalizing…" state while the server writes to the agent (not settled
      // until the POST returns).
      const prog = el("div", "up-progress");
      const bar = el("div", "up-bar");
      const pct = el("div", "up-pct", "0%");
      prog.appendChild(bar);
      prog.appendChild(pct);
      chip.appendChild(prog);
      const x = el("button", "x", "×");
      x.title = "Remove attachment";
      x.addEventListener("click", function () { removeUpload(storedName); });
      chip.appendChild(x);
      uploadStrip.appendChild(chip);

      const fd = new FormData();
      fd.append("file", file);
      fd.append("filename", storedName);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/agents/" + encodeURIComponent(name) + "/upload");
      xhr.upload.onprogress = function (e) {
        if (!e.lengthComputable) return;
        const p = Math.round((e.loaded / e.total) * 100);
        bar.style.width = p + "%";
        if (p >= 100) { pct.textContent = "finalizing…"; chip.classList.add("finalizing"); }
        else { pct.textContent = p + "%"; }
      };
      xhr.onload = function () {
        let d = null;
        try { d = JSON.parse(xhr.responseText); } catch (_e) { /* non-json */ }
        if (xhr.status >= 200 && xhr.status < 300 && d && d.ok) {
          chip.classList.remove("uploading", "finalizing");
          chip.classList.add("done");
          if (prog.parentNode) prog.parentNode.removeChild(prog);
        } else {
          markUploadError(chip, (d && d.error) || ("HTTP " + xhr.status));
        }
      };
      xhr.onerror = function () { markUploadError(chip, "network error"); };
      uploads.set(storedName, { chip: chip, token: token, objectUrl: objectUrl, xhr: xhr });
      showStrip();
      xhr.send(fd);
    }
    function markUploadError(chip, msg) {
      chip.classList.remove("uploading", "finalizing");
      chip.classList.add("error");
      chip.title = "upload failed: " + msg;
      const prog = chip.querySelector(".up-progress");
      if (prog) prog.remove();
      if (!chip.querySelector(".err-badge")) chip.appendChild(el("div", "err-badge", "failed"));
    }
    // Drop an upload's thumbnail + revoke its preview + rm the remote file. Aborts
    // an in-flight upload first. Does NOT touch the textarea (callers that removed
    // the token themselves use this).
    function dropUploadByName(storedName) {
      const entry = uploads.get(storedName);
      if (entry) {
        if (entry.xhr && entry.xhr.readyState !== 4) { try { entry.xhr.abort(); } catch (_e) {} }
        if (entry.chip && entry.chip.parentNode) entry.chip.parentNode.removeChild(entry.chip);
        if (entry.objectUrl) URL.revokeObjectURL(entry.objectUrl);
        uploads.delete(storedName);
        showStrip();
      }
      // Best-effort delete of the remote file: idempotent rm -f covers a completed
      // upload OR any partial write if an abort raced the server. Also covers
      // manually-typed tokens.
      fetch("/api/agents/" + encodeURIComponent(name) + "/upload/" + encodeURIComponent(storedName), { method: "DELETE" }).catch(() => {});
    }
    // X button on the strip: remove the token from the textarea too, then drop.
    function removeUpload(storedName) {
      const entry = uploads.get(storedName);
      if (entry) removeTokenFromInput(entry.token);
      dropUploadByName(storedName);
    }
    // Backspace/forward-delete deletes a whole [FILE: ...] token in one keystroke
    // when the caret sits inside or against it. Returns true if it handled the key.
    function maybeDeleteToken(isBackspace) {
      const p = input.selectionStart;
      const v = input.value;
      const re = new RegExp(FILE_TOKEN_SRC, "g");
      let m;
      while ((m = re.exec(v)) !== null) {
        const start = m.index;
        const end = start + m[0].length;
        const hit = isBackspace ? start < p && p <= end : start <= p && p < end;
        if (hit) {
          input.value = v.slice(0, start) + v.slice(end);
          try { input.setSelectionRange(start, start); } catch (_e) {}
          autoGrow();
          dropUploadByName(m[1]);
          return true;
        }
      }
      return false;
    }
    function clearUploads() {
      uploads.forEach((entry) => {
        if (entry.chip && entry.chip.parentNode) entry.chip.parentNode.removeChild(entry.chip);
        if (entry.objectUrl) URL.revokeObjectURL(entry.objectUrl);
      });
      uploads.clear();
      showStrip();
    }

    if (attachBtn) attachBtn.addEventListener("click", function () { fileInput.click(); });
    if (fileInput) fileInput.addEventListener("change", function () {
      Array.prototype.forEach.call(fileInput.files || [], uploadFile);
      fileInput.value = "";
    });
    input.addEventListener("paste", function (e) {
      const items = (e.clipboardData && e.clipboardData.items) || [];
      const files = [];
      for (let i = 0; i < items.length; i++) {
        if (items[i].kind === "file") { const f = items[i].getAsFile(); if (f) files.push(f); }
      }
      if (files.length) { e.preventDefault(); files.forEach(uploadFile); }
    });
  }

  // ==========================================================================
  // Terminal page: xterm.js <-> pty websocket bridge
  // ==========================================================================
  // Terminal target: either an agent (/a/<name>/terminal) or the orchestrator
  // shell (/terminal, a plain bash on the foreman server host).
  function terminalTarget() {
    const m = location.pathname.match(/^\/a\/(.+)\/terminal$/);
    if (m) {
      const name = decodeURIComponent(m[1]);
      return { kind: "agent", name: name, wsPath: "/ws/agents/" + encodeURIComponent(name) + "/terminal", back: "/a/" + encodeURIComponent(name), label: name };
    }
    const h = location.pathname.match(/^\/h\/(.+)\/terminal$/);
    if (h) {
      const host = decodeURIComponent(h[1]);
      return { kind: "host", name: host, wsPath: "/ws/hosts/" + encodeURIComponent(host) + "/terminal", back: "/", label: "shell · " + host };
    }
    return { kind: "orchestrator", name: "", wsPath: "/ws/terminal", back: "/", label: "orchestrator (this box)" };
  }

  // Control-bar key → escape sequence sent straight down the WS.
  const CTRL_SEQ = {
    up: "\x1b[A", down: "\x1b[B", right: "\x1b[C", left: "\x1b[D",
    enter: "\r", esc: "\x1b", tab: "\t", shifttab: "\x1b[Z",
    pgup: "\x1b[5~", pgdn: "\x1b[6~",
  };

  function initTerminal() {
    const tgt = terminalTarget();
    document.getElementById("agent-name").textContent = tgt.label;
    document.title =
      tgt.kind === "agent" ? tgt.name + " — terminal"
      : tgt.kind === "host" ? "[shell] " + tgt.name + " — terminal"
      : "foreman — terminal";
    const back = document.getElementById("back");
    if (back) back.href = tgt.back;

    // Agent terminals show the live state in the tab title too (orchestrator
    // shells have no agent state). Same lazy poll as the chat page.
    if (tgt.kind === "agent" && tgt.name) {
      installStatePolling(tgt.name, (d) => { document.title = statusTitle(d, tgt.name + " — terminal"); });
    }

    if (typeof Terminal === "undefined") {
      setConn("xterm failed to load");
      return;
    }
    const term = new Terminal({
      cursorBlink: true,
      fontFamily: '"Atkinson Hyperlegible Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      fontSize: 13,
      theme: { background: "#000000", foreground: "#ffffff" },
    });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(document.getElementById("term"));
    fit.fit();

    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(proto + "//" + location.host + tgt.wsPath);
    ws.binaryType = "arraybuffer";
    const encoder = new TextEncoder();

    function wsSend(data) {
      if (ws.readyState === WebSocket.OPEN) ws.send(typeof data === "string" ? encoder.encode(data) : data);
    }
    function sendResize() {
      if (ws.readyState !== WebSocket.OPEN) return;
      ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
    }

    ws.onopen = () => { setConn("live"); fit.fit(); sendResize(); term.focus(); };
    ws.onmessage = (ev) => {
      if (ev.data instanceof ArrayBuffer) term.write(new Uint8Array(ev.data));
      else if (typeof ev.data === "string") term.write(ev.data);
    };
    ws.onclose = () => { setConn("closed"); term.write("\r\n\x1b[90m[disconnected]\x1b[0m\r\n"); };
    ws.onerror = () => setConn("error");

    term.onData((data) => wsSend(data));

    let resizeTimer = null;
    function onResize() {
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => { fit.fit(); sendResize(); }, 120);
    }
    window.addEventListener("resize", onResize);
    window.addEventListener("orientationchange", onResize);

    // ---- mobile control bar ----
    // Buttons must NOT steal focus from the terminal: preventDefault on
    // mousedown/touchstart so the xterm textarea keeps focus.
    const bar = document.getElementById("ctrlbar");
    if (bar) {
      bar.querySelectorAll("button").forEach((btn) => {
        // One pointer handler covers mouse, touch, and pen. The old code called
        // preventDefault on touchstart -- which on phones cancels the emulated
        // click, so the button did nothing. preventDefault on pointerdown keeps
        // the terminal focused without swallowing the tap; the action fires on
        // pointerup (which fires once for every input type).
        btn.addEventListener("pointerdown", (e) => e.preventDefault());
        btn.addEventListener("pointerup", (e) => {
          e.preventDefault();
          const key = btn.getAttribute("data-seq");
          if (key === "paste") { doPaste(); return; }
          const seq = CTRL_SEQ[key];
          if (seq) wsSend(seq);
          // Don't yank focus back to the hidden xterm textarea on touch -- that
          // pops the soft keyboard up over the screen after every key tap.
          if (e.pointerType !== "touch") term.focus();
        });
      });
    }

    // ---- paste: clipboard API, with a text-popover fallback ----
    // navigator.clipboard.readText requires a secure context (https/localhost);
    // over plain http:// to a remote host it's unavailable or rejects, so fall
    // back to a popover the user pastes into and sends.
    const popover = document.getElementById("paste-popover");
    function doPaste() {
      const clip = navigator.clipboard;
      if (clip && typeof clip.readText === "function") {
        clip.readText().then((text) => {
          if (text) wsSend(text);
          term.focus();
        }).catch(() => showPastePopover());
      } else {
        showPastePopover();
      }
    }
    function showPastePopover() {
      if (!popover) return;
      const ta = document.getElementById("pp-text");
      popover.hidden = false;
      ta.value = "";
      ta.focus();
    }
    function hidePastePopover() {
      if (popover) popover.hidden = true;
      term.focus();
    }
    if (popover) {
      document.getElementById("pp-send").addEventListener("click", () => {
        const ta = document.getElementById("pp-text");
        if (ta.value) wsSend(ta.value);
        hidePastePopover();
      });
      document.getElementById("pp-cancel").addEventListener("click", hidePastePopover);
      popover.addEventListener("click", (e) => { if (e.target === popover) hidePastePopover(); });
    }
  }

  window.foreman = { initIndex: initIndex, initAgent: initAgent, initTerminal: initTerminal };
})();
