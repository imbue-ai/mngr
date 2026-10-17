Muting an agent no longer flashes the row back to the section it came from.

A refresh that was already running when `m` was pressed read the agent before the keypress, so
landing it put the row back until the next read moved it to Muted again. Board values now record
when they were read, and a mute set on the board outlasts any fetch that read the agent earlier.

Pressing `m` a second time during that window used to undo the first mute rather than repeat it,
because the board decided which way to flip from what it was showing while the write decided from
what was stored. The write now takes the state to set, so the two cannot disagree.
