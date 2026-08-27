Promoting **stable** now has one more step in the release doc: bump the
connector's hardcoded download fallback to the build being promoted.

That fallback is what `https://minds.imbue.com/download?platform=mac-arm64`
serves while the update feed cannot be read. Leaving it behind stable means an
outage hands people an older build; leaving it *ahead* is the one to avoid,
because `allowDowngrade` is false, so those installs never come back down.

It also changes what the doc's feed-versus-link check can tell you. The two used
to name different builds, so a disagreement meant an outage. Now they agree
whether or not the feed was read, and a disagreement outlasting the connector's
cache means both: the feed could not be read, and the connector has not been
redeployed since the last promotion.
