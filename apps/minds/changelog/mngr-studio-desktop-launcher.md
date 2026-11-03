The Linux launcher `install-linux.sh` writes is now `~/.local/bin/studio-desktop`
rather than `~/.local/bin/minds-desktop`. It is the first identifier the app picks
for itself to take the product's `studio` spelling, after `STUDIO_ROOTS_OVERRIDE`.

The old launcher is left in place. It is self-contained -- it execs
`start-desktop.sh` out of the checkout it was written against -- so it keeps
working, and an upgrade that removed it would break the two things that still
name it by absolute path: a Raspberry Pi's menu and autostart entries, and a WSL
install's Windows shortcut. Both are rewritten only by a run that passes
`--raspberry-pi` or sets `WSL_DISTRO_NAME`, so a plain `--version latest` upgrade
would otherwise leave them pointing at a file that no longer exists. Delete
`~/.local/bin/minds-desktop` by hand if you want it gone; only the new name
tracks later installer changes.

The name is `studio-desktop`, not `imbue-studio`: the packaged `.deb` and
AppImage already install an executable called `imbue-studio`, and the generated
launcher puts `~/.local/bin` first on `PATH`, so that spelling would shadow the
packaged binary even inside the launcher's own subprocesses.

`style_guide.md` no longer exempts the app's own identifiers from the product
name. They move to `studio` one at a time, where nothing outside this repo reads
them. `minds-v*` release tags are the standing exception, and the rule now says
why: every provisioned workspace matches those tags with a frozen copy of its own
`update-self` regex, so a `studio-v*` release would stop each existing workspace
updating, with nothing shipped afterwards able to reach the code that failed.
