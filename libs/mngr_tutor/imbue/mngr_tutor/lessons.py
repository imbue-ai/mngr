from textwrap import dedent

from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr_tutor.data_types import AgentExistsCheck
from imbue.mngr_tutor.data_types import AgentInStateCheck
from imbue.mngr_tutor.data_types import AgentNotExistsCheck
from imbue.mngr_tutor.data_types import FileExistsInAgentWorkDirCheck
from imbue.mngr_tutor.data_types import Lesson
from imbue.mngr_tutor.data_types import LessonStep
from imbue.mngr_tutor.data_types import TmuxSessionHasClientsCheck

LESSON_GETTING_STARTED = Lesson(
    title="Basic Local Agent",
    description="Learn to create, use, and manage your first agent.",
    steps=(
        LessonStep(
            heading="Create your first agent",
            details=dedent("""\
                Go to any Git repo that you have, and run:
                    mngr create agent-smith

                If you don't have a Git repo lying around, just make a new one:
                    mkdir learn-mngr; cd learn-mngr; git init; git commit --allow-empty -m 'Initial commit'\
                """),
            check=AgentExistsCheck(agent_name=AgentName("agent-smith")),
        ),
        LessonStep(
            heading="Make some changes using your agent",
            details=dedent("""\
                You should now be in a tmux session with Claude Code running in it.
                Let's ask it to create a file:
                    Create a blue-pill.txt.

                Alternatively, you can also message the agent using the mngr CLI:
                    mngr message agent-smith 'Create a blue-pill.txt'

                (You can run this command in another tmux pane or tab,
                or if you aren't familiar with tmux yet, just run it in another terminal.)\
                """),
            check=FileExistsInAgentWorkDirCheck(
                agent_name=AgentName("agent-smith"),
                file_path="blue-pill.txt",
            ),
        ),
        LessonStep(
            heading="Stop the agent",
            details=dedent("""
                Let's say we don't need the agent for now. Let's stop it:
                    mngr stop agent-smith

                The tmux window should also be gone.
                Alternatively, we also provide a shortcut for this command from within tmux:
                Just press Ctrl-T.

                Stopped agents don't use any resource on your computer,
                but are remembered by mngr and can be restarted any time.
                """),
            check=AgentInStateCheck(
                agent_name=AgentName("agent-smith"),
                expected_states=(AgentLifecycleState.STOPPED,),
            ),
        ),
        LessonStep(
            heading="Restart the agent",
            details=dedent("""\
                Let's restart the agent. First, start it:
                    mngr start agent-smith

                This starts the agent in the background. Now, connect to it:
                    mngr connect agent-smith

                This should drop you back into a tmux window with a resumed Claude Code,
                just like before it was stopped!

                Tip: you can actually skip the first `start` command,
                because the `connect` command will restart the agent first if it's stopped!\
                """),
            check=TmuxSessionHasClientsCheck(
                agent_name=AgentName("agent-smith"),
            ),
        ),
        LessonStep(
            heading="Destroy the agent",
            details=dedent("""\
                Now we're done with the agent. We can get rid of it:
                    mngr destroy agent-smith

                Alternatively, you can just press Ctrl-Q from tmux.\
                """),
            check=AgentNotExistsCheck(agent_name=AgentName("agent-smith")),
        ),
    ),
)


LESSON_REMOTE_AGENTS = Lesson(
    title="Remote Agents on Modal (WIP)",
    description="Learn to launch and manage agents running on Modal's cloud infrastructure.",
    steps=(
        LessonStep(
            heading="Create a remote agent",
            details=dedent("""\
                cd into any git repo and run `mngr create morpheus --provider modal`.
                The --provider modal flag tells mngr to launch the agent on Modal instead of
                locally. This will take a bit longer as it builds a remote sandbox."""),
            check=AgentExistsCheck(agent_name=AgentName("morpheus")),
        ),
        LessonStep(
            heading="Make some changes using your remote agent",
            details=dedent("""\
                Connect to the agent with `mngr connect morpheus`, then ask it to
                create a file called `red-pill.txt` and make a commit."""),
            check=FileExistsInAgentWorkDirCheck(
                agent_name=AgentName("morpheus"),
                file_path="red-pill.txt",
            ),
        ),
        LessonStep(
            heading="Stop the remote agent",
            details="Run `mngr stop morpheus`, or press Ctrl-T from within the tmux session.",
            check=AgentInStateCheck(
                agent_name=AgentName("morpheus"),
                expected_states=(AgentLifecycleState.STOPPED,),
            ),
        ),
        LessonStep(
            heading="Restart the remote agent",
            details=dedent("""\
                Run `mngr start morpheus` and then `mngr connect morpheus` to restart
                and reconnect to the agent. You can see all its work is still there."""),
            check=AgentInStateCheck(
                agent_name=AgentName("morpheus"),
                expected_states=(AgentLifecycleState.RUNNING, AgentLifecycleState.WAITING),
            ),
        ),
        LessonStep(
            heading="Destroy the remote agent",
            details="Run `mngr destroy morpheus` or press Ctrl-Q from within the tmux session.",
            check=AgentNotExistsCheck(agent_name=AgentName("morpheus")),
        ),
    ),
)


ALL_LESSONS: tuple[Lesson, ...] = (
    LESSON_GETTING_STARTED,
    LESSON_REMOTE_AGENTS,
)
