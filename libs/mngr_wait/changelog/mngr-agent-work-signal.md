Changed: `mngr wait <agent>` with no states named now waits until nothing but you will move the agent, rather than until its process goes idle.

An agent that ends its turn leaving a background task running reports the new `RUNNING_BACKGROUND` state, which is not terminal, so the wait holds open instead of reporting finished work that is still running. Naming states explicitly is unaffected, and `mngr wait <agent> RUNNING_BACKGROUND` waits for exactly that.
