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
  function setConn(state) {
    const c = document.getElementById("conn");
    if (c) c.textContent = state;
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

  // Poll an agent's input-state and call onState(d) each tick. Keeps polling
  // while the tab is hidden (slower) so the title stays live in background tabs.
  function installStatePolling(agentName, onState) {
    let timer = null;
    function tick() {
      fetch("/api/agents/" + encodeURIComponent(agentName) + "/input-state")
        .then((r) => r.json())
        .then(onState)
        .catch(() => {});
    }
    function schedule() {
      if (timer) clearInterval(timer);
      timer = setInterval(tick, document.hidden ? 15000 : 4000);
    }
    document.addEventListener("visibilitychange", () => { tick(); schedule(); });
    tick();
    schedule();
  }

  // ==========================================================================
  // Index page: agent list
  // ==========================================================================
  function initIndex() {
    document.title = "foreman — home";
    const listEl = document.getElementById("list");
    const cards = new Map(); // id -> element

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
      card.appendChild(row2);
      return card;
    }
    function renderAll(agents) {
      cards.clear();
      listEl.innerHTML = "";
      if (!agents.length) {
        listEl.appendChild(el("div", "empty", "no agents"));
        return;
      }
      agents.forEach((a) => {
        const c = cardEl(a);
        cards.set(a.id, c);
        listEl.appendChild(c);
      });
    }
    function upsert(a) {
      const fresh = cardEl(a);
      if (cards.has(a.id)) {
        cards.get(a.id).replaceWith(fresh);
      } else {
        const empty = listEl.querySelector(".empty");
        if (empty) empty.remove();
        listEl.appendChild(fresh);
      }
      cards.set(a.id, fresh);
    }
    function remove(id) {
      if (cards.has(id)) {
        cards.get(id).remove();
        cards.delete(id);
      }
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

    function connect() {
      if (typeof EventSource === "undefined") { startPolling(); return; }
      const es = new EventSource("/api/agents/stream");
      es.onopen = () => { stopPolling(); setConn("live"); };
      es.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch (_e) { return; }
        if (msg.type === "snapshot") renderAll(msg.agents || []);
        else if (msg.type === "upsert") upsert(msg.agent);
        else if (msg.type === "remove") remove(msg.agent_id);
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
    // Optimistic "queued" bubbles: a message sent while the agent is busy shows
    // immediately in purple, then swaps to a normal user bubble once it lands in
    // the transcript. Each entry: { text, node }.
    const pendingQueued = [];
    // The upload token: "[FILE: ./chat_uploads/<uuid>.<ext>]". Captured group 1
    // is the stored name. Used to render chips in the transcript and to delete a
    // whole token on backspace. Build a fresh RegExp per use to reset lastIndex.
    const FILE_TOKEN_SRC = "\\[FILE:\\s*\\./chat_uploads/([A-Za-z0-9._-]+)\\]";
    const IMAGE_EXTS = { png: 1, jpg: 1, jpeg: 1, gif: 1, webp: 1, bmp: 1, svg: 1 };

    function atBottom() {
      return window.innerHeight + window.scrollY >= document.body.offsetHeight - 80;
    }
    function scrollDown(force) {
      if (force || atBottom()) window.scrollTo(0, document.body.scrollHeight);
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

    // "Working" indicator: pinned pulsing dot shown while claude is generating.
    // Two signals drive it:
    //  - transcript heuristic (instant ON): a user_message or tool_result is the
    //    latest input (claude should respond), or the latest assistant message
    //    made tool calls (more to come). A plain-text assistant message with no
    //    tool calls means the turn completed -> OFF. This is instant but can mis-
    //    read the tail (e.g. after an interrupt the last event stays a tool_result
    //    forever, so the dot would be stuck on).
    //  - mngr lifecycle state (authoritative OFF): the input-state poll reports
    //    busy=false whenever mngr sees no 'active' marker -- claude idle at the
    //    prompt or blocked on a dialog. This clears a dot the heuristic misreads.
    // mngr's OFF is ignored for a short grace window right after an ON, because
    // the RUNNING state takes ~1-3s to propagate after a prompt is submitted, and
    // acting on the pre-flip WAITING would blink the dot off immediately.
    const MNGR_IDLE_GRACE_MS = 5000;
    let working = false;
    let workingSince = 0;
    const escBtn = document.getElementById("esc");
    const workingEl = el("div", "working");
    workingEl.hidden = true;
    workingEl.appendChild(el("span", "dot"));
    workingEl.appendChild(el("span", null, "working…"));

    function setWorking(v) {
      if (v && !working) workingSince = Date.now();
      working = v;
    }
    function updateWorkingFrom(ev) {
      if (ev.type === "user_message") {
        setWorking(true);
      } else if (ev.type === "tool_result") {
        setWorking(true);
      } else if (ev.type === "assistant_message") {
        if ((ev.tool_calls || []).length > 0) setWorking(true);
        else if (ev.text && ev.text.trim()) setWorking(false);
        // else: empty assistant chunk -> leave state unchanged
      }
    }
    // Authoritative OFF from mngr's busy flag (see the input-state poll below).
    function applyMngrBusy(busy) {
      if (busy === false && working && Date.now() - workingSince > MNGR_IDLE_GRACE_MS) {
        setWorking(false);
        refreshWorking();
      }
      // busy === true / null / undefined: leave the transcript heuristic in charge.
    }
    function refreshWorking() {
      // State precedence: BLOCKED (a ❯ dialog / mngr PERMISSIONS -> greyed
      // composer) beats "working", so never show the dot while blocked -- even if
      // mngr still reads RUNNING because a mid-turn menu left the active marker set.
      workingEl.hidden = !working || blocked;
      if (!workingEl.hidden) tEl.appendChild(workingEl); // keep it pinned last
      if (escBtn) escBtn.disabled = false; // always usable; Escape is harmless
    }

    // ---- optimistic "queued" bubbles ----
    // Claude's CLI queues input pasted mid-turn, and the paste lands even while
    // it generates (mngr's send preflight only blocks on dialogs). So a send
    // while busy is real -- we just show it purple until the transcript confirms
    // delivery, then resolveQueued swaps it for the normal user bubble.
    function addQueued(text) {
      const node = el("div", "entry user queued");
      renderUserContent(node, text);
      tEl.appendChild(node);
      pendingQueued.push({ text: text, node: node });
      refreshWorking(); // keep the working dot pinned below the new bubble
      scrollDown(true);
    }
    function resolveQueued(text) {
      const key = (text || "").trim();
      const i = pendingQueued.findIndex((q) => q.text.trim() === key);
      if (i === -1) return;
      const q = pendingQueued.splice(i, 1)[0];
      if (q.node && q.node.parentNode) q.node.parentNode.removeChild(q.node);
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

    function renderEvent(ev) {
      const wasBottom = atBottom();
      if (ev.type === "user_message") {
        // If we optimistically showed this as a "queued" bubble, drop the
        // placeholder -- the real delivered message renders normally below.
        resolveQueued(ev.content || "");
        const e = el("div", "entry user");
        renderUserContent(e, ev.content || "");
        // Pasted / queued images ride along on the message.
        if (ev.images && ev.images.length) ev.images.forEach((img) => e.appendChild(toolImageEl(img)));
        tEl.appendChild(e);
      } else if (ev.type === "framework_message") {
        tEl.appendChild(frameworkEl(ev));
      } else if (ev.type === "assistant_message") {
        const e = el("div", "entry assistant");
        if (ev.text && ev.text.trim()) {
          const md = el("div", "assistant-md");
          md.innerHTML = renderMarkdown(ev.text);
          enrichAssistant(md); // syntax highlight, math, mermaid, safe links
          e.appendChild(md);
        }
        (ev.tool_calls || []).forEach((tc) => e.appendChild(toolCallEl(tc)));
        if (e.childNodes.length) tEl.appendChild(e);
      } else if (ev.type === "tool_result") {
        if (toolBodies.has(ev.tool_call_id)) attachResult(ev);
        else pendingResults.set(ev.tool_call_id, ev);
      }
      updateWorkingFrom(ev);
      refreshWorking();
      scrollDown(wasBottom);
    }

    function setStatus(text) {
      let s = document.getElementById("stat");
      if (!s) { s = el("div", "status-line"); s.id = "stat"; tEl.appendChild(s); }
      s.textContent = text;
    }
    function clearStatus() {
      const s = document.getElementById("stat");
      if (s) s.remove();
    }

    // ---- transcript SSE ----
    function connect() {
      if (typeof EventSource === "undefined") {
        setStatus("Live transcript needs EventSource support.");
        composer.hidden = false;
        return;
      }
      setStatus("loading transcript…");
      const es = new EventSource("/api/agents/" + encodeURIComponent(name) + "/transcript");
      es.onopen = () => setConn("live");
      es.onmessage = (raw) => {
        let msg;
        try { msg = JSON.parse(raw.data); } catch (_e) { return; }
        if (msg.type === "event") {
          renderEvent(msg.event);
        } else if (msg.type === "backfill_complete") {
          clearStatus();
          composer.hidden = false;
          scrollDown(true);
        } else if (msg.type === "unsupported") {
          clearStatus();
          tEl.appendChild(el("div", "unsupported", "No transcript for agent type '" + msg.agent_type + "'."));
        } else if (msg.type === "error") {
          setStatus(msg.message || "error");
          composer.hidden = false;
        }
      };
      es.onerror = () => setConn("reconnecting");
    }
    connect();

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
    installStatePolling(name, (d) => {
      document.title = statusTitle(d, name + " — chat");
      if (d.blocked) setBlocked(); else clearBlocked();
      applyMngrBusy(d.busy);
    });

    function autoGrow() {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, window.innerHeight * 0.4) + "px";
    }
    input.addEventListener("input", autoGrow);

    function showError(text) {
      sendErr.hidden = false;
      sendErr.innerHTML = escapeHtml(text) + '  ·  <a href="#" id="retry">dismiss</a>';
      const r = document.getElementById("retry");
      if (r) r.onclick = (e) => { e.preventDefault(); sendErr.hidden = true; };
    }

    function send() {
      const msg = input.value.trim();
      if (!msg) return;
      // Composer lock: disable the textarea + button while the POST is in flight
      // so nothing is edited mid-send. Re-enabled in finally.
      sendBtn.disabled = true;
      input.disabled = true;
      sendErr.hidden = true;
      fetch("/api/agents/" + encodeURIComponent(name) + "/message", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: msg }),
      })
        .then((r) => r.json().then((d) => ({ ok: r.ok, d: d })))
        .then(({ ok, d }) => {
          if (ok && d.ok) {
            input.value = "";
            autoGrow();
            clearBlocked();
            // Show it immediately as "queued" (purple); the transcript swaps it
            // for a normal bubble when the delivered message arrives.
            addQueued(msg);
            // The [FILE: ...] tokens went out with the message; the uploaded
            // files stay in the agent's workdir. Clear the strip (don't delete).
            clearUploads();
          } else {
            const err = (d && d.error) || "send failed — open the terminal to resolve any prompt.";
            showError(err);
            // A failed send usually means a blocking dialog ate the paste; flip
            // to the greyed state immediately (the next poll re-confirms/clears).
            setBlocked();
          }
        })
        .catch((e) => showError("network error: " + e))
        .finally(() => { sendBtn.disabled = false; input.disabled = false; input.focus(); });
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

      const chip = el("div", "upload-chip pending");
      let objectUrl = null;
      if ((file.type || "").indexOf("image/") === 0) {
        objectUrl = URL.createObjectURL(file);
        const img = el("img");
        img.src = objectUrl;
        chip.appendChild(img);
      } else {
        chip.appendChild(el("div", "file-icon", (file.name || storedName).slice(0, 18)));
      }
      const x = el("button", "x", "×");
      x.title = "Remove attachment";
      x.addEventListener("click", function () { removeUpload(storedName); });
      chip.appendChild(x);
      uploadStrip.appendChild(chip);
      uploads.set(storedName, { chip: chip, token: token, objectUrl: objectUrl });
      showStrip();

      const fd = new FormData();
      fd.append("file", file);
      fd.append("filename", storedName);
      fetch("/api/agents/" + encodeURIComponent(name) + "/upload", { method: "POST", body: fd })
        .then((r) => r.json().then((d) => ({ ok: r.ok, d: d })))
        .then(({ ok, d }) => {
          if (ok && d.ok) chip.classList.remove("pending");
          else markUploadError(chip, (d && d.error) || "failed");
        })
        .catch(() => markUploadError(chip, "network error"));
    }
    function markUploadError(chip, msg) {
      chip.classList.remove("pending");
      chip.classList.add("error");
      chip.title = "upload failed: " + msg;
      if (!chip.querySelector(".err-badge")) chip.appendChild(el("div", "err-badge", "failed"));
    }
    // Drop an upload's thumbnail + revoke its preview + rm the remote file. Does
    // NOT touch the textarea (callers that removed the token themselves use this).
    function dropUploadByName(storedName) {
      const entry = uploads.get(storedName);
      if (entry) {
        if (entry.chip && entry.chip.parentNode) entry.chip.parentNode.removeChild(entry.chip);
        if (entry.objectUrl) URL.revokeObjectURL(entry.objectUrl);
        uploads.delete(storedName);
        showStrip();
      }
      // Best-effort delete of the remote file (also covers manually-typed tokens).
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
    document.title = tgt.kind === "agent" ? tgt.name + " — terminal" : "foreman — terminal";
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
        btn.addEventListener("mousedown", (e) => e.preventDefault());
        btn.addEventListener("touchstart", (e) => e.preventDefault(), { passive: false });
        btn.addEventListener("click", (e) => {
          e.preventDefault();
          const key = btn.getAttribute("data-seq");
          if (key === "paste") { doPaste(); return; }
          const seq = CTRL_SEQ[key];
          if (seq) wsSend(seq);
          term.focus();
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
