Modal's snapshot-and-shutdown endpoint now waits for the sandbox to actually stop before
reporting success. Modal's terminate call is fire-and-forget, so the endpoint used to answer
"success" the instant it asked for a shutdown -- leaving a sandbox that was still running, and
still billing, while mngr believed it was gone. The response now also carries the sandbox's
exit code, which is the proof that the shutdown was observed rather than merely requested.
