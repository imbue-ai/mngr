import json
import subprocess
import threading
from typing import Final

from fastcore.xml import FT
from fasthtml.common import Div
from fasthtml.common import Html
from fasthtml.common import Iframe
from fasthtml.common import Input
from fasthtml.common import Li
from fasthtml.common import Link
from fasthtml.common import Main
from fasthtml.common import Meta
from fasthtml.common import Nav
from fasthtml.common import Script
from fasthtml.common import Span
from fasthtml.common import Style
from fasthtml.common import Title
from fasthtml.common import Ul
from fasthtml.common import fast_app
from loguru import logger
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.sculptor_web.data_types import AgentDisplayInfo
from imbue.sculptor_web.data_types import AgentHostInfo
from imbue.sculptor_web.data_types import AgentListResult
from imbue.sculptor_web.data_types import AgentStatusInfo
from imbue.sculptor_web.errors import AgentListingError

POLL_INTERVAL_SECONDS: Final[float] = 2.0
DEFAULT_PORT: Final[int] = 8765
DEFAULT_AGENT_URL: Final[str] = "https://en.wikipedia.org/wiki/Main_Page"


class AgentListState(MutableModel):
    """Mutable state holding the current list of agents."""

    agents: tuple[AgentDisplayInfo, ...] = Field(default=(), description="Current list of agents")
    errors: tuple[str, ...] = Field(default=(), description="Errors from last poll")
    is_running: bool = Field(default=False, description="Whether the polling thread is running")


# Global state for the agent list
_agent_list_state = AgentListState()
_poll_thread: threading.Thread | None = None
_stop_event = threading.Event()


def _run_mngr_list() -> AgentListResult:
    """Run mngr list command and parse the JSON output."""
    logger.debug("Running mngr list command")
    try:
        result = subprocess.run(
            ["mngr", "list", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            error_message = result.stderr.strip() if result.stderr else f"Exit code {result.returncode}"
            raise AgentListingError(error_message)

        # Parse the JSON output
        data = json.loads(result.stdout)

        # Convert to our data types
        agents_data = data.get("agents", [])
        errors_data = data.get("errors", [])

        agents = tuple(_parse_agent_info(agent_data) for agent_data in agents_data)
        errors = tuple(str(error) for error in errors_data)

        return AgentListResult(agents=agents, errors=errors)

    except subprocess.TimeoutExpired as e:
        raise AgentListingError("Command timed out") from e
    except json.JSONDecodeError as e:
        raise AgentListingError(f"Invalid JSON output: {e}") from e


@pure
def _parse_agent_info(data: dict) -> AgentDisplayInfo:
    """Parse agent info from JSON data."""
    host_data = data.get("host", {})
    host_info = AgentHostInfo(
        id=host_data.get("id", ""),
        name=host_data.get("name", ""),
        provider_name=host_data.get("provider_name", ""),
    )

    status_data = data.get("status")
    status_info = AgentStatusInfo(line=status_data.get("line", "")) if status_data else None

    # Extract state value
    state_data = data.get("state", "")
    if isinstance(state_data, dict):
        state = state_data.get("value", "").lower()
    else:
        state = str(state_data).lower()

    return AgentDisplayInfo(
        id=data.get("id", ""),
        name=data.get("name", ""),
        type=data.get("type", ""),
        command=data.get("command", ""),
        work_dir=data.get("work_dir", "."),
        create_time=data.get("create_time", "1970-01-01T00:00:00Z"),
        start_on_boot=data.get("start_on_boot", False),
        state=state,
        status=status_info,
        url=data.get("url"),
        start_time=data.get("start_time"),
        runtime_seconds=data.get("runtime_seconds"),
        host=host_info,
        plugin=data.get("plugin", {}),
    )


def _poll_agents_loop() -> None:
    """Background loop that polls mngr list periodically."""
    logger.info("Starting agent polling loop")
    _agent_list_state.is_running = True

    while not _stop_event.is_set():
        try:
            result = _run_mngr_list()
            _agent_list_state.agents = result.agents
            _agent_list_state.errors = result.errors
            logger.trace("Polled {} agents", len(result.agents))
        except AgentListingError as e:
            logger.warning("Failed to list agents: {}", e.message)
            _agent_list_state.errors = (e.message,)
        except Exception as e:
            logger.exception("Unexpected error while polling agents: {}", e)
            _agent_list_state.errors = (str(e),)

        # Wait for the next poll interval or stop signal
        _stop_event.wait(POLL_INTERVAL_SECONDS)

    _agent_list_state.is_running = False
    logger.info("Agent polling loop stopped")


def _start_polling() -> None:
    """Start the background polling thread."""
    global _poll_thread
    if _poll_thread is not None and _poll_thread.is_alive():
        return

    _stop_event.clear()
    _poll_thread = threading.Thread(target=_poll_agents_loop, daemon=True)
    _poll_thread.start()


def _stop_polling() -> None:
    """Stop the background polling thread."""
    global _poll_thread
    _stop_event.set()
    if _poll_thread is not None:
        _poll_thread.join(timeout=5.0)
        _poll_thread = None


# === FastHTML App ===

app, rt = fast_app()


@pure
def _render_page_head() -> tuple:
    """Render the page head elements."""
    return (
        Meta(charset="utf-8"),
        Meta(name="viewport", content="width=device-width, initial-scale=1"),
        Title("Sculptor Web - Agent Manager"),
        Link(rel="stylesheet", href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css"),
        Style(_CSS_STYLES),
    )


@pure
def _render_sidebar(agents: tuple[AgentDisplayInfo, ...], selected_agent_id: str | None) -> FT:
    """Render the sidebar with the list of agents."""
    agent_items = []
    for agent in agents:
        is_selected = agent.id == selected_agent_id
        state_class = f"state-{agent.state}"
        selected_class = "selected" if is_selected else ""

        status_text = agent.status.line if agent.status else ""
        agent_url = agent.url if agent.url else DEFAULT_AGENT_URL
        agent_items.append(
            Li(
                Div(
                    Span(agent.name, cls="agent-name"),
                    Span(agent.state, cls=f"agent-state {state_class}"),
                    Span(status_text, cls="agent-status") if status_text else None,
                    cls="agent-item-content",
                ),
                cls=f"agent-item {selected_class}",
                data_agent_id=agent.id,
                data_agent_name=agent.name.lower(),
                data_agent_url=agent_url,
                onclick="selectAgent(this)",
            )
        )

    return Nav(
        Div(
            Input(
                type="text",
                placeholder="Search agents...",
                cls="search-input",
                id="agent-search",
                oninput="filterAgents(this.value)",
                autocomplete="off",
            ),
            Ul(*agent_items, cls="agent-list", id="agent-list"),
            cls="sidebar-content",
        ),
        cls="sidebar",
        id="sidebar",
        hx_get="/sidebar",
        hx_trigger="every 2s",
        hx_swap="outerHTML",
    )


@pure
def _render_main_content(agents: tuple[AgentDisplayInfo, ...]) -> FT:
    """Render the main content area with iframes (lazy-loaded on first click)."""
    iframes = []
    for agent in agents:
        agent_url = agent.url if agent.url else DEFAULT_AGENT_URL
        # Use data-src instead of src to prevent loading until clicked
        iframes.append(
            Iframe(
                cls="agent-iframe",
                id=f"iframe-{agent.id}",
                data_src=agent_url,
                style="display: none;",
            )
        )

    # Always show placeholder initially until an agent is selected
    placeholder = Div(
        "No agents found. Create an agent with 'mngr create' to get started."
        if not agents
        else "Select an agent from the sidebar",
        cls="placeholder",
        id="placeholder",
    )
    return Main(placeholder, *iframes, cls="main-content", id="main-content")


@rt("/")
def get():
    """Render the main page."""
    agents = _agent_list_state.agents

    return Html(
        *_render_page_head(),
        Div(
            _render_sidebar(agents, selected_agent_id=None),
            _render_main_content(agents),
            cls="app-container",
        ),
        Script(_JS_CODE),
    )


@rt("/sidebar")
def get_sidebar():
    """Get the sidebar content for HTMX polling."""
    agents = _agent_list_state.agents
    # Don't pre-select any agent; selection is handled client-side
    return _render_sidebar(agents, selected_agent_id=None)


# === CSS Styles ===

_CSS_STYLES: Final[str] = """
:root {
    --sidebar-width: 300px;
    --sidebar-bg: #1a1a2e;
    --sidebar-text: #e0e0e0;
    --selected-bg: #16213e;
    --state-running: #4ade80;
    --state-stopped: #f87171;
    --state-waiting: #facc15;
}

* {
    margin: 0;
    padding: 0;
    box-sizing: border-box;
}

html, body {
    height: 100%;
    overflow: hidden;
}

.app-container {
    display: flex;
    height: 100vh;
    width: 100vw;
}

.sidebar {
    width: var(--sidebar-width);
    min-width: var(--sidebar-width);
    background: var(--sidebar-bg);
    color: var(--sidebar-text);
    display: flex;
    flex-direction: column;
    overflow: hidden;
}

.sidebar-content {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    padding: 1rem;
}

.search-input {
    width: 100%;
    padding: 0.75rem;
    margin-bottom: 1rem;
    border: 1px solid rgba(255, 255, 255, 0.2);
    border-radius: 0.5rem;
    background: rgba(255, 255, 255, 0.05);
    color: var(--sidebar-text);
    font-size: 0.95rem;
    outline: none;
    transition: border-color 0.2s, background 0.2s;
}

.search-input:focus {
    border-color: rgba(255, 255, 255, 0.4);
    background: rgba(255, 255, 255, 0.1);
}

.search-input::placeholder {
    color: rgba(255, 255, 255, 0.4);
}

.agent-list {
    list-style: none;
    display: block;
    flex: 1;
    overflow-y: auto;
    margin: 0;
    padding: 0;
}

.agent-item {
    display: block;
    padding: 0.75rem;
    margin-bottom: 0.5rem;
    border-radius: 0.5rem;
    cursor: pointer;
    transition: background 0.2s;
}

.agent-item:hover {
    background: rgba(255, 255, 255, 0.1);
}

.agent-item.selected {
    background: var(--selected-bg);
}

.agent-item-content {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
}

.agent-name {
    font-weight: 600;
    font-size: 0.95rem;
}

.agent-state {
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
}

.agent-state.state-running {
    color: var(--state-running);
}

.agent-state.state-stopped {
    color: var(--state-stopped);
}

.agent-state.state-waiting {
    color: var(--state-waiting);
}

.agent-status {
    font-size: 0.8rem;
    color: rgba(255, 255, 255, 0.6);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.main-content {
    flex: 1;
    position: relative;
    background: #0f0f23;
}

.agent-iframe {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    border: none;
}

.placeholder {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 100%;
    color: #666;
    font-size: 1.1rem;
}
"""

# === JavaScript Code ===

_JS_CODE: Final[str] = """
let selectedAgentId = null;
let currentSearchValue = '';

function selectAgent(element) {
    const agentId = element.dataset.agentId;

    // Update selected state in sidebar
    document.querySelectorAll('.agent-item').forEach(item => {
        item.classList.remove('selected');
    });
    element.classList.add('selected');

    selectedAgentId = agentId;

    // Hide all iframes and placeholder
    document.querySelectorAll('.agent-iframe').forEach(iframe => {
        iframe.style.display = 'none';
    });

    const iframe = document.getElementById('iframe-' + agentId);
    const placeholder = document.getElementById('placeholder');

    if (iframe) {
        // Lazy load: set src from data-src on first click
        if (!iframe.src && iframe.dataset.src) {
            iframe.src = iframe.dataset.src;
        }
        iframe.style.display = 'block';
        if (placeholder) placeholder.style.display = 'none';
    } else {
        if (placeholder) placeholder.style.display = 'flex';
    }
}

function filterAgents(searchValue) {
    currentSearchValue = searchValue.toLowerCase();
    document.querySelectorAll('.agent-item').forEach(item => {
        const agentName = item.dataset.agentName || '';
        if (agentName.includes(currentSearchValue)) {
            item.style.display = 'block';
        } else {
            item.style.display = 'none';
        }
    });
}

// Preserve selection and search after HTMX updates the sidebar
document.body.addEventListener('htmx:afterSwap', function(event) {
    if (event.detail.target.id === 'sidebar') {
        // Restore search value
        const searchInput = document.getElementById('agent-search');
        if (searchInput && currentSearchValue) {
            searchInput.value = currentSearchValue;
            filterAgents(currentSearchValue);
        }
        // Restore selection
        if (selectedAgentId) {
            const selectedItem = document.querySelector('[data-agent-id="' + selectedAgentId + '"]');
            if (selectedItem) {
                selectedItem.classList.add('selected');
            }
        }
    }
});
"""


def run() -> None:
    """Entry point for the sculptor_web CLI."""
    import uvicorn

    logger.info("Starting Sculptor Web on port {}", DEFAULT_PORT)

    # Start the background polling thread
    _start_polling()

    try:
        # Run the FastHTML server using uvicorn directly
        # We pass the app instance directly since serve() doesn't work well with entry points
        uvicorn.run(app, host="0.0.0.0", port=DEFAULT_PORT)
    finally:
        # Stop the polling thread when the server stops
        _stop_polling()
