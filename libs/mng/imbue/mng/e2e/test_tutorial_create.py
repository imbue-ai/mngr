"""Tests for tutorial create command blocks in tutorials/mega_tutorial.sh.

Each test's docstring contains the exact text of one or more script blocks
from the tutorial. Tests run the actual commands from those blocks and verify
the resulting behavior.
"""

import json

import pytest

from imbue.mng.utils.testing import get_short_random_string
from imbue.skitwright.expect import expect
from imbue.skitwright.session import Session

# ---------------------------------------------------------------------------
# BASIC CREATION -- local tests
# ---------------------------------------------------------------------------


@pytest.mark.release
@pytest.mark.tmux
def test_create_bare(e2e: Session, agent_name: str) -> None:
    """
    # running mng create is strictly better than running claude! It's less letters to type :-D
    # running this command launches claude (Claude Code) immediately *in a new worktree*
    mng create
    # the defaults are the following: agent=claude, provider=local, project=current dir
    """
    expect(e2e.run(f"mng create {agent_name} --no-connect --command 'sleep 99999' --no-ensure-clean")).to_succeed()

    list_result = e2e.run("mng list --format json")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    assert len(parsed["agents"]) == 1
    assert parsed["agents"][0]["name"] == agent_name

    # Verify the agent runs in a worktree (different directory from the main repo)
    exec_result = e2e.run(f"mng exec {agent_name} pwd")
    expect(exec_result).to_succeed()
    cwd_result = e2e.run("pwd")
    assert exec_result.stdout.strip() != cwd_result.stdout.strip()


@pytest.mark.release
@pytest.mark.tmux
def test_create_in_place(e2e: Session, agent_name: str) -> None:
    """
    # if you want the default behavior of claude (starting in-place), you can specify that:
    mng create --in-place
    # mng defaults to creating a new worktree for each agent because the whole point of mng is to let you run multiple agents in parallel.
    # without creating a new worktree for each, they will make conflicting changes with one another.
    """
    expect(
        e2e.run(f"mng create {agent_name} --in-place --no-connect --command 'sleep 99999' --no-ensure-clean")
    ).to_succeed()

    # Verify the agent is running in the same directory (in-place, not a worktree)
    exec_result = e2e.run(f"mng exec {agent_name} pwd")
    expect(exec_result).to_succeed()
    cwd_result = e2e.run("pwd")
    expect(exec_result.stdout.strip()).to_equal(cwd_result.stdout.strip())


@pytest.mark.release
@pytest.mark.tmux
def test_create_short_form(e2e: Session) -> None:
    """
    # you can use a short form for most commands (like create) as well--the above command is the same as these:
    mng create my-task claude
    mng c my-task
    """
    name = f"e2e-short-{get_short_random_string()}"
    # Use the short form 'c' to create an agent
    result = e2e.run(f"mng c {name} --no-connect --command 'sleep 99999' --no-ensure-clean")
    expect(result).to_succeed()

    list_result = e2e.run("mng list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain(name)


@pytest.mark.release
@pytest.mark.tmux
def test_create_different_agent_type(e2e: Session, agent_name: str) -> None:
    """
    # you can also specify a different agent (ex: codex)
    mng create my-task codex
    """
    expect(
        e2e.run(f"mng create {agent_name} codex --no-connect --command 'sleep 99999' --no-ensure-clean")
    ).to_succeed()

    # Verify the agent was created with the codex agent type
    list_result = e2e.run("mng list --format json")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    assert len(parsed["agents"]) == 1
    assert parsed["agents"][0]["agent_type"] == "codex"


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_agent_args(e2e: Session, agent_name: str) -> None:
    """
    # you can specify the arguments to the *agent* (ie, send args to claude rather than mng)
    # by using `--` to separate the agent arguments from the mng arguments:
    mng create my-task -- --model opus
    # that command launches claude with the "opus" model instead of the default
    """
    # The -- separator passes remaining args to the agent process
    expect(
        e2e.run(f"mng create {agent_name} --no-connect --command 'sleep 99999' --no-ensure-clean -- extra-arg")
    ).to_succeed()

    list_result = e2e.run("mng list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain(agent_name)


@pytest.mark.release
@pytest.mark.tmux
def test_create_custom_command(e2e: Session) -> None:
    """
    # you can run *any* literal command instead of a named agent type:
    mng create my-task --command python -- my_script.py
    # remember that the arguments to the "agent" (or command) come after the `--` separator
    """
    name = f"e2e-cmd-{get_short_random_string()}"
    result = e2e.run(
        f"mng create {name} --command 'echo hello && sleep 99999' --no-connect --no-ensure-clean",
    )
    expect(result).to_succeed()

    list_result = e2e.run("mng list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain(name)


@pytest.mark.release
@pytest.mark.tmux
def test_create_command_with_idle(e2e: Session) -> None:
    """
    # this enables some pretty interesting use cases, like running servers or other programs (besides AI agents)
    # this make debugging easy--you can snapshot when a task is complete, then later connect to that exact machine state:
    mng create my-task --command python --idle-mode run --idle-timeout 60 -- my_long_running_script.py extra-args
    # see "RUNNING NON-AGENT PROCESSES" below for more details
    """
    name = f"e2e-idle-{get_short_random_string()}"
    result = e2e.run(
        f"mng create {name} --command 'sleep 99999' --idle-mode run --idle-timeout 60 --no-connect --no-ensure-clean",
    )
    expect(result).to_succeed()

    list_result = e2e.run("mng list --format json")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    assert len(parsed["agents"]) == 1
    assert parsed["agents"][0]["name"] == name


@pytest.mark.release
@pytest.mark.tmux
def test_create_extra_windows(e2e: Session, agent_name: str) -> None:
    """
    # alternatively, you can simply add extra tmux windows that run alongside your agent:
    mng create my-task -w server="npm run dev" -w logs="tail -f app.log"
    # that command automatically starts two tmux windows named "server" and "logs" that run those commands (in addition to the main window that runs the agent)
    """
    expect(
        e2e.run(
            f"mng create {agent_name} --command 'sleep 99999' -w extra='sleep 99999' --no-connect --no-ensure-clean"
        )
    ).to_succeed()

    list_result = e2e.run("mng list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain(agent_name)


# ---------------------------------------------------------------------------
# SPECIFYING DATA FOR THE AGENT
# ---------------------------------------------------------------------------


@pytest.mark.release
@pytest.mark.tmux
def test_create_source_path(e2e: Session) -> None:
    """
    # by default, the agent uses the data from its current git repo (if any) or folder, but you can specify a different source:
    mng create my-task --source-path /path/to/some/other/project
    """
    name = f"e2e-source-{get_short_random_string()}"
    source_dir = f"/tmp/mng-e2e-source-{name}"
    e2e.run(f"mkdir -p {source_dir} && git init {source_dir}")
    result = e2e.run(
        f"mng create {name} --source-path {source_dir} --no-connect --command 'sleep 99999' --no-ensure-clean",
    )
    expect(result).to_succeed()

    # Verify the agent's working directory is derived from the source path
    exec_result = e2e.run(f"mng exec {name} pwd")
    expect(exec_result).to_succeed()
    cwd_result = e2e.run("pwd")
    # The agent should NOT be in the original repo directory
    assert exec_result.stdout.strip() != cwd_result.stdout.strip()

    e2e.run(f"rm -rf {source_dir}")


@pytest.mark.release
@pytest.mark.tmux
def test_create_project(e2e: Session, agent_name: str) -> None:
    """
    # similarly, by default the agent is tagged with a "project" label that matches the name of the current git repo (or folder), but you can specify a different project:
    mng create my-task --project my-project
    """
    expect(
        e2e.run(f"mng create {agent_name} --project my-project --no-connect --command 'sleep 99999' --no-ensure-clean")
    ).to_succeed()

    list_result = e2e.run("mng list --format json")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching = [a for a in agents if a["name"] == agent_name]
    assert len(matching) == 1
    assert matching[0]["project"] == "my-project"


@pytest.mark.release
@pytest.mark.tmux
def test_create_no_git(e2e: Session) -> None:
    """
    # mng doesn't require git at all--if there's no git repo, it will just use the files from the folder as the source data
    mkdir -p /tmp/my_random_folder
    echo "print('hello world')" > /tmp/my_random_folder/script.py
    mng create my-task --source-path /tmp/my_random_folder --command python -- script.py
    """
    name = f"e2e-nogit-{get_short_random_string()}"
    nogit_dir = f"/tmp/mng-e2e-nogit-{name}"
    e2e.run(f"mkdir -p {nogit_dir}")
    e2e.run(f"echo 'import time; time.sleep(99999)' > {nogit_dir}/script.py")
    result = e2e.run(
        f"mng create {name} --source-path {nogit_dir} --command python -- script.py --no-connect --no-ensure-clean",
    )
    expect(result).to_succeed()

    list_result = e2e.run("mng list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain(name)

    e2e.run(f"rm -rf {nogit_dir}")


# ---------------------------------------------------------------------------
# GIT BRANCHES
# ---------------------------------------------------------------------------


@pytest.mark.release
@pytest.mark.tmux
def test_create_auto_branch(e2e: Session, agent_name: str) -> None:
    """
    # however, if you do use git, mng makes that convenient
    # by default, it creates a new git branch for each agent (so that their changes don't conflict with each other):
    mng create my-task
    git branch | grep mng/my-task
    """
    expect(e2e.run(f"mng create {agent_name} --no-connect --command 'sleep 99999' --no-ensure-clean")).to_succeed()

    branch_result = e2e.run("git branch --all")
    expect(branch_result).to_succeed()
    expect(branch_result.stdout).to_contain(f"mng/{agent_name}")


@pytest.mark.release
@pytest.mark.tmux
def test_create_custom_branch_pattern(e2e: Session, agent_name: str) -> None:
    """
    # --branch controls branch creation. the default is :mng/* which creates a new branch named mng/{agent_name}
    # you can change the pattern (the * is replaced by the agent name):
    mng create my-task --branch ":feature/*"
    git branch | grep feature/my-task
    """
    expect(
        e2e.run(
            f"mng create {agent_name} --branch \":feature/*\" --no-connect --command 'sleep 99999' --no-ensure-clean"
        )
    ).to_succeed()

    branch_result = e2e.run("git branch --all")
    expect(branch_result).to_succeed()
    expect(branch_result.stdout).to_contain(f"feature/{agent_name}")


@pytest.mark.release
@pytest.mark.tmux
def test_create_base_branch(e2e: Session, agent_name: str) -> None:
    """
    # you can also specify a different base branch (instead of the current branch):
    mng create my-task --branch "main:mng/*"
    """
    e2e.run("git checkout -b main || true")
    e2e.run("git checkout -")

    expect(
        e2e.run(
            f"mng create {agent_name} --branch \"main:mng/*\" --no-connect --command 'sleep 99999' --no-ensure-clean"
        )
    ).to_succeed()

    branch_result = e2e.run("git branch --all")
    expect(branch_result).to_succeed()
    expect(branch_result.stdout).to_contain(f"mng/{agent_name}")


@pytest.mark.release
@pytest.mark.tmux
def test_create_explicit_branch(e2e: Session) -> None:
    """
    # or set the new branch name explicitly:
    mng create my-task --branch ":feature/my-task"
    """
    name = f"e2e-explicit-{get_short_random_string()}"
    branch_name = f"feature/{name}"
    result = e2e.run(
        f"mng create {name} --branch \":{branch_name}\" --no-connect --command 'sleep 99999' --no-ensure-clean",
    )
    expect(result).to_succeed()

    branch_result = e2e.run("git branch --all")
    expect(branch_result).to_succeed()
    expect(branch_result.stdout).to_contain(branch_name)


@pytest.mark.release
@pytest.mark.tmux
def test_create_copy(e2e: Session, agent_name: str) -> None:
    """
    # you can create a copy instead of a worktree:
    mng create my-task --copy
    # that is used by default if you're not in a git repo
    """
    expect(
        e2e.run(f"mng create {agent_name} --copy --no-connect --command 'sleep 99999' --no-ensure-clean")
    ).to_succeed()

    # Verify the agent's working dir is different from the main repo (it's a copy)
    exec_result = e2e.run(f"mng exec {agent_name} pwd")
    expect(exec_result).to_succeed()
    cwd_result = e2e.run("pwd")
    assert exec_result.stdout.strip() != cwd_result.stdout.strip()


@pytest.mark.release
@pytest.mark.tmux
def test_create_copy_with_branch(e2e: Session, agent_name: str) -> None:
    """
    # you can disable new branch creation entirely by omitting the :NEW part (requires --in-place or --copy due to how worktrees work, and --in-place implies no new branch):
    mng create my-task --copy --branch main
    """
    # Record branches before creating
    before_branches = e2e.run("git branch")
    expect(before_branches).to_succeed()

    expect(
        e2e.run(f"mng create {agent_name} --copy --branch main --no-connect --command 'sleep 99999' --no-ensure-clean")
    ).to_succeed()

    # Verify no new branch was created (--copy --branch main means use existing main, no new branch)
    after_branches = e2e.run("git branch")
    expect(after_branches).to_succeed()
    assert before_branches.stdout.strip() == after_branches.stdout.strip()

    # Verify the agent is on the main branch
    head_result = e2e.run(f"mng exec {agent_name} git branch --show-current")
    expect(head_result).to_succeed()
    expect(head_result.stdout.strip()).to_equal("main")


@pytest.mark.release
@pytest.mark.tmux
def test_create_clone(e2e: Session, agent_name: str) -> None:
    """
    # you can create a "clone" instead of worktree or copy, which is a lightweight copy that shares git objects with the original repo but has its own separate working directory:
    mng create my-task --clone
    """
    expect(
        e2e.run(f"mng create {agent_name} --clone --no-connect --command 'sleep 99999' --no-ensure-clean")
    ).to_succeed()

    exec_result = e2e.run(f"mng exec {agent_name} pwd")
    expect(exec_result).to_succeed()
    cwd_result = e2e.run("pwd")
    assert exec_result.stdout.strip() != cwd_result.stdout.strip()


@pytest.mark.release
@pytest.mark.tmux
def test_create_shallow_clone(e2e: Session, agent_name: str) -> None:
    """
    # you can make a shallow clone for faster setup:
    mng create my-task --depth 1
    # (--shallow-since clones since a specific date instead)
    """
    expect(
        e2e.run(f"mng create {agent_name} --depth 1 --no-connect --command 'sleep 99999' --no-ensure-clean")
    ).to_succeed()

    # Verify the clone is shallow (only 1 commit)
    depth_result = e2e.run(f"mng exec {agent_name} git rev-list --count HEAD")
    expect(depth_result).to_succeed()
    assert depth_result.stdout.strip() == "1"


@pytest.mark.release
@pytest.mark.tmux
def test_create_from_agent(e2e: Session) -> None:
    """
    # you can clone from an existing agent's work directory:
    mng create my-task --from other-agent
    # (--source, --source-agent, and --source-host are alternative forms for more specific control)
    """
    src = f"e2e-from-src-{get_short_random_string()}"
    expect(e2e.run(f"mng create {src} --no-connect --command 'sleep 99999' --no-ensure-clean")).to_succeed()

    tgt = f"e2e-from-tgt-{get_short_random_string()}"
    expect(
        e2e.run(f"mng create {tgt} --from {src} --no-connect --command 'sleep 99999' --no-ensure-clean")
    ).to_succeed()

    list_result = e2e.run("mng list --format json")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    names = [a["name"] for a in parsed["agents"]]
    assert src in names
    assert tgt in names

    # Verify the target has the same git HEAD as the source
    src_head = e2e.run(f"mng exec {src} git rev-parse HEAD")
    expect(src_head).to_succeed()
    tgt_head = e2e.run(f"mng exec {tgt} git rev-parse HEAD")
    expect(tgt_head).to_succeed()
    expect(tgt_head.stdout.strip()).to_equal(src_head.stdout.strip())


# ---------------------------------------------------------------------------
# CONTROLLING THE AGENT ENVIRONMENT
# ---------------------------------------------------------------------------


@pytest.mark.release
@pytest.mark.tmux
def test_create_env(e2e: Session, agent_name: str) -> None:
    """
    # you can set environment variables for the agent:
    mng create my-task --env DEBUG=true
    # (--env-file loads from a file, --pass-env forwards a variable from your current shell)
    """
    expect(
        e2e.run(f"mng create {agent_name} --env DEBUG=true --no-connect --command 'sleep 99999' --no-ensure-clean")
    ).to_succeed()

    exec_result = e2e.run(f"mng exec {agent_name} printenv DEBUG")
    expect(exec_result).to_succeed()
    expect(exec_result.stdout.strip()).to_equal("true")


@pytest.mark.release
@pytest.mark.tmux
def test_create_pass_env(e2e: Session) -> None:
    """
    # it is *strongly encouraged* to use either use --env-file or --pass-env, especially for any sensitive environment variables (like API keys) rather than --env, because that way they won't end up in your shell history or in your config files by accident. For example:
    export API_KEY=abc123
    mng create my-task --pass-env API_KEY
    # that command passes the API_KEY environment variable from your current shell into the agent's environment, without you having to specify the value on the command line.
    """
    name = f"e2e-passenv-{get_short_random_string()}"
    result = e2e.run(
        f"API_KEY=abc123 mng create {name} --pass-env API_KEY --no-connect --command 'sleep 99999' --no-ensure-clean",
    )
    expect(result).to_succeed()

    exec_result = e2e.run(f"mng exec {name} printenv API_KEY")
    expect(exec_result).to_succeed()
    expect(exec_result.stdout.strip()).to_equal("abc123")


# ---------------------------------------------------------------------------
# TEMPLATES, ALIASES, AND SHORTCUTS
# ---------------------------------------------------------------------------


@pytest.mark.release
@pytest.mark.tmux
def test_create_template(e2e: Session) -> None:
    """
    # you can use templates to quickly apply a set of preconfigured options:
    echo '[create_templates.my_modal_template]' >> .mng/settings.local.toml
    echo 'provider = "modal"' >> .mng/settings.local.toml
    echo 'build_args = "cpu=4"' >> .mng/settings.local.toml
    mng create my-task --template my_modal_template
    # templates are defined in your config (see the CONFIGURATION section for more) and can be stacked: --template modal --template codex
    # templates take exactly the same parameters as the create command
    # -t is short for --template. Many commands have a short form (see the "--help")
    """
    # Create a local-provider template (modal is disabled in test env)
    e2e.run("echo '' >> .mng/settings.local.toml")
    e2e.run("echo '[create_templates.my_local_template]' >> .mng/settings.local.toml")
    e2e.run("echo 'in_place = true' >> .mng/settings.local.toml")

    name = f"e2e-tmpl-{get_short_random_string()}"
    result = e2e.run(
        f"mng create {name} --template my_local_template --no-connect --command 'sleep 99999' --no-ensure-clean",
    )
    expect(result).to_succeed()

    # Verify the template's in_place=true was applied (agent pwd matches main repo)
    exec_result = e2e.run(f"mng exec {name} pwd")
    expect(exec_result).to_succeed()
    cwd_result = e2e.run("pwd")
    expect(exec_result.stdout.strip()).to_equal(cwd_result.stdout.strip())


@pytest.mark.release
@pytest.mark.tmux
def test_create_plugins(e2e: Session) -> None:
    """
    # you can enable or disable specific plugins:
    mng create my-task --plugin my-plugin --disable-plugin other-plugin
    """
    name = f"e2e-plugin-{get_short_random_string()}"
    result = e2e.run(
        f"mng create {name} --plugin nonexistent-plugin --no-connect --command 'sleep 99999' --no-ensure-clean",
    )
    if result.exit_code == 0:
        # Plugin was silently accepted; verify the agent was created
        list_result = e2e.run("mng list")
        expect(list_result).to_succeed()
        expect(list_result.stdout).to_contain(name)
    else:
        # Plugin not found; verify the error is plugin-related
        expect(result.stderr + result.stdout).to_contain("plugin")


@pytest.mark.release
@pytest.mark.tmux
def test_create_aliases(e2e: Session, agent_name: str) -> None:
    """
    # you should probably use aliases for making little shortcuts for yourself, because many of the commands can get a bit long:
    echo "alias mc='mng create --in-place'" >> ~/.bashrc && source ~/.bashrc
    # or use a more sophisticated tool, like Espanso
    """
    # Test the command that the alias would expand to: mng create --in-place
    expect(
        e2e.run(f"mng create {agent_name} --in-place --no-connect --command 'sleep 99999' --no-ensure-clean")
    ).to_succeed()

    exec_result = e2e.run(f"mng exec {agent_name} pwd")
    expect(exec_result).to_succeed()
    cwd_result = e2e.run("pwd")
    expect(exec_result.stdout.strip()).to_equal(cwd_result.stdout.strip())


# ---------------------------------------------------------------------------
# TIPS AND TRICKS
# ---------------------------------------------------------------------------


@pytest.mark.release
@pytest.mark.tmux
def test_create_no_ensure_clean(e2e: Session) -> None:
    """
    # by default, mng aborts the create command if the working tree has uncommitted changes. You can avoid this by doing:
    mng create my-task --no-ensure-clean
    # this is particularly useful for starting agents when, eg, you are in the middle of a merge conflict and you just want the agent to finish it off, for example
    # it should probably be avoided in general, because it makes it more difficult to merge work later.
    """
    # Make the working tree dirty
    e2e.run("echo 'dirty' >> README.md")

    name = f"e2e-noclean-{get_short_random_string()}"
    result = e2e.run(
        f"mng create {name} --no-ensure-clean --no-connect --command 'sleep 99999'",
    )
    expect(result).to_succeed()

    list_result = e2e.run("mng list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain(name)


@pytest.mark.release
@pytest.mark.tmux
def test_create_connect_command(e2e: Session, agent_name: str) -> None:
    """
    # you can use a custom connect command instead of the default (eg, useful for, say, connecting in a new iterm window instead of the current one)
    mng create my-task --connect-command "my_script.sh"
    """
    expect(
        e2e.run(
            f"mng create {agent_name} --connect-command 'echo connected'"
            " --no-connect --command 'sleep 99999' --no-ensure-clean"
        )
    ).to_succeed()

    # Verify the connect command is stored
    list_result = e2e.run("mng list --format json")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    matching = [a for a in parsed["agents"] if a["name"] == agent_name]
    assert len(matching) == 1
    assert matching[0].get("connect_command") == "echo connected"


# ---------------------------------------------------------------------------
# CREATING AND USING AGENTS PROGRAMMATICALLY
# ---------------------------------------------------------------------------


@pytest.mark.release
def test_config_set_headless(e2e: Session) -> None:
    """
    # or you can set that option in your config so that it always applies:
    mng config set headless true
    """
    result = e2e.run("mng config set headless true")
    expect(result).to_succeed()

    get_result = e2e.run("mng config get headless")
    expect(get_result).to_succeed()
    expect(get_result.stdout).to_contain("true")


@pytest.mark.release
@pytest.mark.tmux
def test_headless_env_var(e2e: Session) -> None:
    """
    # or you can set it as an environment variable:
    export MNG_HEADLESS=true
    """
    name = f"e2e-headenv-{get_short_random_string()}"
    result = e2e.run(
        f"MNG_HEADLESS=true mng create {name} --no-connect --command 'sleep 99999' --no-ensure-clean",
    )
    expect(result).to_succeed()

    list_result = e2e.run("mng list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain(name)


@pytest.mark.release
def test_config_set_provider(e2e: Session) -> None:
    """
    # *all* mng options work like that. For example, if you want to always run agents in Modal by default, you can set that in your config:
    mng config set commands.create.provider modal
    # for more on configuration, see the CONFIGURATION section below
    """
    result = e2e.run("mng config set commands.create.provider modal")
    expect(result).to_succeed()

    get_result = e2e.run("mng config get commands.create.provider")
    expect(get_result).to_succeed()
    expect(get_result.stdout).to_contain("modal")


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_message(e2e: Session, agent_name: str) -> None:
    """
    # you can send a message when starting the agent (great for scripting):
    mng create my-task --no-connect --message "Do the thing"
    """
    expect(
        e2e.run(
            f'mng create {agent_name} --no-connect --message "Do the thing"'
            " --command 'sleep 99999' --no-ensure-clean"
        )
    ).to_succeed()

    list_result = e2e.run("mng list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain(agent_name)


# ---------------------------------------------------------------------------
# MODAL/DOCKER TESTS -- provider disabled, verifying command parsing
#
# The test environment disables Modal and Docker providers. These tests run
# the actual commands from the tutorial blocks and verify they fail with an
# appropriate provider-related error (rather than a flag-parsing error).
# ---------------------------------------------------------------------------


_DISABLED_PROVIDER_CREATE_ARGS = [
    pytest.param("--provider modal", id="modal-basic"),
    pytest.param(
        '--provider modal --no-connect --message "Speed up one of my tests and make a PR on github"',
        id="modal-message",
    ),
    pytest.param("--provider modal --edit-message", id="modal-edit-message"),
    pytest.param(
        "--provider modal --rsync --rsync-args '--exclude=node_modules'",
        id="modal-rsync",
    ),
    pytest.param(
        '--provider modal -- --dangerously-skip-permissions --append-system-prompt "Don\'t ask me any questions!"',
        id="modal-permissions",
    ),
    pytest.param("--provider modal --idle-timeout 60", id="modal-idle-timeout"),
    pytest.param('--provider modal --idle-mode "ssh"', id="modal-idle-mode"),
    pytest.param(
        "--provider modal -b cpu=4 -b memory=16 -b image=python:3.12",
        id="modal-build-args",
    ),
    pytest.param(
        "--provider modal -b file=./Dockerfile.agent -b context-dir=./agent-context",
        id="modal-dockerfile",
    ),
    pytest.param("--provider modal -b volume=my-data:/data", id="modal-volume"),
    pytest.param("--provider modal --snapshot snap-123abc", id="modal-snapshot"),
    pytest.param("--provider modal --target-path /workspace", id="modal-target-path"),
    pytest.param(
        '--provider modal --upload-file ~/.ssh/config:/root/.ssh/config --user-command "pip install foo"',
        id="modal-provisioning",
    ),
    pytest.param("--provider modal --no-start-on-boot", id="modal-no-start"),
    pytest.param("--provider modal --pass-host-env MY_VAR", id="modal-host-env"),
    pytest.param("--reuse --provider modal", id="modal-reuse"),
    pytest.param("--provider modal --retry 5 --retry-delay 10s", id="modal-retry"),
    pytest.param('--provider docker -s "--gpus all"', id="docker-start-args"),
]


@pytest.mark.release
@pytest.mark.parametrize("create_args", _DISABLED_PROVIDER_CREATE_ARGS)
def test_create_with_disabled_provider(e2e: Session, create_args: str) -> None:
    """
    These tests exercise tutorial blocks that use --provider modal or --provider docker.
    The providers are disabled in the test environment, so the commands fail. This verifies
    the flags are parsed correctly and the error is provider-related.

    # you can also launch claude remotely in Modal:
    mng create my-task --provider modal
    # see more details below in "CREATING AGENTS REMOTELY" for relevant options

    # you can send an initial message (so you don't have to wait around, eg, while a Modal container starts)
    mng create my-task --provider modal --no-connect --message "Speed up one of my tests and make a PR on github"
    # here we disable the default --connect behavior (because presumably you just wanted to launch that in the background and continue on your way)
    # and then we also pass in an explicit message for the agent to start working on immediately
    # the message can also be specified as the contents of a file (by using --message-file instead of --message)

    # you can also edit the message *while the agent is starting up*, which is very handy for making it "feel" instant:
    mng create my-task --provider modal --edit-message

    # you can use rsync to transfer extra data as well, beyond just the git data:
    mng create my-task --provider modal --rsync --rsync-args "--exclude=node_modules"

    # one of the coolest features of mng is the ability to create agents on remote hosts just as easily as you can create them locally:
    mng create my-task --provider modal -- --dangerously-skip-permissions --append-system-prompt "Don't ask me any questions!"
    # that command passes the "--dangerously-skip-permissions" flag to claude because it's safe to do so:
    # agents running remotely are running in a sandboxed environment where they can't really mess anything up on their local machine (or if they do, it doesn't matter)
    # because it's running remotely, you might also want something like that system prompt (to tell it not to get blocked on you)

    # running agents remotely is really cool because you can create an unlimited number of them, but it comes with some downsides
    # one of the main downsides is cost--remote hosts aren't free, and if you forget about them, they can rack up a big bill.
    # mng makes it really easy to deal with this by automatically shutting down hosts when their agents are idle:
    mng create my-task --provider modal --idle-timeout 60
    # that command shuts down the Modal host (and agent) after 1 minute of inactivity.

    # You can customize what "inactivity" means by using the --idle-mode flag:
    mng create my-task --provider modal --idle-mode "ssh"
    # that command will only consider agents as "idle" when you are not connected to them
    # see the idle_detection.md file for more details on idle detection and timeouts

    # generally though, you'll want to construct a new Modal host for each agent.
    # build arguments let you customize that new remote host (eg, GPU type, memory, base Docker image for Modal):
    mng create my-task --provider modal -b cpu=4 -b memory=16 -b image=python:3.12
    # see "mng create --help" for all provider-specific build args
    # some other useful Modal build args: --region, --timeout, --offline (blocks network), --secret, --cidr-allowlist, --context-dir

    # the most important build args for Modal are probably "--file" and "--context-dir",
    # which let you specify a custom Dockerfile and build context directory (respectively) for building the host environment.
    # This is how you can get custom dependencies, files, and setup steps on your Modal hosts. For example:
    mng create my-task --provider modal -b file=./Dockerfile.agent -b context-dir=./agent-context
    # that command builds a Modal host using the Dockerfile at ./Dockerfile.agent and the build context at ./agent-context
    # (which is where the Dockerfile can COPY files from, and also where build args are evaluated from)

    # you can mount persistent Modal volumes in order to share data between hosts, or have it be available even when they are offline (or after they are destroyed):
    mng create my-task --provider modal -b volume=my-data:/data

    # you can use an existing snapshot instead of building a new host from scratch:
    mng create my-task --provider modal --snapshot snap-123abc

    # some providers (like docker), take "start" args as well as build args:
    mng create my-task --provider docker -s "--gpus all"
    # these args are passed to "docker run", whereas the build args are passed to "docker build".

    # you can specify the target path where the agent's work directory will be mounted:
    mng create my-task --provider modal --target-path /workspace

    # you can upload files and run custom commands during host provisioning:
    mng create my-task --provider modal --upload-file ~/.ssh/config:/root/.ssh/config --user-command "pip install foo"
    # (--sudo-command runs as root; --append-to-file and --prepend-to-file are also available)

    # by default, agents are started when a host is booted. This can be disabled:
    mng create my-task --provider modal --no-start-on-boot
    # but it only makes sense to do this if you are running multiple agents on the same host
    # that's because hosts are automatically stopped when they have no more running agents, so you have to have at least one.

    # you can also set host-level environment variables (separate from agent env vars):
    mng create my-task --provider modal --pass-host-env MY_VAR
    # --host-env-file and --pass-host-env work the same as their agent counterparts, and again, you should generally prefer those forms (but if you really need to you can use --host-env to specify host env vars directly)

    # another handy trick is to make the create command "idempotent" so that you don't need to worry about remembering whether you created an agent yet or not:
    mng create sisyphus --reuse --provider modal
    # if that agent already exists, it will be reused (and started) instead of creating a new one. If it doesn't exist, it will be created.

    # you can control connection retries and timeouts:
    mng create my-task --provider modal --retry 5 --retry-delay 10s
    # (--reconnect / --no-reconnect controls auto-reconnect on disconnect)
    """
    name = f"e2e-provider-{get_short_random_string()}"
    result = e2e.run(f"mng create {name} --no-connect --no-ensure-clean {create_args}")
    expect(result).to_fail()
    # Verify the failure is provider-related, not a flag-parsing error
    output = result.stderr + result.stdout
    expect(output).to_match(r"(?i)(provider|modal|docker|disabled|not.*(enabled|available))")


# ---------------------------------------------------------------------------
# ADDRESS SYNTAX AND NAMED HOST TESTS
# ---------------------------------------------------------------------------


@pytest.mark.release
def test_create_address_syntax(e2e: Session) -> None:
    """
    # you can specify which existing host to run on using the address syntax (eg, if you have multiple Modal hosts or SSH servers):
    mng create my-task@my-dev-box
    """
    name = f"e2e-addr-{get_short_random_string()}"
    result = e2e.run(f"mng create {name}@my-dev-box --no-connect --no-ensure-clean")
    # Address syntax requires the host to exist; expect failure
    expect(result).to_fail()


@pytest.mark.release
def test_create_named_host(e2e: Session) -> None:
    """
    # you can name the host using the address syntax:
    mng create my-task@my-modal-box.modal --new-host
    # (--host-name-style and --name-style control auto-generated name styles for hosts and agents respectively)
    """
    name = f"e2e-named-{get_short_random_string()}"
    result = e2e.run(f"mng create {name}@my-modal-box.modal --new-host --no-connect --no-ensure-clean")
    # Modal is disabled; expect failure
    expect(result).to_fail()
