The launch-to-first-message end-to-end script no longer writes the Anthropic API key it signs in with into its uploaded CI log.

The workflow tees the script's output to a file and uploads it as a build artifact. loguru annotates each traceback frame with the values of the variables on that line, so any failing run wrote the live key into that file. GitHub's secret masking rewrites the console log only -- it never touches an uploaded file -- so the key was readable by anyone who could download the artifact.

The key is now held as a `SecretStr` from the moment it is read, so every frame that names it renders `**********` instead, and the tracebacks the script logs no longer carry frame values at all. Stack frames, source lines and the exception message are unchanged, so a failure is still diagnosable from the artifact.
