# The update and get-help chats Minds starts run on your signed-in account

A machine keeps one config dir per provider account it is signed in to, and leaves the
harness's own config-dir variable unset otherwise, so a chat created without being told which
account to use lands on a directory that holds no credential. The machine's own UI tells every
chat it creates; the two chats Minds starts from outside -- the `/update-self` chat behind
"Update now", and the get-help chat behind "Ask an agent" -- did not, so on any machine built
from `minds-v0.5.0` or newer they answered every turn "Not logged in - Please run /login" and
the update could not begin. Machines older than that keep no per-account dirs and were never
affected.

Both now ask the machine which account a new chat should run on and bind the chat to it, so an
update started from the app runs as the account the user is signed in with, and so does the
worker the update hands its merge to. The question is put to the machine's own resolver rather
than answered from out here, so the app and the machine cannot disagree about which account is
the default one, and a machine too old to keep accounts answers that it keeps none and is
launched exactly as before.

A machine that keeps accounts but has none the chat could run on is now told so -- "This
machine has no signed-in Anthropic account for the update agent to run on" -- instead of being
handed a chat that cannot take a turn. The update's run slot is released when that happens, so
signing in and pressing Update again works. A machine whose resolver broke rather than
declined is reported as one the app could not get an answer out of, so nobody is sent to sign
in over a fault signing in cannot fix.
