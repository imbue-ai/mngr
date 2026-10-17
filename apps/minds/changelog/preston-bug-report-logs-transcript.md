A bug report filed from a machine can now carry that machine's own diagnostics, so a report no longer has to start with a round-trip asking you to go fetch logs.

The "Report a bug" form gained two checkboxes above the remote-access one — "Include workspace logs" and "Include recent chats". Both are checked by default and remember their setting for next time, and both are hidden when the help overlay was opened outside a machine. Unchecking one means those files are genuinely absent from that report rather than collected and quietly ignored. The text below the form says what is always attached regardless: the app's own logs, which go with every report either way.

With workspace logs included, the machine gathers its host health (disk, memory, uptime and load, and recent memory-shed events), its `supervisorctl status`, and the tail of each system service's log. Services you created are identified through the machine's own service definitions rather than a hardcoded list of names, so your app's logs — your content, not diagnostics — are never collected. The report also records which commit the machine's checkout is on and whether it has uncommitted changes.

With recent chats included, every conversation written to in the last two hours rides along, newest first; when none is that recent, the one you last spoke in still does. They arrive as a zip holding one conversation per file, each keeping the `.jsonl` its harness wrote so it opens in whatever already reads a transcript, and carrying the time that chat was last written to. Which agent and which harness a conversation came from is in its filename rather than injected into the transcript text.

Nothing leaves the machine unscanned. Two secret scanners read the logs, every conversation, and the filenames they will be packed under — as plain text, before anything is compressed — and a report ships only what they cleared. One conversation with a finding withholds all of them rather than quietly sending the rest, because a partial set is indistinguishable from a complete one. If a scanner cannot run, nothing is attached. Whatever is withheld is replaced by a short reason recorded on the report, so a reader can tell "there were none" from "we would not send them".

The machine's captured console output attaches too, and the app's own logs as always. Both are attached unscanned, which is what they have always been.

Sending is immediate: the report id appears as soon as the report is filed, and the machine's diagnostics are collected and uploaded behind it. The report says where each attachment will be readable and, when it lands, records what each one ended up doing — attached, or the reason it was not. A collection that fails afterwards cannot take the filed report back. While a report is sending the help window stays up until the id (or an error) appears, so a second submission cannot race the first.

A report's files are attached only to the report they were collected for. Previously they sat in the log folder every automatic error report also sweeps, so an unrelated crash between two reports would have carried the earlier report's chats and logs along with it.

Machines built before this shipped report that plainly: their template has no collector, so a report from one says so rather than appearing to have found nothing.
