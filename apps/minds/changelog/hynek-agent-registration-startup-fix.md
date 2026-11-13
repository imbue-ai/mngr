Starting the app with a stopped remote workspace no longer logs "Failed to read the latchkey permissions of host ... Host not found" for it. A change to a workspace's registered agents is now carried to its machine only once discovery has looked at the workspace since the app started and found it running; until then it is held, where before the attempt failed and the change was lost.

The errors logged when a workspace's machine cannot be read or pushed to now end with the host state discovery reports.
