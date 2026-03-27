# Git Status Plugin [future]

This plugin provides information about the git status of the agent's work_dir repository by adding fields under the `plugin.git_status` namespace on controlled shutdown:

  - `plugin.$PLUGIN_NAME.*`: Each plugin can add its own fields under its namespace (e.g., `plugin.chat_history.messages`)
  - `plugin.git_status`: A default plugin that shows git state for the agent's work_dir
  - `plugin.git_status.branch`: Current git branch
  - `plugin.git_status.commit`: Current git commit hash
  - `plugin.git_status.repo_url`: Remote repository URL (ex: on GitHub). If multiple, shows `origin` remote
  - `plugin.git_status.url`: Agent repository URL (for direct access to the agent's repo)
  - `plugin.git_status.has_uncommitted_changes`: Boolean indicating uncommitted changes
  - `plugin.git_status.has_unpushed_commits`: Boolean indicating unpushed commits
  - `plugin.git_status.has_untracked_files`: Boolean indicating untracked files
  - `plugin.git_status.modified_file_count`: Number of modified files
  - `plugin.git_status.untracked_file_count`: Number of untracked files
  - `plugin.git_status.additions`: Number of added lines
  - `plugin.git_status.deletions`: Number of deleted lines

While the agent is online, these fields are computed when the agent state is queried.
