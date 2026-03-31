/**
 * Agents page plugin for llm-webchat.
 *
 * Registers an /agents route via $llm.registerRoute() and adds an "Agents"
 * link to the sidebar via $llm.registerSidebarItem(). Fetches agent data
 * from GET /api/agents when the route is visited.
 */

// Defer everything until "load" so that the main app's ES module has
// executed and window.$llm is available. Plugin "load" listeners are
// registered before the module's listener (regular scripts run first),
// so this fires after $llm is set but before bootstrap() reads the
// plugin route map.
window.addEventListener("load", function () {
  "use strict";

  var AGENTS_ROUTE = "/agents";

  // ── Base path ────────────────────────────────────────────────

  /**
   * Read the root path that the ASGI server was mounted at.  The
   * llm-webchat server injects this into the HTML as a meta tag.
   * All absolute URLs (fetch, hrefs, pushState) must be prefixed
   * with this value so the plugin works behind a reverse proxy.
   */
  function getBasePath() {
    var meta = document.querySelector('meta[name="llm-webchat-base-path"]');
    return ((meta && meta.getAttribute("content")) || "").replace(/\/+$/, "");
  }

  var basePath = getBasePath();

  // Load stylesheet using the base path so it resolves correctly
  // behind a reverse proxy.
  (function () {
    var linkElement = document.createElement("link");
    linkElement.rel = "stylesheet";
    linkElement.href = basePath + "/plugins/webchat_agents.css";
    document.head.appendChild(linkElement);
  })();

  // ── SVG icon paths ───────────────────────────────────────────

  var AGENTS_ICON_PATHS =
    '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>' +
    '<circle cx="9" cy="7" r="4"/>' +
    '<path d="M23 21v-2a4 4 0 0 0-3-3.87"/>' +
    '<path d="M16 3.13a4 4 0 0 1 0 7.75"/>';

  var BACK_ARROW_SVG =
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M19 12H5"/><path d="M12 19l-7-7 7-7"/></svg>';

  // ── State ────────────────────────────────────────────────────

  var agents = [];
  var loadError = null;
  var agentName = null;

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

  // ── Data fetching ────────────────────────────────────────────

  function fetchAgents(container) {
    // Show a loading spinner immediately while the fetch is in flight.
    renderAgentsLoading(container);

    fetch(basePath + "/api/agents")
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

  // ── Agent name branding ───────────────────────────────────────

  // Claim the sidebar-branding slot so the upstream app does not
  // render its default "LLM Webchat" title. Once claimed, mithril
  // will not touch the slot element, so we can safely render into
  // it from the "ready" callback.
  $llm.claim("sidebar-branding");

  function fetchAgentName() {
    fetch(basePath + "/api/agent-info")
      .then(function (response) {
        if (!response.ok) return;
        return response.json();
      })
      .then(function (data) {
        if (!data || !data.name) return;
        agentName = data.name;
        applyAgentBranding();
      })
      .catch(function (error) {
        console.error("[agents-plugin] Failed to fetch agent info:", error);
      });
  }

  function applyAgentBranding() {
    if (!agentName) return;

    // Render the agent name into the claimed sidebar-branding slot.
    var slot = document.querySelector('[data-slot="sidebar-branding"]');
    if (slot && slot.textContent !== agentName) {
      slot.textContent = agentName;
    }

    // Replace the document title
    if (document.title !== agentName) {
      document.title = agentName;
    }
  }

  // Re-apply branding whenever the upstream app (re-)creates the
  // claimed slot element -- e.g. after a route change that causes
  // the sidebar component to be fully recreated.
  $llm.on("slot_rendered", function (event) {
    if (event.slotName === "sidebar-branding") {
      applyAgentBranding();
    }
    return event;
  });

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

  function navigateHome() {
    // Use mithril routing to navigate to the home page.  This keeps
    // the SPA state consistent and avoids a full page reload.
    history.pushState(null, "", basePath + "/");
    window.dispatchEvent(new PopStateEvent("popstate"));
  }

  function renderAgentsLoading(container) {
    container.innerHTML =
      '<div class="agents-page">' +
      '<div class="agents-page-header">' +
      '<a class="agents-back-link" href="' + basePath + '/">' +
      BACK_ARROW_SVG +
      "Back" +
      "</a>" +
      '<span class="agents-page-title">Agents</span>' +
      "</div>" +
      '<div class="agents-loading">' +
      '<div class="agents-spinner"></div>' +
      "</div>" +
      "</div>";

    var backLink = container.querySelector(".agents-back-link");
    if (backLink) {
      backLink.addEventListener("click", function (event) {
        event.preventDefault();
        navigateHome();
      });
    }
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
      '<a class="agents-back-link" href="' + basePath + '/">' +
      BACK_ARROW_SVG +
      "Back" +
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
        navigateHome();
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

  // ── Sidebar item ─────────────────────────────────────────────

  $llm.registerSidebarItem({
    route: AGENTS_ROUTE,
    name: "Agents",
    icon: AGENTS_ICON_PATHS,
  });

  // ── Initialization ───────────────────────────────────────────

  $llm.on("ready", function () {
    fetchAgentName();
  });
});
