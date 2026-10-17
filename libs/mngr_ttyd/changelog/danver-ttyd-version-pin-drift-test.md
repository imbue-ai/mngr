Added a test that fails if the ttyd version pinned in mngr's Dockerfile drifts from the one the
plugin installs.

ttyd 1.7.7 is pinned in two places: `TTYD_VERSION` in the plugin, and the download URL in
`libs/mngr/imbue/mngr/resources/Dockerfile`, which preinstalls ttyd into the image. They cannot
be collapsed into a single pin -- mngr_modal ships each Dockerfile instruction to Modal as its
own `dockerfile_commands` call, so a build ARG expands to empty in the RUN that would use it,
which is why the neighbouring restic and offload pins are inline literals too.

The test asserts the two agree, so bumping one without the other fails instead of silently
shipping an image whose ttyd differs from the one agents install for themselves. This is the
same approach already used for `CLAUDE_CODE_VERSION`.
