---
name: create-task-from-json
argument-hint: [json_file] [context_file] [output_dir]
description: Create a prompt from a short task description in a JSON file
---

Your goal is to create a full prompt for a particular task that I want to do.

To understand which task we want to create the prompt for, first read this file $1

That file defines the name of the task, and the content of that task.

Next, read this file for context: $2

That file is where the task came from. From the file, you should be able to see other context (goals, reminders, current focus, etc) that are meant to help guide you as you are creating the task. It also contains other current tasks, which should help you AVOID including any of that content in your own task prompt (they will be handled separately).

Next, look at the git log for the past ~10 or so commits to better understand what was recently accomplished. These should serve as reasonable context for what is being worked on currently.

After that, gather context (as appropriate) from the project in this repo that is being worked on (read the docs, specs, interfaces, and any code that seems like it might be related)

Finally, think carefully about how best to convert this task into a complete, concise, clear prompt that can be sent along to an AI coding model.

Adhere to the following guidelines:

- You may need to gather more context about the project in order to resolve some ambiguities or uncertainty and figure out what was meant by the task. Feel free to do so.
- The purpose of creating the prompt for me is to save me time. If you're unsure of what was meant, it's ok to make some assumptions, or even to make multiple variants (separated by "---" within the file)
- Generally, your prompts should start with something like this:
    Go gather all the context for the mngr library (per instructions in CLAUDE.md).

    Once you've gathered that context, please do the below (and commit when you're finished).
- At the bottom of the prompt file, you may leave some open questions about the largest open questions or areas of uncertainty (if there are any that matter). Do NOT make these vague--make them very specific. Try to use the context and think and make reasonable assumptions before saying something is uncertain.
- Do NOT include specific implementation details! The point is to describe WHAT to do, not HOW to do it.
- Write one sentence per line. For sentences that are within the same paragraph, have them immediately follow one another with no blank lines in between. For a new paragraph, insert a single blank line.
- Your prompt should be as concise as possible while still being clear and complete. This means that, for simpler tasks (most tasks), it will be about 7 lines, while the most complex tasks might have 15-20 lines.
- NEVER use backtick ("`") characters in the prompt (it messes with my prompt processing system). Always use single quotes (') instead.

Once you've created the prompt text, iterate on it at least once or twice. Reflect on what could be improved, and then make those improvements.

When you're done iterating on it, put the prompt into $3 in a file named <task-name>_<date>.md (where you should get "date" by calling this precise command: "date +%Y-%m-%d-%T | tr : -")  Be sure to separate the two components of the filename with an underscore ("_"), not a dash ("-").

You are not allowed to create or modify any other files (besides the one mentioned above) while doing your task, nor are you allowed to run any commands (besides invocations of "git log")

Note that you are running as part of a larger scripted workflow. Your chat responses will not be seen, and you cannot invoke tools to request user input, etc. If you have questions or comments, simply leave them in the output file that you are supposed to create.
