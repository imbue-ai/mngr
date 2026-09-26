Discovery now caches a host's agents only from a listing that includes its `system-services` agent. A listing without it is partial, for example one cut off by the container being stopped, and used to overwrite the complete cached set. A stopped workspace is served from that cache and never re-listed, so it stayed in minds as "unreachable" with no Start button, even across app restarts, until the cache file was removed by hand.

A partial listing is now answered with the host's last complete listing (marked stale), the same as a failed listing, so a running workspace keeps its controls while it lasts instead of reading as "unreachable".

A stopped workspace whose cached agents already lack the `system-services` agent now gets that agent from its lifecycle row, so it shows up as startable again and its next live listing rewrites the cache.
