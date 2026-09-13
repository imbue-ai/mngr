# Backup restore

Understanding this behavior corpus calls for the tmr-behaviors skill; consult it when reading this file.

This folder specifies the in-place restore of a workspace backup, as observed through the desktop client's restore operation and the state of the workspace afterwards.
A restore rewinds the workspace's persistent data to a chosen backup snapshot and then brings the workspace's services back up.

A service is *restore-critical* when a restored workspace is not meaningfully usable by its user without it.
The restore's verdict gates on restore-critical services and on nothing else; every other service's recovery is reported, never fatal.
The system interface is currently the sole restore-critical service.
