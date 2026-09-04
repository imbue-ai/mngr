The Updates panel now answers "where am I?" in two fixed lines and lets each channel row answer "where is it?".

The header reads `You're on Minds <version>.` and, below it, where you stand with your channel -- `You're up to date with Stable.` when nothing is waiting, and something else in the states where that would be false: a download in flight, a check that failed, and running ahead of your own channel. A finished download keeps its own notice, since it is the one state asking you to act rather than reporting where you are.

Each channel's version moves out of its heading and into its blurb: `Stable / Ready for everyday use. Currently on 0.5.0.` A channel that is mid-rollout says `Currently rolling out 0.5.0.` instead, because a build being staged by `rollout_percentage` means the channel serves two versions at once -- one to the installs inside the bucket and another to the rest -- so `Stable (0.4.2)` beside the name is wrong for whichever group is not being described. Neither form ever shows a percentage.

Being ahead of your channel now reads `You're ahead of Stable and will get updates when it catches up.` rather than naming both versions in a sentence, since the channel's own row already states what it serves.

Fixed: a stored channel this build cannot serve stopped updates entirely. A tier that configures no `update_feed_base_url` serves stable alone and `feedForChannel` raises by name for any other, so a preference stored while on a tier that did serve it made every later check, peek and channel switch throw -- surfacing as a broken feed rather than as a preference that no longer applies. The stored channel now falls back to one the build serves, and says why in the log, which is what an unrecognised channel name already did.
