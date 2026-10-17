An agent can now ask for a synchronized copy of a folder in the same file-sharing request that asks for access to it: the request payload takes an optional `sync` object naming the clash rule. Until now only you could turn syncing on, from the Local files card, after approving the request.

The approval dialog draws the same sync band the Local files card does, from one shared component, so the two cannot drift the way the dialog's earlier copy did. The switch starts on when the agent asked and the folder can be synced; a line under it says the agent asked. You can flip it either way before approving. A folder that cannot be synced greys the switch out with the reason, exactly as the card would.

A sync that cannot be started is refused before anything is granted, so the request stays pending with the reason and you can turn the switch off or pick another folder. The notice the agent receives names where the copy lands on its machine, or says the copy was not enabled when you turned off a sync it asked for.

The "keep a synchronized copy" control is a switch now, like every other on/off control in the Permissions pane, rather than a checkbox.
