have to be careful with offline mode here:
    we could install hooks such that periodically, and while shutting down, we snapshotted some data into the agent state dir
    and then if the machine is offline, we could try using that data instead (and make a note that we seems offline)

