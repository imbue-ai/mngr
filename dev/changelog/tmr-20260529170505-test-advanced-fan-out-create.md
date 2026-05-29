Extended the root `.gitignore` TMR section to also ignore `**/tmr-report/`
directories (previously only `**/tmr_*/` with an underscore was covered), so the
TMR test-runner's report output is not flagged as an untracked file.
