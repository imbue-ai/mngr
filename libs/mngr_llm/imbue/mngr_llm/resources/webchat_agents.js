/**
 * Agents page plugin for llm-webchat.
 *
 * Registers an /agents route via $llm.registerRoute() and adds an "Agents"
 * link to the sidebar. Fetches agent data from GET /api/agents when the
 * route is visited.
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

  function isAgentsRoute() {
    var expected = basePath + AGENTS_ROUTE;
    try {
      return window.location.pathname === expected;
    } catch (e) {
      return false;
    }
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

    // Replace the sidebar branding title -- but only when it differs,
    // to avoid a DOM mutation that would re-trigger our MutationObserver
    // and cause an infinite loop.
    var title = document.querySelector(".sidebar-branding-title");
    if (title && title.textContent !== agentName) {
      title.textContent = agentName;
    }

    // Replace the document title
    if (document.title !== agentName) {
      document.title = agentName;
    }
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
      "Conversations" +
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

  // ── Sidebar link ─────────────────────────────────────────────

  function navigateToAgents() {
    history.pushState(null, "", basePath + AGENTS_ROUTE);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }

  function updateSidebarLinkState() {
    var active = isAgentsRoute();

    var pill = document.querySelector(".agents-sidebar-link");
    if (pill) {
      if (active) {
        pill.classList.add("active");
      } else {
        pill.classList.remove("active");
      }
    }

    var collapsedBtn = document.querySelector(".agents-sidebar-collapsed-button");
    if (collapsedBtn) {
      if (active) {
        collapsedBtn.classList.add("active");
      } else {
        collapsedBtn.classList.remove("active");
      }
    }
  }

  function injectSidebarLink() {
    var injectedExpanded = !!document.querySelector(".agents-sidebar-link");
    var injectedCollapsed = !!document.querySelector(".agents-sidebar-collapsed-button");

    // Inject the pill into the expanded sidebar, between the
    // sidebar-header slot and the conversation selector.
    if (!injectedExpanded) {
      var sidebarHeader = document.querySelector(
        ".sidebar-expanded-content [data-slot='sidebar-header']"
      );
      if (sidebarHeader && sidebarHeader.nextSibling) {
        var pill = document.createElement("a");
        pill.className = "agents-sidebar-link";
        pill.href = basePath + AGENTS_ROUTE;
        pill.innerHTML = AGENTS_ICON_SVG + "<span>Agents</span>";
        pill.addEventListener("click", function (event) {
          event.preventDefault();
          navigateToAgents();
        });
        sidebarHeader.parentNode.insertBefore(pill, sidebarHeader.nextSibling);
        injectedExpanded = true;
      }
    }

    // Inject an icon button into the collapsed sidebar content.
    if (!injectedCollapsed) {
      var collapsedContent = document.querySelector(".sidebar-collapsed-content");
      if (collapsedContent) {
        var btn = document.createElement("a");
        btn.className = "agents-sidebar-collapsed-button";
        btn.href = basePath + AGENTS_ROUTE;
        btn.title = "Agents";
        btn.setAttribute("aria-label", "Agents");
        btn.innerHTML = AGENTS_ICON_SVG;
        btn.addEventListener("click", function (event) {
          event.preventDefault();
          navigateToAgents();
        });
        collapsedContent.appendChild(btn);
        injectedCollapsed = true;
      }
    }

    updateSidebarLinkState();
    return injectedExpanded || injectedCollapsed;
  }

  // ── Initialization ───────────────────────────────────────────

  $llm.on("ready", function () {
    injectSidebarLink();
    fetchAgentName();

    // Re-inject after mithril re-renders (the sidebar may be
    // re-created).  Observe the #app container rather than the
    // sidebar element itself, because mithril may replace the
    // entire sidebar DOM node -- which would disconnect an
    // observer attached to it.
    var appRoot = document.getElementById("app");
    if (appRoot) {
      var observer = new MutationObserver(function () {
        if (
          !document.querySelector(".agents-sidebar-link") ||
          !document.querySelector(".agents-sidebar-collapsed-button")
        ) {
          injectSidebarLink();
        }
        updateSidebarLinkState();
        applyAgentBranding();
      });
      observer.observe(appRoot, { childList: true, subtree: true });
    }
  });
});
