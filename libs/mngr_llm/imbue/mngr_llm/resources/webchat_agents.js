/**
 * Agents page plugin for llm-webchat.
 *
 * Registers an /agents route via $llm.registerRoute() and adds an "Agents"
 * link to the sidebar. Fetches agent data from GET /api/agents when the
 * route is visited.
 */

// Load stylesheet immediately (no dependency on $llm).
(function () {
  var linkElement = document.createElement("link");
  linkElement.rel = "stylesheet";
  linkElement.href = "/plugins/webchat_agents.css";
  document.head.appendChild(linkElement);
})();

// Defer the rest until "load" so that the main app's ES module has
// executed and window.$llm is available. Plugin "load" listeners are
// registered before the module's listener (regular scripts run first),
// so this fires after $llm is set but before bootstrap() reads the
// plugin route map.
window.addEventListener("load", function () {
  "use strict";

  var AGENTS_ROUTE = "/agents";

  // ── SVG icons ────────────────────────────────────────────────

  var AGENTS_ICON_SVG =
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>' +
    '<circle cx="9" cy="7" r="4"/>' +
    '<path d="M23 21v-2a4 4 0 0 0-3-3.87"/>' +
    '<path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>';

  var BACK_ARROW_SVG =
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M19 12H5"/><path d="M12 19l-7-7 7-7"/></svg>';

  // ── State ────────────────────────────────────────────────────

  var agents = [];
  var loadError = null;

  // ── Helpers ──────────────────────────────────────────────────

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  function badgeClass(state) {
    var lower = (state || "unknown").toLowerCase();
    if (lower === "running") return "agents-badge agents-badge-running";
    if (lower === "stopped") return "agents-badge agents-badge-stopped";
    if (lower === "waiting") return "agents-badge agents-badge-waiting";
    return "agents-badge agents-badge-unknown";
  }

  function isAgentsRoute() {
    // m.route.prefix is "" so m.route.get() returns the pathname
    try {
      return window.location.pathname === AGENTS_ROUTE;
    } catch (e) {
      return false;
    }
  }

  // ── Data fetching ────────────────────────────────────────────

  function fetchAgents(container) {
    fetch("/api/agents")
      .then(function (response) {
        if (!response.ok)
          throw new Error("Failed to fetch agents: " + response.status);
        return response.json();
      })
      .then(function (data) {
        agents = data.agents || [];
        loadError = null;
        renderAgentsContent(container);
      })
      .catch(function (error) {
        console.error("[agents-plugin]", error);
        loadError = error.message;
        renderAgentsContent(container);
      });
  }

  // ── Rendering ────────────────────────────────────────────────

  function buildAgentsListHtml() {
    var html = "";

    if (loadError) {
      html += '<p class="agents-error">Error loading agents: ' + escapeHtml(loadError) + "</p>";
    } else if (agents.length === 0) {
      html += '<p class="agents-empty">No agents found on this host.</p>';
    } else {
      html += '<ul class="agents-list">';
      for (var i = 0; i < agents.length; i++) {
        var agent = agents[i];
        var name = agent.name || "unnamed";
        var state = (agent.state || "unknown").toUpperCase();
        var stateLower = (agent.state || "unknown").toLowerCase();
        html +=
          '<li class="agents-item">' +
          '<span class="agents-item-name">' +
          escapeHtml(name) +
          "</span>" +
          '<span class="' +
          badgeClass(stateLower) +
          '">' +
          escapeHtml(state) +
          "</span>" +
          "</li>";
      }
      html += "</ul>";
    }

    return html;
  }

  function renderAgentsContent(container) {
    // Find or create the list container within the page wrapper
    var listContainer = container.querySelector(".agents-list-container");
    if (listContainer) {
      listContainer.innerHTML = buildAgentsListHtml();
      return;
    }

    // First render: build the full page structure
    container.innerHTML =
      '<div class="agents-page">' +
      '<div class="agents-page-header">' +
      '<a class="agents-back-link" href="/">' +
      BACK_ARROW_SVG +
      "Conversations" +
      "</a>" +
      '<span class="agents-page-title">Agents</span>' +
      "</div>" +
      '<div class="agents-list-container">' +
      buildAgentsListHtml() +
      "</div>" +
      "</div>";

    // Wire up the back link to use mithril routing
    var backLink = container.querySelector(".agents-back-link");
    if (backLink) {
      backLink.addEventListener("click", function (event) {
        event.preventDefault();
        history.pushState(null, "", "/");
        window.dispatchEvent(new PopStateEvent("popstate"));
      });
    }
  }

  // ── Route registration (must happen before "ready") ──────────

  var currentContainer = null;

  $llm.registerRoute(AGENTS_ROUTE, {
    render: function (container) {
      currentContainer = container;
      fetchAgents(container);
    },
    destroy: function () {
      currentContainer = null;
    },
  });

  // ── Sidebar link ─────────────────────────────────────────────

  function updateSidebarLinkState() {
    var link = document.querySelector(".agents-sidebar-link");
    if (link) {
      if (isAgentsRoute()) {
        link.classList.add("active");
      } else {
        link.classList.remove("active");
      }
    }
  }

  function injectSidebarLink() {
    var slot = document.querySelector('[data-slot="sidebar-before-list"]');
    if (!slot) {
      slot = document.querySelector(".conversation-selector");
    }
    if (!slot) return false;

    // Avoid duplicate injection
    if (document.querySelector(".agents-sidebar-link")) return true;

    var link = document.createElement("a");
    link.className = "agents-sidebar-link";
    link.href = AGENTS_ROUTE;
    link.innerHTML = AGENTS_ICON_SVG + "<span>Agents</span>";
    link.addEventListener("click", function (event) {
      event.preventDefault();
      history.pushState(null, "", AGENTS_ROUTE);
      window.dispatchEvent(new PopStateEvent("popstate"));
    });

    slot.parentNode.insertBefore(link, slot);
    updateSidebarLinkState();
    return true;
  }

  // ── Initialization ───────────────────────────────────────────

  $llm.on("ready", function () {
    injectSidebarLink();

    // Re-inject after mithril re-renders (the sidebar may be re-created)
    var observer = new MutationObserver(function () {
      if (!document.querySelector(".agents-sidebar-link")) {
        injectSidebarLink();
      }
      updateSidebarLinkState();
    });
    var sidebar = document.querySelector('[data-slot="sidebar"]');
    if (sidebar) {
      observer.observe(sidebar, { childList: true, subtree: true });
    }
  });
});
