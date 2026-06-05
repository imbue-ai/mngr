Fixed the tutorial's Docker resource-limit example: `mngr create my-task
--provider docker -s cpus=2` used a bare `cpus=2` token, which `docker run`
would have interpreted as the image name rather than a CPU limit. The example
now uses the correct `--cpus=2` flag. Also strengthened the corresponding e2e
release test to specify an agent type and to verify the CPU limit is actually
applied inside the created container.
