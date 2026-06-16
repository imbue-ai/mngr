Renamed the `_AGENT_TAG_FIELDS` constant imported from `mngr_vps_docker` to the
public `AGENT_TAG_FIELDS` (matching its sibling `AGENT_TAG_PREFIX`), so the
AWS tag-mirror code no longer imports a private name across modules. No
behavior change.
