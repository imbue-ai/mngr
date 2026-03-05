## ARCHITECTURE OVERVIEW: Imbue mng Monorepo

### Project Overview

The **mng** project is a framework for creating and managing teams of AI engineering agents across local and remote environments. It provides a unified CLI that enables users to spawn AI agents (like Claude Code or Codex) with full control over execution environment (local, Docker, Modal), automatic resource management, idle detection, and a comprehensive plugin system for extensibility.

**Key Mission:** Make it easy to create, deploy, and manage AI coding agents at scale, whether running locally or on remote platforms, with automatic cost optimization through idle detection and shutdown.

---

### Repository Structure

This is a **monorepo** organized into three primary directories:

#### **Root Level** (`/home/user/mng/`)
- `pyproject.toml` - Workspace configuration defining all member packages
- `CLAUDE.md` - Critical project instructions (overrides all defaults)
- `README.md` - User-facing documentation
- `style_guide.md` - Code style requirements
- `conftest.py` - Root pytest configuration
- `justfile` - Task automation commands
- Other config files (`.pre-commit-config.yaml`, `.gitignore`, etc.)

#### **`libs/`** - Core Libraries and Plugins (10 packages)

**Core System:**
1. **`libs/mng/`** (v0.1.4) - **The main mng framework**
   - Manages agent lifecycle, host providers, and CLI commands
   - Implements multi-layer architecture: main → CLI → API → agents → providers → hosts → interfaces → config → utils → primitives
   - Supports multiple providers: Local, Docker, Modal
   - Includes idle detection and automatic resource cleanup

2. **`libs/imbue_common/`** (v0.1.4) - **Shared core utilities**
   - Reusable libraries shared across all Imbue projects
   - Includes logging, validation, HTTP client utilities
   - Provides testing fixtures and infrastructure

3. **`libs/concurrency_group/`** (v0.1.4) - **Thread/process management**
   - Ensures proper cleanup of concurrent primitives
   - Prevents thread/process leaks with context manager pattern
   - Nested group support and shutdown signaling

**mng Plugins** (Extensions to core mng):
4. **`libs/mng_pair/`** (v0.1.0) - **Continuous file sync**
   - Plugin providing `mng pair` command
   - Syncs files between agent and local directory in real-time

5. **`libs/mng_schedule/`** (v0.1.0) - **Scheduled agent runs**
   - Plugin providing `mng schedule` command
   - Cron-based scheduling of mng commands on local and remote providers
   - Handles environment/dependency packaging for remote execution

6. **`libs/mng_opencode/`** (v0.1.0) - **OpenCode agent type**
   - Plugin providing OpenCode agent type option
   - Minimal dependencies, only depends on mng

7. **`libs/mng_tutor/`** (v0.1.0) - **Interactive tutorial**
   - Plugin providing `mng tutor` command
   - Interactive TUI for learning mng

8. **`libs/mng_kanpan/`** (v0.1.0) - **Agent tracking dashboard**
   - Plugin providing `mng kanpan` command
   - All-seeing agent tracker aggregating state from mng, git, GitHub PRs, CI

**Infrastructure:**
9. **`libs/flexmux/`** (v0.1.0) - **Tab manager**
   - Simple FlexLayout-based tab manager
   - Flask backend with Pydantic models
   - Minimal dependencies

10. **`libs/mng_opencode/`** - *Listed but appears to be duplicate or referenced differently*

#### **`apps/`** - Standalone Applications (3 projects)

1. **`apps/changelings/`** (v0.1.0) - **Specialized autonomous agents**
   - Experimental project for scheduling and running autonomous agents
   - Depends on: mng, imbue-common, concurrency-group, modal, loguru
   - Includes deployment modules for Modal (cron_runner, remote_runner, verification)

2. **`apps/claude_web_view/`** (v0.1.0) - **Web viewer for Claude Code transcripts**
   - FastAPI backend with Server-Sent Events (SSE) for live updates
   - React + TypeScript frontend (Radix UI)
   - Allows viewing Claude Code session transcripts in browser

3. **`apps/sculptor_web/`** (v0.1.0) - **Web UI for agent management**
   - FastAPI-based web interface
   - Uses python-fasthtml for server-side rendering
   - Manages AI agents via mng programmatically

---

### Package Dependency Graph

```
ROOT (imbue monorepo)
├── imbue-common (foundation library)
├── concurrency-group (foundation library)
│
├── mng (core framework)
│   ├── depends on: imbue-common, concurrency-group
│   ├── modal, docker, click, pydantic, urwid, pluggy
│   └── [plugins all depend on mng]
│
├── mng-pair (plugin)
│   └── depends on: mng
│
├── mng-schedule (plugin)
│   ├── depends on: mng, imbue-common, modal
│   └── handles remote scheduling via Modal
│
├── mng-opencode (plugin)
│   └── depends on: mng
│
├── mng-tutor (plugin)
│   └── depends on: mng
│
├── mng-kanpan (plugin)
│   ├── depends on: mng
│   └── integrates with gh CLI (GitHub)
│
├── flexmux (utility)
│   ├── depends on: flask, pydantic
│   └── independent from mng
│
└── APPS:
    ├── changelings (autonomous agents)
    │   └── depends on: mng, imbue-common, concurrency-group, modal
    │
    ├── claude_web_view (web transcript viewer)
    │   └── depends on: fastapi, watchfiles (no mng dependency)
    │
    └── sculptor_web (agent management UI)
        └── depends on: mng, python-fasthtml
```

**Key Observations:**
- **All plugins depend on mng**, making it the central hub
- **imbue-common and concurrency-group** are foundational; used throughout
- **Modal integration** appears in mng_schedule and changelings for remote execution
- **Minimal coupling**: each plugin is relatively independent
- **Plugin system uses pluggy** entry points (see `[project.entry-points.mng]` in plugin pyproject.toml files)

---

### Key Technologies and Frameworks

**Language & Runtime:**
- Python 3.11+ (required by all packages)
- uv for package management and task running

**Core Dependencies:**
- **Click** - CLI framework with option groups
- **Pydantic** - Data validation and settings management
- **Pluggy** - Plugin system (extensibility)
- **Urwid** - Terminal UI framework (for interactive TUIs)
- **Modal** - Serverless container platform for remote agents

**Infrastructure & DevOps:**
- **Docker** - Container support
- **SSH** - All host communication
- **Git** - Version control integration
- **tmux** - Session management for agents
- **pyinfra** - Infrastructure provisioning

**Web Stack:**
- **FastAPI** - API framework
- **FastHTML** - Server-side HTML framework
- **React + TypeScript** - Frontend (claude_web_view)
- **Radix UI** - Component library
- **Flask** - lightweight web server (flexmux)

**Security & Cryptography:**
- **cryptography** - For SSH key handling
- **python-dotenv** - Environment variable management

**Testing & Quality:**
- **pytest** with pytest-xdist (parallel execution)
- **pytest-cov** - Coverage reporting (80%+ required)
- **ruff** - Code formatting and linting
- **pre-commit** - Git hook integration
- **pyright** - Type checking (strict mode)
- **import-linter** - Architecture layer enforcement

**Development Tools:**
- **inline-snapshot** - Snapshot testing
- **tomlkit** - TOML parsing/manipulation
- **tenacity** - Retry logic
- **tabulate** - CLI table formatting
- **loguru** - Enhanced logging

---

### Core Architectural Concepts

Based on README and code structure, mng operates on these key principles:

1. **Agents** - AI processes running in tmux sessions with configuration (environment, secrets, work_dir)
2. **Hosts** - Compute resources where agents run (local, Docker, Modal, or any SSH-accessible machine)
3. **Providers** - Implementations for different host types (local, docker, modal providers)
4. **Plugins** - Extensible system for adding commands, agent types, and lifecycle hooks
5. **State minimization** - mng relies on conventions over configuration; stores minimal state
6. **Convention-based** - Uses standard tools (tmux, SSH, git, Docker) and location conventions
7. **Layered architecture** - Clear separation of concerns (main → CLI → API → agents → providers → hosts)

---

### Important Files for Future Reference

- `/home/user/mng/README.md` - Primary user documentation
- `/home/user/mng/CLAUDE.md` - Critical project guidelines
- `/home/user/mng/style_guide.md` - Code style requirements
- `/home/user/mng/libs/mng/docs/architecture.md` - Detailed mng architecture (46 lines)
- `/home/user/mng/libs/mng/docs/` - Comprehensive documentation directory with concepts, commands, security model
- `/home/user/mng/pyproject.toml` - Workspace root configuration with coverage targets and test markers

---

### Summary

This is a well-architected Python monorepo centered on the **mng** core framework for managing AI agents. The design follows clear principles: plugin-based extensibility, minimal state, convention-based operation, and strict architectural layering. Dependencies flow cleanly from applications → plugins → core framework → common libraries, with no circular dependencies. The project emphasizes code quality (80%+ test coverage, strict type checking, automated formatting) and comprehensive testing infrastructure.