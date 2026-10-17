The workspace vocabulary was renamed around "creations": users make apps (opened as tabs), skills (an automation is a skill run on a schedule), data, and customizations; "service" now means a background supervisord program only, and "application"/"artifact"/"web service" are retired terms.

The workspace glossary gains creation, app, service, automation (future), customization, inspiration, and template base entries. Minds docs describe the template's three-way system/ split (system/apps, system/services, system/libs) and the app registry's new name (data/.state/apps.toml).

The minds docs no longer claim the app watcher reconciles with the Cloudflare forwarding API: the watcher only writes discovery events to events/services/events.jsonl, and Cloudflare forwarding registration happens from the desktop client's sharing flow.

The backup workspace scripts resolve the backup-service code at system/services/host_backup first (falling back to the older system/libs/host_backup and libs/host_backup layouts), and the snapshot-resume release test reads the app registry at apps.toml with an applications.toml fallback, so both span template versions.
