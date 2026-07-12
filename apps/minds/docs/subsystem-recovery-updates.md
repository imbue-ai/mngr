discovery producer: max(time of last event, time of last sleep) to decide if broken

switch everything to banner - no auto-restart producer, no consume rdeath or backend death takeover

drop system-services restart tier - just keep host one
CRASHED shouldn't auto-dispatch a host restart - i.e. only auto-dispatch a restart if we're confident that the host is offline and restarting will fix it
UNREACHABLE case for dead inner sshd instead of UNAUTHENTICATED?

try to subtract sleep time in general from timeouts - timeout since we woke
most of the time - retry probing after a sleep happened

add "are we online in general" detector - try hitting some known endpoint

watch for latchkey gateway down - banner again
- minds app should start with login to launch latchkey stuff

latchkey approval nudge - check we're using mngr message in the right way - failing to send/match should fail and retry. at the very least report to sentry

separate: fix the issue of dropping imbue cloud hosts when they're offline

flag to gleb - replace OS-level notifications with some better in-app thing