Bumped the LIMA launch-mode progress-bar duration estimate from 300s to 600s on
the workspace creation page: LIMA mode now boots a VM *and* builds the project
image inside it (the workspace runs in a Docker container in the Lima VM), so a
cold create takes longer than the old run-directly-in-the-VM path. This only
affects the creating-page animation, not any hard timeout.
