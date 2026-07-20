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

  // --- connection status ----------------------------------------------------
  function setConn(state) {
    const c = document.getElementById("conn");
    if (c) c.textContent = state;
  }

  // ==========================================================================
  // Index page: agent list
  // ==========================================================================
  function initIndex() {
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
    document.title = "foreman · " + name;
    const termlink = document.getElementById("termlink");
    if (termlink) termlink.href = "/a/" + encodeURIComponent(name) + "/terminal";
    const tEl = document.getElementById("transcript");
    const composer = document.getElementById("composer");
    const toolBodies = new Map(); // tool_call_id -> {body, toolName, resolved}
    const pendingResults = new Map(); // tool_call_id -> result event (arrived early)

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

      if (DIFF_TOOLS[tc.tool_name] && tc.input_full) {
        body.appendChild(diffBlock(tc.tool_name, tc.input_full));
        details.open = true;
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

    function attachResult(ev) {
      const entry = toolBodies.get(ev.tool_call_id);
      const pre = el("pre", "result" + (ev.is_error ? " error" : ""));
      pre.textContent = ev.output || "";
      // Long outputs collapse behind a summary; short ones show inline.
      if ((ev.output || "").length > 1200) {
        const det = el("details", "tool");
        const sm = el("summary");
        sm.appendChild(el("span", "tname", "output"));
        sm.appendChild(el("span", "targ", (ev.output || "").length + " chars"));
        det.appendChild(sm);
        const b = el("div", "body");
        b.appendChild(pre);
        det.appendChild(b);
        if (entry) entry.body.appendChild(det);
      } else if (entry) {
        entry.body.appendChild(pre);
      }
    }

    function renderEvent(ev) {
      const wasBottom = atBottom();
      if (ev.type === "user_message") {
        const e = el("div", "entry user");
        e.appendChild(el("span", "prompt", "> "));
        e.appendChild(document.createTextNode(ev.content || ""));
        tEl.appendChild(e);
      } else if (ev.type === "assistant_message") {
        const e = el("div", "entry assistant");
        if (ev.text && ev.text.trim()) {
          const md = el("div", "assistant-md");
          md.innerHTML = renderMarkdown(ev.text);
          e.appendChild(md);
        }
        (ev.tool_calls || []).forEach((tc) => e.appendChild(toolCallEl(tc)));
        if (e.childNodes.length) tEl.appendChild(e);
      } else if (ev.type === "tool_result") {
        if (toolBodies.has(ev.tool_call_id)) attachResult(ev);
        else pendingResults.set(ev.tool_call_id, ev);
      }
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
      sendBtn.disabled = true;
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
          } else {
            showError((d && d.error) || "send failed. Try the terminal page (phase 2) for blocking prompts.");
          }
        })
        .catch((e) => showError("network error: " + e))
        .finally(() => { sendBtn.disabled = false; input.focus(); });
    }

    sendBtn.addEventListener("click", send);
    input.addEventListener("keydown", (e) => {
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
  }

  // ==========================================================================
  // Terminal page: xterm.js <-> pty websocket bridge
  // ==========================================================================
  function terminalNameFromPath() {
    const m = location.pathname.match(/^\/a\/(.+)\/terminal$/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function initTerminal() {
    const name = terminalNameFromPath();
    document.getElementById("agent-name").textContent = name;
    document.title = "foreman · " + name + " · terminal";
    const back = document.getElementById("back");
    if (back) back.href = "/a/" + encodeURIComponent(name);

    if (typeof Terminal === "undefined") {
      setConn("xterm failed to load");
      return;
    }
    const term = new Terminal({
      cursorBlink: true,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      fontSize: 13,
      theme: { background: "#0a0c0f", foreground: "#d7dce2" },
    });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(document.getElementById("term"));
    fit.fit();

    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = proto + "//" + location.host + "/ws/agents/" + encodeURIComponent(name) + "/terminal";
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";

    function sendResize() {
      if (ws.readyState !== WebSocket.OPEN) return;
      ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
    }

    ws.onopen = () => {
      setConn("live");
      fit.fit();
      sendResize();
      term.focus();
    };
    ws.onmessage = (ev) => {
      // Binary frames are terminal output; decode as UTF-8 and write.
      if (ev.data instanceof ArrayBuffer) {
        term.write(new Uint8Array(ev.data));
      } else if (typeof ev.data === "string") {
        term.write(ev.data);
      }
    };
    ws.onclose = () => { setConn("closed"); term.write("\r\n\x1b[90m[disconnected]\x1b[0m\r\n"); };
    ws.onerror = () => setConn("error");

    // Keystrokes -> pty as binary.
    const encoder = new TextEncoder();
    term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(encoder.encode(data));
    });

    // Reflow on rotate/resize; debounce so we don't spam winsize changes.
    let resizeTimer = null;
    function onResize() {
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => { fit.fit(); sendResize(); }, 120);
    }
    window.addEventListener("resize", onResize);
    window.addEventListener("orientationchange", onResize);
  }

  window.foreman = { initIndex: initIndex, initAgent: initAgent, initTerminal: initTerminal };
})();
