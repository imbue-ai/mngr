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
    // Optimistic "queued" bubbles: a message sent while the agent is busy shows
    // immediately in purple, then swaps to a normal user bubble once it lands in
    // the transcript. Each entry: { text, node }.
    const pendingQueued = [];

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
      node.appendChild(document.createTextNode(text));
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

    function renderEvent(ev) {
      const wasBottom = atBottom();
      if (ev.type === "user_message") {
        // If we optimistically showed this as a "queued" bubble, drop the
        // placeholder -- the real delivered message renders normally below.
        resolveQueued(ev.content || "");
        const e = el("div", "entry user");
        e.appendChild(document.createTextNode(ev.content || ""));
        tEl.appendChild(e);
      } else if (ev.type === "framework_message") {
        tEl.appendChild(frameworkEl(ev));
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

    // Poll the input-state endpoint lazily: only while the tab is visible.
    // A single tmux pane capture over SSH per poll; paused when hidden.
    let pollTimer = null;
    function pollInputState() {
      fetch("/api/agents/" + encodeURIComponent(name) + "/input-state")
        .then((r) => r.json())
        .then((d) => {
          if (d.blocked) setBlocked(); else clearBlocked();
          applyMngrBusy(d.busy);
        })
        .catch(() => {});
    }
    function startInputStatePolling() {
      if (pollTimer || document.hidden) return;
      pollInputState();
      pollTimer = setInterval(pollInputState, 4000);
    }
    function stopInputStatePolling() {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    }
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stopInputStatePolling();
      else startInputStatePolling();
    });
    startInputStatePolling();

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
    function removeUpload(storedName) {
      const entry = uploads.get(storedName);
      if (!entry) return;
      removeTokenFromInput(entry.token);
      if (entry.chip && entry.chip.parentNode) entry.chip.parentNode.removeChild(entry.chip);
      if (entry.objectUrl) URL.revokeObjectURL(entry.objectUrl);
      uploads.delete(storedName);
      showStrip();
      // Best-effort delete of the remote file.
      fetch("/api/agents/" + encodeURIComponent(name) + "/upload/" + encodeURIComponent(storedName), { method: "DELETE" }).catch(() => {});
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
    document.title = "foreman · " + tgt.label + " · terminal";
    const back = document.getElementById("back");
    if (back) back.href = tgt.back;

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
