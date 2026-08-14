# Backup restore

This folder specifies the in-place restore of a machine backup, as observed through the desktop client's restore operation and the state of the machine afterwards.
A restore rewinds the machine's persistent data to a chosen backup snapshot and then brings the machine's services back up.

A service is *restore-critical* when a restored machine is not meaningfully usable by its user without it.
The restore's verdict gates on restore-critical services and on nothing else; every other service's recovery is reported, never fatal.
The system interface is currently the sole restore-critical service.
