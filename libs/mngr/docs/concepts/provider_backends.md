# Provider Backends

A [provider instance](./providers.md) is a configured instance of a **"provider backend"** that creates and manages [hosts](./hosts.md).

A "provider backend" (like `docker`, `modal`, or `aws`) defines a *parameterized* way to create and manage hosts. A *provider instance* is a configured endpoint of that backend.

This lets you have multiple provider instances of the same backend: multiple Modal accounts, AWS accounts, remote Docker hosts, or even remote `mngr` instances that manage their own local agents.

## Choosing a Provider Backend

Each provider backend has different trade-offs:

|                         |     Local      |      Docker       |       Modal        |
|-------------------------|:--------------:|:-----------------:|:------------------:|
| **Cost**                |      Free      |       Free        |    Pay-per-use     |
| **Setup**               |      None      |  Install Docker   |   Modal account    |
| **Isolation**           |     ❌ None     |   🔶 Container    |     ✅ Full VM      |
| **Performance**         |    ✅ Native    |   ✅ Near-native   | 🔶 Network latency |
| **Accessible anywhere** |       ❌        |         ❌         |         ✅          |
| **Snapshots**           |       ❌        | ✅ `docker commit` |      ✅ Native      |
| **Resource limits**     |       ❌        |     ✅ cgroups     |      ✅ Native      |
| **GPU support**         | ✅ If available | 🔶 Requires setup |    ✅ On-demand     |

**When to use each:**

- **Local**: Fast iteration with trusted agents. No overhead, but no isolation.
- **Docker**: Isolation without cloud costs. Good for untrusted agents on your machine.
- **Modal**: Full isolation in the cloud. Best for untrusted agents or long-running work. Access from anywhere.
- **SSH**: Static pool of pre-existing SSH-accessible machines. mngr connects to them but does not create or destroy them.

## Custom Provider Backends

Browse [100's of additional plugins](http://imbue.com/mngr/plugins) [future] for other provider backends (like AWS [future], GCP, Kubernetes, etc.).

Only `local` and `ssh` are built in; `docker`, `modal`, `lima`, and the cloud backends are plugins that register themselves via the `register_provider_backend` hook. See [the plugin API](./api.md) and the docker or modal plugin for examples.
