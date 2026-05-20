# Electron bundling audit (PR #1671, area A.2)

Targeted audit of the build-side embedding of per-tier client config
into the packaged Electron app: `MINDS_CLIENT_CONFIG_BUNDLE` /
`MINDS_ROOT_NAME_BUNDLE` → `apps/minds/scripts/build.js` →
`_bundled/` → `apps/minds/electron/paths.js` →
`apps/minds/electron/backend.js` → `minds run --config-file`.

Verdict scheme as before: **CRITICAL** (packaged app cannot start),
**CONFIRMED BUG**, **DESIGN RISK**, **MINOR**, **NOT AN ISSUE**.

## TL;DR

**The packaged Electron app almost certainly cannot start today.**
Two independent bugs make this near-certain:

1. **F1**: `bundleClientConfig` writes to the **source tree**
   (`apps/minds/imbue/minds/config/envs/_bundled/`) but todesktop
   only ships `resources/`. `getBundledConfigDir()` in packaged mode
   looks at `resources/pyproject/imbue/minds/config/envs/_bundled/`
   which nothing populates → `--config-file` is never passed →
   `minds run` raises "No client config file is set" and exits.
2. **F2**: The standalone `apps/minds/electron/pyproject/uv.lock`
   pins `minds` as `editable = "../../"`. From the packaged location
   `<app>/Resources/pyproject/uv.lock`, `../../` resolves to
   `<app>/Contents/` (or equivalent) — which contains no
   `pyproject.toml`. `uv run` cannot resolve the `minds` package.

Both are the kind of bug that surfaces the **first time** anyone
runs `pnpm dist` and launches the resulting .app. The spec explicitly
acknowledged the gap: *"The actual build configuration
(apps/minds/todesktop.json, package.json, CI workflow) hasn't been
touched; needs a short follow-up pass"*. That follow-up doesn't appear
to have happened.

## Sources

- Spec: `specs/vault-environments/spec.md` (§"Build pipeline location
  for `MINDS_BUILD_TIER`" open question + §"Files to modify"
  Electron sections)
- Docs: `apps/minds/docs/environments.md` (§"Build embedding for the
  desktop client")
- Build: `apps/minds/scripts/build.js`
- Runtime path resolution: `apps/minds/electron/paths.js`
- Runtime backend launcher: `apps/minds/electron/backend.js`
- Standalone pyproject: `apps/minds/electron/pyproject/{pyproject.toml,uv.lock}`
- Packaging config: `apps/minds/todesktop.json`,
  `apps/minds/package.json`
- Python consumer: `apps/minds/imbue/minds/cli/run.py`
  (`--config-file` / `MINDS_CLIENT_CONFIG_PATH` handling),
  `apps/minds/imbue/minds/config/loader.py`
  (`bundled_client_config_path_or_none` — currently unused at
  runtime)

---

## Findings

### F1. The `_bundled/` directory is written into the source tree, but the packaged app looks for it under `resources/pyproject/`

**Verdict: CRITICAL — packaged app cannot start.**

`build.js:bundleClientConfig` (line 399) sets `bundledDir` to:

```js
const bundledDir = path.join(
  ROOT, 'imbue', 'minds', 'config', 'envs', '_bundled'
);
```

where `ROOT = path.resolve(__dirname, '..')` = `apps/minds/`. So
the bundled files land at
`apps/minds/imbue/minds/config/envs/_bundled/{client.toml,root_name}`
— **in the source tree**, not under `apps/minds/resources/`.

`todesktop.json` only ships `resources/`:

```json
"extraResources": [{ "from": "resources/", "to": "." }]
```

`apps/minds/electron/paths.js:getBundledConfigDir` in packaged mode:

```js
return path.join(getPyprojectDir(), 'imbue', 'minds', 'config', 'envs', '_bundled');
```

where `getPyprojectDir()` in packaged mode is
`path.join(getResourcesDir(), 'pyproject')`. So the packaged-mode
lookup is at `<app>/Resources/pyproject/imbue/.../_bundled/`.

`build.js:copyPyproject` only puts `pyproject.toml` + `uv.lock`
into `resources/pyproject/`. There is **no step anywhere** that
copies `apps/minds/imbue/minds/config/envs/_bundled/` into
`resources/pyproject/imbue/...`.

End result:

- `paths.getBundledClientConfigPath()` returns `null`
- `paths.getBundledMindsRootName()` returns `null` →
  `getMindsRootName()` falls through to `process.env.MINDS_ROOT_NAME`
  or `"minds"` (the production root, regardless of what the build
  was tagged as!)
- `backend.js` builds `configFileArgs = []`
- `minds run` is invoked with no `--config-file` and no
  `MINDS_CLIENT_CONFIG_PATH` in `env` (backend.js doesn't set it)
- `minds run` raises:

  ```
  No client config file is set. Activate an env first: ...
  ```

**Fix options:**

1. **Cleanest:** have `bundleClientConfig` write to `resources/` (a
   new `resources/bundled-client/` subdir), and update
   `paths.getBundledConfigDir()` packaged-mode resolution to look
   there. The "source tree only used for dev mode" comment in
   `paths.js` becomes accurate.
2. **Alternative:** have `copyPyproject` ALSO copy the entire
   `_bundled/` directory into `resources/pyproject/imbue/.../`.
   Matches the (incorrect) comment in `paths.js`. Slightly weirder
   conceptually since it interleaves config with the pyproject
   files.

Either way, this needs to be verified by actually building +
launching the packaged app at least once.

---

### F2. The standalone `uv.lock` pins `minds` to an editable source path that doesn't exist in the packaged app

**Verdict: CRITICAL — `uv run` cannot install the minds package.**

`apps/minds/electron/pyproject/uv.lock` includes:

```toml
[[package]]
name = "minds"
version = "0.1.0"
source = { editable = "../../" }
```

The `editable = "../../"` is relative to the lockfile's location.
In dev mode, the lockfile is at
`apps/minds/electron/pyproject/uv.lock` → `../../` =
`apps/minds/` (the real source tree). Works fine.

In packaged mode, `copyPyproject` copies the lockfile to
`<app>/Resources/pyproject/uv.lock`. `../../` from there resolves to
`<app>/` (= `Minds.app/Contents/` on macOS) — a directory that
contains the app bundle internals, not a Python project.

Plus `pyproject.toml`'s `[tool.uv.sources]` is **stripped** by
`copyPyproject` (line 347):

```js
content = content.replace(/\[tool\.uv\.sources\][^\[]*/, '').trimEnd() + '\n';
```

So at runtime, `uv run` sees `minds>=0.1.0` as the dependency, no
explicit source, and a lockfile pointing at an editable path that
doesn't exist. The install fails. The minds backend never starts.

Unless `minds` is published to PyPI (it isn't, based on the package
metadata — no PyPI account, no release workflow that I can see),
this is unrecoverable in the packaged build.

**Fix options:**

1. **Build a real wheel.** Run `uv build apps/minds` (or hatchling
   directly) before packaging, ship the wheel under `resources/`,
   and have the standalone pyproject install from a local file:
   `minds @ file:///path/to/wheel.whl` (or via a custom `[[tool.uv.index]]`
   pointing at a local dir).
2. **Ship the entire source tree** under `resources/` and rewrite
   the lockfile's `editable = "../../"` at copy time to point at
   the new in-bundle path. Heavier (the imbue/ tree is large) but
   simpler operationally.
3. **Vendor minds + its workspace deps as wheels** at build time
   (same as #1 but for every workspace dep listed in the lockfile).

Option 1 is the conventional Electron-app-with-Python-backend
pattern. Whichever option lands, the fix is large enough that it's
worth doing with a CI smoke-test that actually `pnpm dist`s and
launches the resulting .app.

---

### F3. No test or CI step exercises the packaged build path

**Verdict: CONFIRMED COVERAGE GAP (root cause of F1 + F2).**

The build has internal smoke-tests for:

- `bundleLatchkey` runs `node bundledCli --version` to catch
  `ERR_MODULE_NOT_FOUND` at build time
- The two new bundling vars are validated at build time
  (`MINDS_CLIENT_CONFIG_BUNDLE` + `MINDS_ROOT_NAME_BUNDLE` shape +
  pair-required-together check)

But there's **no test** that:

- Runs `pnpm dist` end-to-end in CI
- Launches the packaged app and confirms it can reach
  `minds run --config-file <bundled-path>`
- Confirms the resolved `MINDS_ROOT_NAME` matches what was bundled
- Confirms `~/.minds-<env-name>/` is what the runtime actually
  writes to

Without that test, F1 + F2 wouldn't be caught until someone
manually `pnpm dist`s and launches the .app — which based on the
spec's own "follow-up pass needed" comment, no one has done.

**Fix:** add a CI job (or at minimum a local script) that:

1. Sets `MINDS_CLIENT_CONFIG_BUNDLE=apps/minds/imbue/minds/config/envs/staging/client.toml`
   and `MINDS_ROOT_NAME_BUNDLE=minds-staging`
2. Runs `pnpm build`
3. Runs `node -e 'process.env.HOME = "/tmp/fake-home"; const paths = require("./electron/paths.js"); console.log(paths.getMindsRootName())'`
4. Asserts the output is `"minds-staging"`
5. (Optional extension): runs `pnpm exec todesktop build`, unpacks
   the resulting app, and runs the embedded `uv` against the
   embedded pyproject + lockfile to confirm `minds run --help`
   exits 0.

---

### F4. `getMindsRootName()` silently falls through to `"minds"` (production) when no bundle and no env var

**Verdict: DESIGN RISK (defaults to production silently).**

`paths.js:getMindsRootName`:

```js
function getMindsRootName() {
  const bundled = getBundledMindsRootName();
  if (bundled) return bundled;
  const fromEnv = process.env.MINDS_ROOT_NAME;
  if (fromEnv) { /* validate + return */ }
  return 'minds';  // <-- silent default
}
```

Combined with F1 (bundled never populated in packaged mode) this
means a packaged build that was MEANT to be staging or a beta tier
silently writes to `~/.minds/` (the **production** root). If a user
already has a production install on the same machine, the beta
build would corrupt the production user's mngr profile, auth
state, agents, etc.

The docstring says *"Default to 'minds' (production) for the case
where dev mode runs without activation"* — but in packaged mode
this default fires too, silently. The Python backend then refuses
to start without `--config-file`, so the damage is limited to
"the packaged app starts a `minds run` subprocess that exits
immediately." But the data-dir math (`getDataDir()`,
`getMngrHostDir()`, `getMngrPrefix()`) all return production paths
before that subprocess is even spawned, so any code path that
touches those before the subprocess exits (e.g., log file
creation in `getLogDir()`) writes to `~/.minds/logs/`.

**Fix:** in packaged mode, refuse loudly when no bundle was
embedded:

```js
function getMindsRootName() {
  const bundled = getBundledMindsRootName();
  if (bundled) return bundled;
  if (!isDev()) {
    throw new Error(
      'Packaged build has no bundled root_name. The build was made ' +
      'without MINDS_ROOT_NAME_BUNDLE set; the packaged app cannot ' +
      'choose between production / staging / beta safely. Rebuild ' +
      'with MINDS_CLIENT_CONFIG_BUNDLE + MINDS_ROOT_NAME_BUNDLE set.'
    );
  }
  // dev-mode fallback (env var or production default) ...
}
```

---

### F5. `backend.js` doesn't set `MINDS_CLIENT_CONFIG_PATH` in the child env

**Verdict: MINOR (currently masked by F1).**

In packaged mode, `backend.js` builds the child's env:

```js
env = {
  ...process.env,
  ...
  MINDS_ROOT_NAME: mindsRootName,
  MNGR_HOST_DIR: mngrHostDir,
  MNGR_PREFIX: mngrPrefix,
  ...
};
```

No `MINDS_CLIENT_CONFIG_PATH`. The intent is that `--config-file`
is passed explicitly (`configFileArgs`), which takes precedence
over the env var. That's correct when `getBundledClientConfigPath()`
returns a real path. When it returns `null` (always today, per F1),
`configFileArgs = []` and the child inherits `process.env`
unchanged — which may or may not carry `MINDS_CLIENT_CONFIG_PATH`
from the user's shell.

**Once F1 is fixed,** this is harmless: `--config-file` is always
passed and `MINDS_CLIENT_CONFIG_PATH` doesn't matter. Until then,
the behavior depends on whether the user happens to have a
parent-shell env var set, which is operator-dependent and
unpredictable.

Worth being explicit: in packaged mode, after F1 is fixed, set
`MINDS_CLIENT_CONFIG_PATH` in the child env too as belt-and-
suspenders. Or delete it explicitly (`delete env.MINDS_CLIENT_CONFIG_PATH`)
to prevent a stale parent-shell var from misdirecting the backend.

---

### F6. `bundled_client_config_path_or_none()` exists in Python but is never called

**Verdict: MINOR.**

`apps/minds/imbue/minds/config/loader.py:55-66` defines:

```python
def bundled_client_config_path_or_none() -> Path | None:
    bundled = _BUNDLED_DIR / _CLIENT_FILENAME
    if bundled.is_file():
        return bundled
    return None
```

A grep for callers of this function in the Python source returns
nothing — it's dead code. The intent (presumably) was that the
Python backend would have a fallback: if `--config-file` wasn't
passed AND `MINDS_CLIENT_CONFIG_PATH` wasn't set, the Python code
could itself look at `_bundled/client.toml` as a last resort.
Today it doesn't.

Two consequences:

- The Electron side is the only path that knows how to find the
  bundled file. If a user ever runs `uv run minds run` directly
  (e.g., from a hatch-built minds wheel) against an installed
  build that had `_bundled/client.toml` populated, the Python
  backend would still refuse to start.
- The function exists, takes up code review attention, and does
  nothing. Either wire it up (have `run.py` fall back to it when
  `config_file is None`) or delete it.

**Fix option:** wire it up. `run.py:148-153` becomes:

```python
if config_file is None:
    config_file = bundled_client_config_path_or_none()
    if config_file is None:
        raise click.ClickException(
            "No client config file is set. ..."
        )
```

That also provides a Python-side fallback path for non-Electron
launches of a wheel-built minds.

---

### F7. The build-time validation regex on `MINDS_ROOT_NAME_BUNDLE` is duplicated in 3 places

**Verdict: MINOR.**

The same regex appears in:

- `apps/minds/scripts/build.js:445` (build-time validation)
- `apps/minds/electron/paths.js:116` (read-back validation)
- `apps/minds/electron/paths.js:147` (env-var validation)
- `apps/minds/imbue/minds/bootstrap.py` (the Python runtime
  bootstrap, presumably)

If the validation rules ever change (e.g., longer env names), all
4 places need to track. No DRY-pattern is in place (e.g., a single
constant in a JS file that both build.js and paths.js import).
Worth a tiny constant.

---

### F8. The Python wheel-build step is missing from `pnpm dist`

**Verdict: CONFIRMED BUG (consequence of F2).**

For F2's "build a real wheel" fix to work, `pnpm dist` would need
to:

1. Run `uv build apps/minds` to produce a wheel under `dist/`
2. Run `uv build libs/mngr_claude libs/mngr_modal libs/mngr_latchkey
   libs/mngr_forward libs/mngr_ovh libs/mngr_vps_docker
   libs/concurrency_group libs/imbue_common libs/imbue_mngr`
   (every workspace dep listed in the standalone uv.lock)
3. Copy all wheels into `resources/wheels/`
4. Rewrite the standalone `pyproject.toml` / `uv.lock` to install
   from `file://` URLs pointing at the bundled wheels

None of this happens today. `package.json:dist`:

```json
"dist": "pnpm build && pnpm exec todesktop build"
```

`pnpm build` is just `node scripts/build.js`. `scripts/build.js`
doesn't shell out to `uv build`. So no wheels are produced.

(This is really an F2 sub-finding; called out separately because
the fix is in the build pipeline, not in the bundling code.)

---

### F9. `_bundled/.gitignore` correctly ignores `*` + un-ignores itself

**Verdict: NOT AN ISSUE.**

The committed `.gitignore` content is:

```
*
!.gitignore
```

This is the right shape: every artifact produced by
`bundleClientConfig` is gitignored, but the directory itself is
preserved in source control via the un-ignored `.gitignore` file.
No risk of accidentally committing a build-baked
`client.toml`/`root_name`.

---

### F10. `bundleClientConfig` correctly fails when only one of the two vars is set

**Verdict: NOT AN ISSUE (defensive coding worked).**

`build.js:432-439`:

```js
if (!configBundle || !rootNameBundle) {
  throw new Error(
    'MINDS_CLIENT_CONFIG_BUNDLE and MINDS_ROOT_NAME_BUNDLE must both be ' +
      'set (or both unset); ...'
  );
}
```

Closes the "operator sets one but forgets the other" footgun at
build time. Good.

---

### F11. `bundleClientConfig` cleans stale artifacts before each build

**Verdict: NOT AN ISSUE (defensive coding worked).**

`build.js:416-420` deletes any pre-existing `client.toml` /
`root_name` before re-running. Prevents a developer who flips the
bundle vars off from accidentally shipping yesterday's production
URL. Good.

---

## Summary

| Finding | Verdict | Action |
|---|---|---|
| F1: `_bundled/` written to source tree, looked up under `resources/` | **CRITICAL** | Either write to `resources/` or copy source-tree → `resources/` |
| F2: standalone `uv.lock` pins `minds = editable = "../../"` (no source there in packaged build) | **CRITICAL** | Build a real wheel + ship; or ship the full source tree under `resources/` |
| F3: no CI step exercises the packaged build path | **COVERAGE GAP** | Add a `pnpm dist` smoke test |
| F4: `getMindsRootName()` silently defaults to production | **DESIGN RISK** | Refuse loudly in packaged mode without a bundle |
| F5: `backend.js` doesn't set `MINDS_CLIENT_CONFIG_PATH` in child env | **MINOR** | After F1 fix: set or `delete` the var explicitly |
| F6: `bundled_client_config_path_or_none()` is dead code | **MINOR** | Wire it up as a Python-side fallback, or delete |
| F7: `MINDS_ROOT_NAME_BUNDLE` regex duplicated 4 places | **MINOR** | Single shared constant |
| F8: no wheel-build step in `pnpm dist` | **CONFIRMED BUG (consequence of F2)** | Add `uv build` to the dist pipeline |
| F9: `_bundled/.gitignore` shape | **NOT AN ISSUE** | — |
| F10: bundle-vars-required-together check | **NOT AN ISSUE (good)** | — |
| F11: bundle directory cleaned before each build | **NOT AN ISSUE (good)** | — |

### Practical "what I'd do before shipping a packaged build"

1. **Fix F1 + F2 + F4** (minimum viable). Validate by actually
   running `pnpm dist` + launching the .app on macOS and Linux. The
   end-to-end test should:
   - confirm the bundled `client.toml` is found at runtime
   - confirm the resolved `MINDS_ROOT_NAME` matches what was bundled
   - confirm `minds run` starts successfully
   - confirm the app writes to `~/.minds-<env-name>/` (not `~/.minds/`)

2. **Add F3's smoke test** as a CI gate so the next refactor
   doesn't silently re-break the packaging.

3. **Defer F5 / F6 / F7** until the basics work.

### Reflection

Your intuition was correct: the bundling code was written from the
spec, but the packaging-side glue (`todesktop.json`,
`copyPyproject`, the standalone `uv.lock`'s `editable` source) was
never updated to match. The spec itself flagged this as an open
question (*"the actual build configuration ... hasn't been touched;
needs a short follow-up pass"*), and that follow-up didn't land.
The build-time `bundleClientConfig` runs cleanly and writes files
to disk, so it LOOKS like the system works — but those files never
reach the packaged app, and the runtime quietly falls through to
the production default.

The good news: the JavaScript-side resolution (`paths.js`) was
written defensively, with validation regexes that mirror the
Python runtime's. So once the file gets to the right place, the
rest of the chain works. The fix is plumbing, not redesign.
