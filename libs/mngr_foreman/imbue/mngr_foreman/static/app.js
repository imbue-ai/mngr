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
  // THE single source of truth: reduce a live input-state read to ONE status, shown
  // verbatim in all three spots (home card, tab title, chat dot) -- they call these
  // and nothing else, so they can never drift. "" = not running / unknown (caller keeps
  // its last shown value). Four real cases; the backend already computed them off the
  // live pane glyph (never mngr's coarse state), including WORKING_BACKGROUND (a subagent
  // / backgrounded shell is generating while the main loop is free for input).
  function statusKey(d) {
    if (!d || !d.running) return "";
    const s = d.status;
    if (s === "NEEDS_INPUT" || s === "WORKING" || s === "READY" || s === "WORKING_BACKGROUND") return s;
    return ""; // UNKNOWN / missing -> keep last
  }
  // The exact label text for each status -- the ONE place these strings live.
  function statusLabel(k) {
    if (k === "NEEDS_INPUT") return "NEEDS INPUT";
    if (k === "WORKING") return "WORKING";
    if (k === "WORKING_BACKGROUND") return "WORKING IN BACKGROUND, READY";
    if (k === "READY") return "READY";
    return "";
  }
  // CSS class suffix for the status colour (working / needs-input / ready / working-background).
  function statusClass(k) {
    return k ? k.toLowerCase().replace(/_/g, "-") : "";
  }
  function statusTitle(d, base) {
    const label = statusLabel(statusKey(d));
    return label ? "[" + label + "] " + base : base;
  }

  // Poll an agent's input-state and call onState(d) each tick, at a steady rate
  // (faster in a short burst right after a send).
  function installStatePolling(agentName, onState) {
    // Greedy ~1s poll, COALESCED: at most one probe outstanding per agent, so a slow
    // remote/docker probe can't stack up requests. UNKNOWN responses (pane unreadable
    // this poll) are DROPPED so the caller keeps its last state -- mngr's coarse/stale
    // state never enters. onState is the ONE place the tab title + working dot +
    // composer state all derive from (all via statusKey), so they can't drift.
    let inFlight = false;
    function tick() {
      if (inFlight) return;
      inFlight = true;
      fetch("/api/agents/" + encodeURIComponent(agentName) + "/input-state")
        .then((r) => r.json())
        .then((d) => { if (!d || d.status !== "UNKNOWN") onState(d); })
        .catch(() => {})
        .finally(() => { inFlight = false; });
    }
    tick();
    setInterval(tick, 1000);
    // "burst after send" is just an immediate extra poll now; the steady 1s covers the rest.
    return { burst: tick };
  }

  // ==========================================================================
  // Index page: agent list
  // ==========================================================================
  function initIndex() {
    document.title = "foreman — home";
    const listEl = document.getElementById("list");

    function relTime(iso) {
      if (!iso) return "";
      const d = new Date(iso);
      const secs = Math.max(0, (Date.now() - d.getTime()) / 1000);
      if (secs < 60) return Math.floor(secs) + "s ago";
      if (secs < 3600) return Math.floor(secs / 60) + "m ago";
      if (secs < 86400) return Math.floor(secs / 3600) + "h ago";
      return Math.floor(secs / 86400) + "d ago";
    }
    // Backburner: agents the user has parked. Marked by the `foreman.backburner`
    // mngr label (persisted server-side); the home page files them under their own
    // section instead of the live list.
    let lastAgents = [];
    // Per-agent in-flight guard. A toggle flips a persisted mngr label, then a
    // discovery poll (~10s) re-reads it. During that window we (1) hold the optimistic
    // value so a stale snapshot can't flip the card back, and (2) refuse a second
    // toggle of the SAME agent. A 15s timeout is the backstop: if the change never
    // reconciles (POST lost, agent vanished), the guard releases so the button can't
    // wedge forever.
    const bbPending = new Map(); // name -> { want: bool, timer }
    function rawBackburner(a) { return !!(a.labels && a.labels["foreman.backburner"] === "true"); }
    function isBackburner(a) {
      const p = bbPending.get(a.name);
      return p ? p.want : rawBackburner(a); // optimistic value wins until reconciled
    }
    function clearBbPending(name) {
      const p = bbPending.get(name);
      if (!p) return;
      clearTimeout(p.timer);
      bbPending.delete(name);
      renderAll(lastAgents);
    }
    // On a fresh snapshot, release any guard whose persisted label now matches the ask.
    function reconcileBackburner(agents) {
      bbPending.forEach((p, name) => {
        const a = agents.find((x) => x.name === name);
        if (a && rawBackburner(a) === p.want) { clearTimeout(p.timer); bbPending.delete(name); }
      });
    }
    function setBackburner(name, on) {
      if (bbPending.has(name)) return; // a toggle for this agent is already in flight
      const timer = setTimeout(() => clearBbPending(name), 15000);
      bbPending.set(name, { want: on, timer });
      renderAll(lastAgents); // optimistic: isBackburner now reflects `want`
      fetch("/api/agents/" + encodeURIComponent(name) + "/backburner", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ on: on }),
      })
        .then((r) => { if (!r.ok) clearBbPending(name); }) // rejected -> revert to persisted
        .catch(() => clearBbPending(name)); // never sent -> revert; snapshot is truth
    }
    function cardEl(a) {
      const card = el("a", "agent-card");
      card.href = "/a/" + encodeURIComponent(a.name);
      card.dataset.name = a.name; // read back by the shared status poller
      const row1 = el("div", "row1");
      row1.appendChild(el("span", "name", a.name));
      // OUR status -- the single source of truth, identical to the tab title + chat
      // dot. This REPLACES the old mngr-state chip AND the separate dot (those were two
      // duplicate, drifting signals). The ~1s poller fills its label text + colour.
      row1.appendChild(el("span", "status-badge", ""));
      const bb = isBackburner(a);
      if (bb) card.classList.add("backburner");
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
      // ⬇ send to Backburner / ⬆ restore, parked at the card's bottom-right (CSS
      // margin-left:auto). The card is an <a>, so stop it navigating.
      const demote = el("button", "backburner-btn", bb ? "⬆" : "⬇");
      demote.type = "button";
      demote.disabled = bbPending.has(a.name); // one toggle at a time per agent
      demote.title = bb ? "Restore from backburner" : "Send to backburner";
      demote.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        setBackburner(a.name, !bb);
      });
      row2.appendChild(demote);
      card.appendChild(row2);
      return card;
    }
    // Backburner collapse persists in localStorage: renderAll rebuilds the DOM on
    // every discovery snapshot (~every poll), so without persistence a collapse would
    // pop back open on the next tick.
    const BB_COLLAPSE_KEY = "foreman.backburnerCollapsed";
    function scrollSection() { return el("div", "scroll-section"); }
    function renderAll(agents) {
      lastAgents = agents;
      reconcileBackburner(agents); // release guards whose label has now propagated
      listEl.innerHTML = "";
      const active = agents.filter((a) => !isBackburner(a));
      const back = agents.filter(isBackburner);

      // 1. Live agents: no title, not collapsible, scrolls past ~5 rows.
      const live = scrollSection();
      if (!agents.length) live.appendChild(el("div", "empty", "no agents"));
      active.forEach((a) => live.appendChild(cardEl(a)));
      listEl.appendChild(live);

      // 2. Backburner: collapsible header + its own ~5-row scroll body.
      if (back.length) {
        const block = el("div", "bb-block");
        if (localStorage.getItem(BB_COLLAPSE_KEY) === "1") block.classList.add("collapsed");
        const head = el("button", "section-head");
        head.type = "button";
        head.appendChild(el("span", "chevron", "▾"));
        head.appendChild(document.createTextNode("Backburner"));
        head.appendChild(el("span", "count", "(" + back.length + ")"));
        head.addEventListener("click", function () {
          block.classList.toggle("collapsed");
          localStorage.setItem(BB_COLLAPSE_KEY, block.classList.contains("collapsed") ? "1" : "0");
        });
        block.appendChild(head);
        const body = scrollSection();
        back.forEach((a) => body.appendChild(cardEl(a)));
        block.appendChild(body);
        listEl.appendChild(block);
      }

      // 3. Shortcuts: always present, below the agents. (Contents TBD.)
      const sc = el("div", "shortcuts-block");
      sc.appendChild(el("div", "section-head", "Shortcuts"));
      sc.appendChild(el("div", "shortcuts-body"));
      listEl.appendChild(sc);

      pollStatuses(); // populate the new cards' dots right away, don't wait a tick
    }

    // Live status dots: one shared interval, one input-state fetch per visible
    // card. The card DOM is the source of truth for "which cards are live" -- a
    // card that left the set (renderAll wiped + rebuilt) is simply no longer in
    // this query, so its polling stops with no per-card bookkeeping/leak.
    const inFlightCards = new Set(); // coalesce: at most one probe outstanding per agent
    function pollStatuses() {
      listEl.querySelectorAll(".agent-card").forEach((card) => {
        const name = card.dataset.name;
        if (!name || inFlightCards.has(name)) return;
        inFlightCards.add(name);
        fetch("/api/agents/" + encodeURIComponent(name) + "/input-state")
          .then((r) => r.json())
          .then((d) => {
            if (d && d.status === "UNKNOWN") return; // pane unreadable -> keep last badge
            const badge = card.querySelector(".status-badge");
            if (!badge) return;
            const k = statusKey(d); // the ONE derivation the tab title + chat dot also use
            badge.className = "status-badge " + statusClass(k);
            badge.textContent = statusLabel(k);
          })
          .catch(() => {})
          .finally(() => { inFlightCards.delete(name); });
      });
    }
    setInterval(pollStatuses, 1000);

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
    // Timestamp the last frame (snapshot OR heartbeat -- the server heartbeats this
    // stream every ~5s) so we can detect a silently-dead connection whose readyState
    // never flips to CLOSED (mobile radio drop / NAT timeout leaves a zombie OPEN
    // socket). CLOSED-only detection misses that case; staleness catches it.
    let listLastMsgAt = Date.now();
    const LIST_STALE_MS = 15000;
    function connect() {
      if (typeof EventSource === "undefined") { startPolling(); return; }
      if (es) return;
      es = new EventSource("/api/agents/stream");
      es.onopen = () => { stopPolling(); setConn("live"); listLastMsgAt = Date.now(); };
      es.onmessage = (ev) => {
        listLastMsgAt = Date.now(); // any frame (snapshot or heartbeat) proves liveness
        let msg;
        try { msg = JSON.parse(ev.data); } catch (_e) { return; }
        if (msg.type === "snapshot") renderAll(msg.agents || []);
      };
      es.onerror = () => { setConn("reconnecting"); startPolling(); };
    }
    function reconnectList() {
      if (es) { try { es.close(); } catch (_e) {} es = null; }
      connect();
    }
    connect();
    // Reconnect a dead/stale stream: on foreground (mobile freezes the tab + kills the
    // socket while hidden) and on a periodic staleness check (a zombie OPEN socket that
    // has silently stopped delivering -- readyState never flips, so we lean on the
    // heartbeat-derived staleness signal instead).
    function wakeList() {
      if (document.hidden) return;
      if (!es || es.readyState === EventSource.CLOSED || Date.now() - listLastMsgAt > LIST_STALE_MS) reconnectList();
    }
    document.addEventListener("visibilitychange", wakeList);
    window.addEventListener("pageshow", wakeList);
    window.addEventListener("focus", wakeList);
    setInterval(() => { if (!document.hidden && Date.now() - listLastMsgAt > LIST_STALE_MS) reconnectList(); }, 5000);
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
    // queued_command (dark green) to a delivered user_message (green) in place.
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
    const workingText = el("span", null, "working…");
    workingEl.appendChild(workingText);
    // The dot lives permanently in the transcript as a fixed anchor: main content
    // is inserted BEFORE it, queued bubbles are appended AFTER it. Just toggle its
    // visibility; never move it.
    tEl.appendChild(workingEl);

    // Spot 3 of the single status (home card + tab title are the other two): the chat
    // "dot" is driven by the same statusKey, so all three always match. WORKING and
    // WORKING_BACKGROUND show the dot (the latter a distinct colour + label); READY
    // hides it (an active composer IS "ready"); NEEDS INPUT is shown by the composer-
    // blocked UI, not here. "" (unknown this poll) leaves it unchanged.
    function applyStatus(k) {
      if (k === "") return;
      working = k === "WORKING" || k === "WORKING_BACKGROUND";
      workingEl.classList.toggle("bg", k === "WORKING_BACKGROUND");
      workingText.textContent = k === "WORKING_BACKGROUND" ? "working in background · ready" : "working…";
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

    // Full-screen image overlay (tap a chip to preview). Shows a text fallback if
    // the file is gone (a deleted upload) rather than a broken-image glyph.
    function openLightbox(url) {
      const ov = el("div", "lightbox");
      const img = el("img");
      img.src = url;
      img.addEventListener("error", function () {
        ov.textContent = "";
        ov.appendChild(el("div", "lb-missing", "file unavailable"));
      });
      ov.appendChild(img);
      ov.addEventListener("click", function () { ov.remove(); });
      document.body.appendChild(ov);
    }

    // In-page PDF preview: an <iframe> of the served file inside the same dim
    // overlay. A close button and a backdrop click (outside the document) dismiss.
    function openPdfViewer(url) {
      const ov = el("div", "lightbox pdf-viewer");
      const frame = el("iframe", "pdf-frame");
      frame.src = url;
      const close = el("button", "lb-close", "×");
      close.type = "button";
      close.addEventListener("click", function () { ov.remove(); });
      ov.appendChild(frame);
      ov.appendChild(close);
      ov.addEventListener("click", function (e) { if (e.target === ov) ov.remove(); });
      document.body.appendChild(ov);
    }

    // Kind of a stored upload from its extension: "image" | "pdf" | "file".
    function uploadKind(storedName) {
      const ext = (storedName.split(".").pop() || "").toLowerCase();
      if (IMAGE_EXTS[ext]) return "image";
      if (ext === "pdf") return "pdf";
      return "file";
    }

    // A [FILE: ...] token rendered as a compact, clickable chip (icon + label) --
    // NOT an inline image, so the transcript stays clean. Click previews: image ->
    // lightbox, pdf -> pdf viewer, anything else -> open/download in a new tab.
    function fileChip(storedName) {
      const url = "/api/agents/" + encodeURIComponent(name) + "/upload/" + encodeURIComponent(storedName);
      const ext = (storedName.split(".").pop() || "").toLowerCase();
      const kind = uploadKind(storedName);
      const chip = el("button", "file-chip chip-" + kind);
      chip.type = "button";
      chip.title = storedName;
      chip.appendChild(el("span", "fc-icon", (ext || "file").toUpperCase().slice(0, 4)));
      const label = kind === "image" ? "image" : kind === "pdf" ? "PDF" : (ext ? ext.toUpperCase() + " file" : "file");
      chip.appendChild(el("span", "fc-name", label));
      chip.addEventListener("click", function () {
        if (kind === "image") openLightbox(url);
        else if (kind === "pdf") openPdfViewer(url);
        else window.open(url, "_blank", "noopener");
      });
      return chip;
    }

    // A pasted/transcript image carried as base64 (or a /timage reference) shown as
    // the same compact chip instead of a big inline thumbnail; click opens it.
    function imageEventChip(img) {
      const src = imageSrc(img);
      const chip = el("button", "file-chip chip-image");
      chip.type = "button";
      chip.title = "image";
      chip.appendChild(el("span", "fc-icon", "IMG"));
      chip.appendChild(el("span", "fc-name", "image"));
      chip.addEventListener("click", function () { openLightbox(src); });
      return chip;
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

    // Defer the expensive enrichment (syntax highlight / KaTeX / mermaid) until a
    // message is near the viewport, instead of highlighting hundreds of code blocks
    // up front. This is the bulk of a long transcript's load cost.
    const enrichObserver = ("IntersectionObserver" in window)
      ? new IntersectionObserver((entries, obs) => {
          entries.forEach((en) => { if (en.isIntersecting) { obs.unobserve(en.target); enrichAssistant(en.target); } });
        }, { root: scroller, rootMargin: "800px 0px" })
      : null;
    function lazyEnrich(md) { if (enrichObserver) enrichObserver.observe(md); else enrichAssistant(md); }

    // Position relative to the working dot: main content goes ABOVE it (normal
    // history / current turn), queued messages go BELOW it (still in the queue).
    // ``before`` (used while back-filling older history above the tail) inserts at
    // that anchor instead of at the live position.
    function addMain(node, before) { tEl.insertBefore(node, before || workingEl); }
    function addQueued(node, before) { if (before) tEl.insertBefore(node, before); else tEl.appendChild(node); }
    // Queued (dark green, below dot) -> accepted (full green, above dot), in place.
    function promoteBubble(rec, before) {
      if (!rec || !rec.queued) return;
      rec.node.classList.remove("queued");
      rec.queued = false;
      tEl.insertBefore(rec.node, before || workingEl);
    }

    function renderEvent(ev, before) {
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
            if (existing.queued && ev.images && ev.images.length) {
              ev.images.forEach((img) => existing.node.appendChild(imageEventChip(img)));
            }
          } else {
            const e = el("div", "entry user queued");
            renderUserContent(e, ev.content || "");
            if (ev.images && ev.images.length) ev.images.forEach((img) => e.appendChild(imageEventChip(img)));
            addQueued(e, before);
            userBubbles.set(key, { node: e, queued: true });
          }
        } else if (existing) {
          promoteBubble(existing, before);
        } else {
          const e = el("div", "entry user");
          renderUserContent(e, ev.content || "");
          if (ev.images && ev.images.length) ev.images.forEach((img) => e.appendChild(imageEventChip(img)));
          addMain(e, before);
          userBubbles.set(key, { node: e, queued: false });
        }
      } else if (ev.type === "queue_accepted") {
        promoteBubble(userBubbles.get(ev.key), before);
      } else if (ev.type === "queue_removed") {
        const rec = userBubbles.get(ev.key);
        if (rec && rec.queued) { rec.node.remove(); userBubbles.delete(ev.key); }
      } else if (ev.type === "framework_message") {
        addMain(frameworkEl(ev), before);
      } else if (ev.type === "assistant_message") {
        const e = el("div", "entry assistant");
        if (ev.text && ev.text.trim()) {
          const md = el("div", "assistant-md");
          md.innerHTML = renderMarkdown(ev.text);
          lazyEnrich(md); // highlight/math/mermaid when it scrolls into view
          e.appendChild(md);
        }
        (ev.tool_calls || []).forEach((tc) => e.appendChild(toolCallEl(tc)));
        if (e.childNodes.length) addMain(e, before);
      } else if (ev.type === "tool_result") {
        if (toolBodies.has(ev.tool_call_id)) attachResult(ev);
        else pendingResults.set(ev.tool_call_id, ev);
      }
      if (!before) scrollDown(); // live/tail: follow the bottom. older: caller manages scroll.
    }

    // ---- initial-load rendering: tail now, older filled in above during idle ----
    // The server ships the whole transcript in ~0.2s; painting hundreds of messages
    // up front is the slow part. So buffer the first backfill, paint only the last
    // TAIL_RENDER_COUNT immediately, and prepend the rest in idle chunks.
    const TAIL_RENDER_COUNT = 40;
    let backfilling = true;
    let backfillBuffer = [];
    function scheduleIdle(fn) {
      if (window.requestIdleCallback) window.requestIdleCallback(fn, { timeout: 400 });
      else setTimeout(fn, 16);
    }
    function flushInitialBackfill() {
      // Drop the "loading…" status line FIRST -- otherwise it's tEl.firstChild and
      // would be captured as the insert-anchor, then removed, so the async older-
      // fill's insertBefore(...) would throw against a detached node and stop
      // (the "last 40, can't scroll further" bug).
      clearStatus();
      const buf = backfillBuffer;
      backfillBuffer = [];
      const cut = Math.max(0, buf.length - TAIL_RENDER_COUNT);
      const older = buf.slice(0, cut);
      for (let i = cut; i < buf.length; i++) renderEvent(buf[i]); // tail -> instant paint
      const anchor = tEl.firstChild; // first tail node -- stable (won't be removed)
      scrollDown(true);
      if (older.length && anchor) fillOlder(older, anchor);
    }
    function fillOlder(older, anchor) {
      let idx = 0;
      function chunk() {
        if (!anchor.parentNode) return; // anchor detached -> nothing to anchor older content to
        const oldH = scroller.scrollHeight;
        const pinned = stick;
        for (let n = 0; idx < older.length && n < 15; n++, idx++) renderEvent(older[idx], anchor);
        if (pinned) scrollToBottom(); // stay at the tail while history fills above
        else scroller.scrollTop += scroller.scrollHeight - oldH; // keep the viewport stable
        if (idx < older.length) scheduleIdle(chunk);
      }
      scheduleIdle(chunk);
    }

    // Older history streamed newest-first by the server's tail-first backfill: prepend
    // each event at the very top so the transcript grows upward in order. Buffered and
    // rendered in idle chunks (insert before firstChild) so thousands of old events
    // never jank the page or move the reader's viewport.
    let olderBuf = [];
    let olderScheduled = false;
    function scheduleOlder() {
      if (olderScheduled) return;
      olderScheduled = true;
      scheduleIdle(flushStreamedOlder);
    }
    function flushStreamedOlder() {
      olderScheduled = false;
      if (!olderBuf.length) return;
      const oldH = scroller.scrollHeight;
      const pinned = stick;
      for (let n = 0; olderBuf.length && n < 15; n++) renderEvent(olderBuf.shift(), tEl.firstChild);
      if (pinned) scrollToBottom(); // stay pinned to the tail while history fills above
      else scroller.scrollTop += scroller.scrollHeight - oldH; // keep the viewport stable
      if (olderBuf.length) scheduleOlder();
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
          if (backfilling) backfillBuffer.push(msg.event); // buffer the initial load
          else renderEvent(msg.event);
        } else if (msg.type === "backfill_complete") {
          const wasInitial = backfilling;
          if (backfilling) { backfilling = false; flushInitialBackfill(); }
          clearStatus();
          composer.hidden = false;
          // Jump to the bottom only on the first load, or if the user is already there.
          // A background reconnect re-sends backfill_complete; without this guard it
          // would yank someone who has scrolled up reading history back to the bottom.
          if (wasInitial || stick) scrollDown(true);
        } else if (msg.type === "older") {
          // Older history (streamed newest-first after the tail). Dedup, then prepend
          // above the current top in idle chunks.
          const id = msg.event && msg.event.event_id;
          if (id) { if (seenEventIds.has(id)) return; seenEventIds.add(id); }
          olderBuf.push(msg.event);
          scheduleOlder();
        } else if (msg.type === "older_complete") {
          // History fully streamed; the idle flush drains olderBuf on its own.
        } else if (msg.type === "unsupported") {
          clearStatus();
          addMain(el("div", "unsupported", "No transcript for agent type '" + msg.agent_type + "'."));
        } else if (msg.type === "error") {
          // The stream can't read this agent (host offline, etc.). Don't surface
          // raw text -- it's simply "not connected": red dot + locked composer.
          // The watchdog keeps retrying and recovers on its own.
          setConnState("offline");
          setStatus("not connected — this agent isn't reachable right now");
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
    // On mobile the tab is frozen while backgrounded: this watchdog's timer stops
    // and the socket is usually killed. Recover the instant the tab is foregrounded
    // instead of waiting for the next tick (and a possibly-wedged EventSource).
    function wakeChat() {
      if (!document.hidden && Date.now() - lastMsgAt > STALE_MS) forceReconnect();
    }
    document.addEventListener("visibilitychange", wakeChat);
    window.addEventListener("pageshow", wakeChat);
    window.addEventListener("focus", wakeChat);
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
    let awaiting = false;
    // The textbox is unusable while EITHER a dialog blocks the agent (NEEDS INPUT --
    // a paste would be eaten by the dialog) OR a send is awaiting its echo. Lock on
    // the union so clearing one state never re-enables the box while the other holds.
    function syncComposerLock() {
      const lock = blocked || awaiting;
      input.disabled = lock;
      sendBtn.disabled = lock;
    }
    function setBlocked() {
      blocked = true;
      composer.classList.add("blocked");
      if (composerBlocked) composerBlocked.hidden = false;
      syncComposerLock();
      refreshWorking(); // BLOCKED hides the dot immediately, not on the next event
    }
    function clearBlocked() {
      blocked = false;
      composer.classList.remove("blocked");
      if (composerBlocked) composerBlocked.hidden = true;
      syncComposerLock();
      refreshWorking(); // unblocking may reveal the dot again if still working
    }

    // Poll input-state: drives the composer's blocked/working UI and the tab
    // title. Keeps polling (slower) while hidden so the title stays live in a
    // background tab. Each poll is a tmux pane capture over SSH.
    const statePoll = installStatePolling(name, (d) => {
      // ONE derivation (statusKey) drives all three renderers on this page -- the tab
      // title, the composer's blocked state, and the working dot -- so they can never
      // disagree. Same statusKey the home cards use, so home + chat stay in lockstep.
      const k = statusKey(d);
      document.title = statusTitle(d, name + " — chat");
      if (k === "NEEDS_INPUT") setBlocked(); else clearBlocked();
      applyStatus(k);
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
    // echoes it back -- queued (dark green) or delivered (green). Only then do we
    // clear the box. So a bubble exists only once claude has actually recorded the
    // message: no guessing, no colour flip-flop.
    function setAwaiting(on) {
      awaiting = on;
      composer.classList.toggle("awaiting", on);
      syncComposerLock();
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

    // Attachments still transferring to the host when the user hits send: wait for
    // exactly those (never re-upload a finished one), so a send is instant once its
    // images have landed and blocks only for whatever transfer is still running.
    // Resolves false if a referenced attachment failed -> we refuse to deliver a
    // message that points at a file that never made it onto the host.
    function waitForUploadsIn(msg) {
      const re = new RegExp(FILE_TOKEN_SRC, "g");
      const waits = [];
      let failed = false;
      let m;
      while ((m = re.exec(msg)) !== null) {
        const entry = uploads.get(m[1]);
        if (!entry) continue; // already confirmed/cleared, or a hand-typed token
        if (entry.status === "done") continue; // transfer already landed -> no wait
        if (entry.status === "error") { failed = true; continue; }
        waits.push(entry.ready); // still in flight -> await just this transfer
      }
      if (!waits.length) return Promise.resolve(!failed);
      composer.classList.add("attaching");
      return Promise.all(waits).then(function (results) {
        composer.classList.remove("attaching");
        return !failed && results.every(function (r) { return r !== false; });
      });
    }

    // POST the (already attachment-resolved) message; the transcript echo confirms.
    function postMessage(msg) {
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
        .catch(() => {
          releaseSend();
          showError("not connected — your message wasn't sent. try again once reconnected.");
        });
    }

    function send() {
      if (composer.classList.contains("awaiting")) return; // already waiting on one
      if (!connLive) { showError("not connected — waiting to reconnect…"); return; }
      const raw = input.value.trim();
      // A message starting with "1." gets misread as an ordered-list / menu choice;
      // escape it to "#1." so it's sent as plain text.
      const msg = raw.startsWith("1.") ? "#" + raw : raw;
      if (!msg) return;
      sendErr.hidden = true;
      // Lock the input region and keep the text visible while we wait for the
      // transcript to confirm the message actually landed (confirmSend, above).
      pendingSend = msg;
      setAwaiting(true);
      if (pendingSendTimer) clearTimeout(pendingSendTimer);
      // Block ONLY on attachments in this message that are still transferring to the
      // host (started at paste time, not now), then deliver. An attachment-free or
      // already-transferred message posts immediately.
      waitForUploadsIn(msg).then(function (ready) {
        if (pendingSend !== msg) return; // superseded/cleared while we waited
        if (!ready) {
          composer.classList.remove("attaching");
          releaseSend();
          showError("an attachment didn't finish uploading — remove it (×) and retry.");
          return;
        }
        postMessage(msg);
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
        .catch(() => showError("not connected — couldn't interrupt."))
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
      // The whole point: the browser->foreman upload AND the foreman->agent-host
      // transfer both run NOW, in the background, the instant a file is attached --
      // not at send time. `ready` resolves true once that host transfer has landed
      // (false if it failed/aborted); a send awaits only the ones still in flight,
      // so an already-transferred attachment sends instantly and is never re-sent.
      let settle;
      const entry = {
        chip: chip, token: token, objectUrl: objectUrl, xhr: xhr, status: "uploading",
        ready: new Promise(function (res) { settle = res; }),
      };
      entry.settle = settle;
      xhr.upload.onprogress = function (e) {
        if (!e.lengthComputable) return;
        const p = Math.round((e.loaded / e.total) * 100);
        bar.style.width = p + "%";
        // <100% is the browser->foreman leg; at 100% the slow leg is the
        // foreman->host transfer we're still waiting on the response for.
        if (p >= 100) { pct.textContent = "transferring…"; chip.classList.add("finalizing"); }
        else { pct.textContent = p + "%"; }
      };
      xhr.onload = function () {
        let d = null;
        try { d = JSON.parse(xhr.responseText); } catch (_e) { /* non-json */ }
        if (xhr.status >= 200 && xhr.status < 300 && d && d.ok) {
          chip.classList.remove("uploading", "finalizing");
          chip.classList.add("done");
          if (prog.parentNode) prog.parentNode.removeChild(prog);
          entry.status = "done";
          entry.settle(true);
        } else {
          markUploadError(chip, (d && d.error) || ("HTTP " + xhr.status));
          entry.status = "error";
          entry.settle(false);
        }
      };
      xhr.onerror = function () {
        markUploadError(chip, "network error");
        entry.status = "error";
        entry.settle(false);
      };
      uploads.set(storedName, entry);
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
        // Unblock any send that is mid-wait on this now-removed attachment.
        if (entry.settle) { entry.status = "error"; entry.settle(false); }
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

    // Drag-and-drop onto the composer: same immediate background upload as paste.
    // Without these handlers the browser would just navigate to the dropped file.
    function hasFiles(e) {
      const types = e.dataTransfer && e.dataTransfer.types;
      return !!types && Array.prototype.indexOf.call(types, "Files") !== -1;
    }
    composer.addEventListener("dragover", function (e) {
      if (!hasFiles(e)) return;
      e.preventDefault();
      composer.classList.add("dragover");
    });
    composer.addEventListener("dragleave", function (e) {
      if (e.target === composer) composer.classList.remove("dragover");
    });
    composer.addEventListener("drop", function (e) {
      composer.classList.remove("dragover");
      const files = (e.dataTransfer && e.dataTransfer.files) || [];
      if (files.length) { e.preventDefault(); Array.prototype.forEach.call(files, uploadFile); }
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
      // xterm's own bypass for "app has mouse tracking on, plain drag goes to
      // the app instead of making a selection": holding a modifier during the
      // drag forces a local selection instead (SelectionService.shouldForceSelection).
      // Non-Mac checks Shift unconditionally; on Mac it checks Option/Alt but
      // ONLY if this option is on (off by default) -- so this is required for
      // Option+drag-to-select to do anything on a Mac.
      macOptionClickForcesSelection: true,
    });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(document.getElementById("term"));
    fit.fit();

    // ---- OSC 52 clipboard bridge ----
    // Claude's TUI (and tmux copy-mode) copy text OUT OF BAND via an OSC 52 escape
    // sequence (base64), NOT by making an xterm selection -- that's the "it went to
    // the tmux buffer but never my clipboard" case. xterm registers no OSC 52 handler
    // by default, so it was dropped on the floor. Catch it and put it on the real
    // system clipboard. Secure contexts (localhost/https) write immediately; over
    // plain http a clipboard write needs a user gesture but OSC 52 arrives async, so
    // stash it and flush on the next tap/keypress in the terminal.
    let pendingClip = null;
    function flushPendingClip() {
      if (pendingClip == null) return;
      copyViaTextarea(pendingClip); // execCommand path -- allowed, we're in a gesture
      pendingClip = null;
    }
    term.parser.registerOscHandler(52, (data) => {
      const semi = data.indexOf(";");
      const b64 = semi < 0 ? data : data.slice(semi + 1); // "c;<base64>" -> "<base64>"
      if (!b64 || b64 === "?") return true; // clipboard-READ request / empty -> ignore
      let text;
      try {
        text = decodeURIComponent(escape(atob(b64)));
      } catch (_e) {
        try { text = atob(b64); } catch (_e2) { return true; }
      }
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).catch(() => { pendingClip = text; });
      } else {
        pendingClip = text; // http: no async clipboard -> flush on next gesture
      }
      return true;
    });
    document.getElementById("term").addEventListener("pointerdown", flushPendingClip);
    document.addEventListener("keydown", flushPendingClip);

    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = proto + "//" + location.host + tgt.wsPath;
    const encoder = new TextEncoder();
    // The pane is a persistent tmux session, so a dropped socket loses nothing --
    // reconnecting just re-attaches (tmux redraws the current screen). Phones kill
    // the socket whenever the tab is backgrounded / the screen locks, so a terminal
    // with no reconnect looks "randomly disconnected". Reconnect on close with a
    // capped backoff, and -- crucially on mobile -- the instant the tab is
    // foregrounded again (the backoff timer is frozen while the tab is hidden).
    let ws = null;
    let reconnectTimer = null;
    let backoff = 1000;
    let closing = false;

    function wsSend(data) {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(typeof data === "string" ? encoder.encode(data) : data);
    }
    function sendResize() {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
    }
    function scheduleReconnect() {
      if (reconnectTimer || closing) return;
      reconnectTimer = setTimeout(() => { reconnectTimer = null; connectTerm(); }, backoff);
      backoff = Math.min(backoff * 2, 15000);
    }
    function connectTerm() {
      if (closing) return;
      if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
      setConn("reconnecting");
      // Bind handlers to this specific socket (via `sock`) rather than reading the
      // outer `ws`: wakeTerm() reconnects while the old socket is still CLOSING
      // (not just CLOSED), so its onclose can otherwise fire AFTER a new socket has
      // already replaced `ws` and stomp "live" back to "closed" / queue a spurious
      // reconnect for a connection that's already up.
      const sock = new WebSocket(wsUrl);
      ws = sock;
      sock.binaryType = "arraybuffer";
      sock.onopen = () => { if (ws !== sock) return; backoff = 1000; setConn("live"); fit.fit(); sendResize(); term.focus(); };
      sock.onmessage = (ev) => {
        if (ws !== sock) return;
        if (ev.data instanceof ArrayBuffer) term.write(new Uint8Array(ev.data));
        else if (typeof ev.data === "string") term.write(ev.data);
      };
      sock.onclose = () => { if (ws !== sock) return; setConn("closed"); scheduleReconnect(); };
      sock.onerror = () => { if (ws !== sock) return; setConn("error"); };
    }
    connectTerm();

    // Reconnect the moment the tab returns to the foreground (phone unlock / app
    // switch / bfcache restore): the socket is usually already dead but the frozen
    // backoff timer may be minutes from firing.
    function wakeTerm() {
      if (document.hidden) return;
      // A bfcache restore fires pagehide (closing=true) then later pageshow on the
      // *same* JS context -- closing must not stick past that, or a foregrounded
      // tab can never reconnect its terminal again.
      closing = false;
      if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
        if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
        backoff = 1000;
        connectTerm();
      }
    }
    document.addEventListener("visibilitychange", wakeTerm);
    window.addEventListener("pageshow", wakeTerm);
    window.addEventListener("focus", wakeTerm);
    window.addEventListener("pagehide", () => { closing = true; if (ws) { try { ws.close(); } catch (_e) {} } });

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
          if (key === "selmode") { setSelectMode(!selectMode); return; }
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

    // ---- highlight-to-copy ----
    // Selecting text in xterm doesn't copy on its own. We copy on the gesture that
    // ENDS the selection (mouseup / touchend) rather than a debounced timer, because
    // being inside a real user gesture is what lets the plain-http fallback work:
    // navigator.clipboard.writeText needs a secure context (https / http://localhost)
    // and is blocked over http://<remote-ip>:8700 (phone on the tailnet), but
    // document.execCommand("copy") from a hidden textarea DOES work there when called
    // synchronously inside a gesture. So: async clipboard first, execCommand fallback.
    // tmux nuance: a pane with `mouse on` captures a plain drag into tmux copy-mode,
    // so there the user must Shift+drag to make a browser selection; getSelection()
    // returns it either way. (This box's ~/.mngr/tmux.conf leaves mouse off.)
    function copyViaTextarea(text) {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", ""); // readonly -> selecting it won't pop the mobile keyboard
      ta.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;";
      document.body.appendChild(ta);
      ta.select();
      try { ta.setSelectionRange(0, text.length); } catch (_e) {}
      try { document.execCommand("copy"); } catch (_e) {}
      document.body.removeChild(ta);
    }
    function copySelection(e) {
      if (!term.hasSelection()) return;
      const sel = term.getSelection();
      if (!sel) return;
      if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(sel).catch(() => copyViaTextarea(sel));
      else copyViaTextarea(sel);
      // Restore terminal focus on desktop; on touch, refocusing xterm pops the keyboard.
      if (!e || e.type !== "touchend") term.focus();
    }
    const termElForCopy = document.getElementById("term");
    if (termElForCopy) {
      termElForCopy.addEventListener("mouseup", copySelection);
      termElForCopy.addEventListener("touchend", copySelection);
    }

    // ---- mobile "Select" mode: drag-select while the TUI has mouse tracking on ----
    // Desktop gets this for free: xterm's SelectionService forces a local selection
    // (instead of reporting the drag to the pty) whenever the drag is held with
    // Shift (any OS) or Option (Mac, via macOptionClickForcesSelection above) --
    // see the "Shift+drag to select" ctrlbar hint. Touch has no modifier key, so
    // the Select button retargets raw touch coordinates into the SAME
    // forced-modifier synthetic mouse events, reusing xterm's own selection +
    // drag-autoscroll instead of reimplementing hit-testing. While off, touch
    // behaves exactly as before (forwarded to the pane).
    let selectMode = false;
    const selBtn = bar && bar.querySelector('[data-seq="selmode"]');
    function setSelectMode(on) {
      selectMode = on;
      if (selBtn) selBtn.classList.toggle("active", on);
      if (termElForCopy) termElForCopy.classList.toggle("selecting", on);
    }
    function synthMouse(type, touch) {
      const target = term.element || termElForCopy;
      if (!target) return;
      target.dispatchEvent(new MouseEvent(type, {
        bubbles: true, cancelable: true, view: window,
        clientX: touch.clientX, clientY: touch.clientY,
        button: 0, buttons: type === "mouseup" ? 0 : 1,
        shiftKey: true, altKey: true, // force-selection bypass on every platform at once
      }));
    }
    if (termElForCopy) {
      termElForCopy.addEventListener("touchstart", (e) => {
        if (!selectMode || e.touches.length !== 1) return;
        e.preventDefault();
        synthMouse("mousedown", e.touches[0]);
      }, { passive: false });
      termElForCopy.addEventListener("touchmove", (e) => {
        if (!selectMode || e.touches.length !== 1) return;
        e.preventDefault();
        synthMouse("mousemove", e.touches[0]);
      }, { passive: false });
      termElForCopy.addEventListener("touchend", (e) => {
        if (!selectMode) return;
        e.preventDefault();
        const t = e.changedTouches[0];
        // Final mousemove pins the exact lift-off point before mouseup finalizes
        // the selection; the mouseup then bubbles into the mouseup->copySelection
        // listener above (the pre-existing touchend->copySelection listener also
        // still fires right after -- harmless, copySelection() is idempotent).
        if (t) { synthMouse("mousemove", t); synthMouse("mouseup", t); }
      }, { passive: false });
    }

    // Ctrl+Shift+V pastes (mirrors the Paste button); the shell keeps plain Ctrl+V.
    term.attachCustomKeyEventHandler((e) => {
      if (e.type === "keydown" && e.ctrlKey && e.shiftKey && (e.key === "v" || e.key === "V")) {
        doPaste();
        return false;
      }
      return true;
    });
  }

  window.foreman = { initIndex: initIndex, initAgent: initAgent, initTerminal: initTerminal };
})();
