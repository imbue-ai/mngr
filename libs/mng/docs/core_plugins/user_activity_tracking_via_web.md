# User Activity Tracking via Web [future]

This plugin tracks user activity in web-based agent interfaces to prevent hosts from idling while you're actively working.

## Overview

When you interact with an agent through a web interface (like the ttyd terminal), the host needs to know you're active to avoid auto-stopping. This plugin detects keyboard and mouse activity and reports it to the idle detection system.

Without this plugin, a host might stop while you're reading output or thinking—even though you're actively engaged with it.

## Privacy

**Only timestamps are recorded.** The plugin does not log:
- Actual keystrokes
- Mouse positions
- Page content
- Any other information

The sole purpose is to know "when was the user last active?"—nothing more.

## Configuration

You can customize the debounce interval in your mng config:

```toml
[plugins.user_activity_tracking_via_web]
debounce_ms = 1000   # Minimum ms between activity reports (default)
```

To disable activity tracking entirely:

```bash
mng create --disable-plugin user_activity_tracking_via_web ...
```

## Requirements

This plugin requires:

- **nginx** - Provided by [Local Port Forwarding via FRP and Nginx](./local_port_forwarding_via_frp_and_nginx.md) or similar
- **ngx_http_sub_module** - For injecting the tracking script (included in most nginx builds)
