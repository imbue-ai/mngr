Added a design spec (`specs/gcp-azure-stop-start-lifecycle/spec.md`) for bringing
the AWS stop/start (idle-pause + resume) lifecycle to the GCP and Azure providers:
`mngr stop` halts live-instance compute billing (disk preserved), `mngr start`
resumes the session with all files intact, and a stopped VM stays visible in
`mngr list` and resumable by name.
