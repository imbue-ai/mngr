#!/usr/bin/env bash
# Run the minds.app VM test harness against a given build artifact.
#
# Usage: run-test.sh <build-url-or-local-zip-or-app> <persona-name> [--keep-vm]
#
# The first argument is either:
#   - an https:// URL to a ToDesktop zip artifact (downloaded)
#   - a path to a local .zip (copied + unzipped)
#   - a path to an unzipped minds.app bundle (copied as-is)
#
# The persona-name must correspond to a previously built persona image
# (see build-persona.sh). For v1, that's "minds-fresh".
#
# Results are copied to apps/minds/scripts/vm-testing/.results/<ts>-<persona>/.

set -euo pipefail

HERE="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$HERE/lib.sh"

require_cmd tart
require_cmd sshpass
require_cmd unzip

build_source="${1:-}"
persona="${2:-}"
keep_vm=0
[[ "${3:-}" == "--keep-vm" ]] && keep_vm=1

if [[ -z "$build_source" || -z "$persona" ]]; then
    die "usage: $0 <build-url-or-local-zip-or-app> <persona-name> [--keep-vm]"
fi

if ! tart list 2>/dev/null | awk 'NR>1 {print $2}' | grep -Fxq "$persona"; then
    die "persona image '$persona' not found; run build-persona.sh $persona first"
fi

ts="$(date +%Y%m%d-%H%M%S)"
vm_name="${persona}-run-${ts}"
work_dir="$(mktemp -d -t minds-vm-XXXXXXXX)"
share_dir="$work_dir/share"
mkdir -p "$share_dir/results"
local_results="$HERE/.results/${ts}-${persona}"
mkdir -p "$local_results"

dmg_mount_point=""

cleanup() {
    if [[ -n "$dmg_mount_point" && -d "$dmg_mount_point" ]]; then
        /usr/bin/hdiutil detach "$dmg_mount_point" -force -quiet || true
        dmg_mount_point=""
    fi
    if [[ "$keep_vm" == "1" ]]; then
        log "--keep-vm set; leaving $vm_name running. Stop manually with: tart stop $vm_name && tart delete $vm_name"
    else
        stop_and_delete_vm "$vm_name"
    fi
    rm -rf "$work_dir"
}
trap cleanup EXIT

extract_from_dmg() {
    local dmg="$1"
    log "attaching $dmg"
    # Use -plist to get a structured response we can parse robustly; default
    # macOS mountpoints live under /Volumes/<name-N>, which we cannot guess
    # ahead of time when the dmg's volume name is unknown.
    local plist
    plist="$(/usr/bin/hdiutil attach -nobrowse -noverify -noautoopen -plist "$dmg")" \
        || die "hdiutil attach failed for $dmg"
    local mount_point
    mount_point="$(python3 - "$plist" <<'PY' || true
import plistlib, sys
data = plistlib.loads(sys.argv[1].encode())
for entry in data.get("system-entities", []):
    mp = entry.get("mount-point")
    if mp:
        print(mp)
        break
PY
    )"
    [[ -n "$mount_point" && -d "$mount_point" ]] || die "could not determine dmg mount point"
    dmg_mount_point="$mount_point"
    log "dmg mounted at $mount_point"
    local found=""
    for candidate in "$mount_point"/*.app; do
        [[ -d "$candidate" ]] || continue
        found="$candidate"
        break
    done
    [[ -n "$found" ]] || die "no .app bundle inside $dmg"
    log "copying $found out of dmg"
    /usr/bin/ditto "$found" "$share_dir/$(basename "$found")"
    /usr/bin/hdiutil detach "$mount_point" -quiet || true
    dmg_mount_point=""
}

log "preparing minds.app from: $build_source"
case "$build_source" in
    http://*|https://*)
        # Treat the URL extension as the hint for what format to expect.
        case "$build_source" in
            *.dmg|*/dmg/*)
                log "downloading dmg from $build_source"
                curl -fL --progress-bar -o "$share_dir/minds.dmg" "$build_source"
                extract_from_dmg "$share_dir/minds.dmg"
                ;;
            *)
                log "downloading zip from $build_source"
                curl -fL --progress-bar -o "$share_dir/minds.zip" "$build_source"
                log "unzipping"
                unzip -q "$share_dir/minds.zip" -d "$share_dir/"
                ;;
        esac
        ;;
    *.dmg)
        [[ -f "$build_source" ]] || die "dmg file not found: $build_source"
        cp "$build_source" "$share_dir/minds.dmg"
        extract_from_dmg "$share_dir/minds.dmg"
        ;;
    *.zip)
        [[ -f "$build_source" ]] || die "zip file not found: $build_source"
        cp "$build_source" "$share_dir/minds.zip"
        unzip -q "$share_dir/minds.zip" -d "$share_dir/"
        ;;
    *.app)
        [[ -d "$build_source" ]] || die "app bundle not found: $build_source"
        # `ditto` is the macOS-correct way to copy a bundle and keep
        # extended attributes (notably the code signature).
        /usr/bin/ditto "$build_source" "$share_dir/$(basename "$build_source")"
        ;;
    *)
        die "unrecognized build source: $build_source (expect URL, .dmg, .zip, or .app)"
        ;;
esac

minds_app_path=""
for candidate in "$share_dir"/*.app; do
    [[ -d "$candidate" ]] || continue
    minds_app_path="$candidate"
    break
done
[[ -n "$minds_app_path" ]] || die "no .app bundle found under $share_dir after unzip"
log "found app bundle: $minds_app_path"

# Tart's shared-volume virtiofs misreports the symlinks inside an Electron
# .app bundle as cyclic ("Too many levels of symbolic links"), which breaks
# any in-VM copy. Tar the bundle on the host (where the symlinks are sane)
# and untar it in the VM. The harness's install_app step understands either
# a directory or a tarball.
log "tarring minds.app for transport into VM"
( cd "$share_dir" && /usr/bin/tar -cf minds.app.tar "$(basename "$minds_app_path")" )
rm -rf "$minds_app_path"
app_artifact_basename="minds.app.tar"

# If TEMPLATE_GIT_REF is set, materialize a host-side clone of the
# template repo at that ref into the shared dir. The harness then passes
# the VM-mounted path as git_url, which pins the test to a known-good
# revision -- important when the bundled minds.app and the template main
# branch have drifted.
template_in_vm=""
if [[ -n "${TEMPLATE_GIT_REF:-}" ]]; then
    template_url_src="${TEMPLATE_GIT_URL:-https://github.com/imbue-ai/forever-claude-template.git}"
    log "cloning $template_url_src at $TEMPLATE_GIT_REF into shared dir"
    git clone --quiet "$template_url_src" "$share_dir/template"
    ( cd "$share_dir/template" && git checkout --quiet "$TEMPLATE_GIT_REF" )
    template_in_vm="/Volumes/My Shared Files/share/template"
fi

# Stage the harness under the same shared dir so the VM gets the most
# recent version every run without scp.
cp -R "$HERE/harness" "$share_dir/harness"

log "cloning persona '$persona' to throwaway VM '$vm_name'"
tart clone "$persona" "$vm_name"

log "booting $vm_name with shared dir $share_dir"
tart run "$vm_name" --vnc-experimental --dir=share:"$share_dir" &
boot_pid=$!
sleep 2
kill -0 "$boot_pid" 2>/dev/null || die "tart run exited prematurely"

ip="$(wait_for_vm_ssh "$vm_name" 180 240)"

# Shared dir is mounted (read-only by default!) at
# /Volumes/My Shared Files/share/ inside the VM. Verify it.
share_in_vm='/Volumes/My Shared Files/share'
vm_ssh "$ip" "ls '$share_in_vm/' >/dev/null 2>&1" \
    || die "shared volume not mounted at '$share_in_vm' inside VM"

# The shared mount is read-only inside the VM -- writes from the guest fail
# with EROFS. The harness writes its results to a writable scratch dir
# inside the guest; we scp them back out after it exits.
results_in_vm="/tmp/minds-harness-results"
vm_ssh "$ip" "rm -rf '$results_in_vm' && mkdir -p '$results_in_vm'"

# Default AI provider selection: if the user passed an ANTHROPIC_API_KEY,
# wire it through as the API_KEY provider. Otherwise default to SUBSCRIPTION,
# which requires the VM to already have claude credentials -- generally only
# useful when running against a persona that pre-provisions them.
ai_provider="${AI_PROVIDER:-}"
if [[ -z "$ai_provider" ]]; then
    if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
        ai_provider="API_KEY"
    else
        ai_provider="SUBSCRIPTION"
    fi
fi
if [[ "$ai_provider" == "API_KEY" && -z "${ANTHROPIC_API_KEY:-}" ]]; then
    die "AI_PROVIDER=API_KEY but ANTHROPIC_API_KEY is unset on the host"
fi

# Materialize the harness invocation as a host-side script and ship it to
# the VM. This avoids fragile shell-quoting through ssh of a multi-line
# command with embedded variables.
runner="$share_dir/harness/run-in-vm.sh"
cat > "$runner" <<EOF
#!/usr/bin/env bash
# The cirruslabs vanilla macOS image does not include Xcode Command Line
# Tools, so /usr/bin/python3 is a stub that triggers a GUI install prompt
# on first run. We bootstrap a managed Python via the uv that we extract
# from the shipped tarball -- a fully-static executable that does not
# require CLT and can fetch a Python interpreter to its own dir.
set -euo pipefail
export MINDS_APP_PATH="$share_in_vm/$app_artifact_basename"
export RESULTS_DIR="$results_in_vm"
export TEMPLATE_GIT_URL="${template_in_vm:-${TEMPLATE_GIT_URL:-https://github.com/imbue-ai/forever-claude-template.git}}"
export LAUNCH_MODE="${LAUNCH_MODE:-LOCAL}"
export AI_PROVIDER="$ai_provider"
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
export HOST_NAME="${HOST_NAME:-vmtest-$ts}"
export TEST_PROMPT="${TEST_PROMPT:-Print exactly the literal string PINGPONG-OK on a line by itself, with no other output.}"
export EXPECTED_RESPONSE="${EXPECTED_RESPONSE:-PINGPONG-OK}"
export APPLY_QUARANTINE="${APPLY_QUARANTINE:-0}"
export BACKEND_READY_TIMEOUT="${BACKEND_READY_TIMEOUT:-300}"
export CREATE_TIMEOUT="${CREATE_TIMEOUT:-600}"
export MESSAGE_TIMEOUT="${MESSAGE_TIMEOUT:-300}"

# Extract just the uv binary from the bundle tarball into /tmp so we can
# run python before the app is installed to /Applications/. Using
# --strip-components keeps the path short and avoids relying on minds.app
# being unpacked into /Applications first.
mkdir -p /tmp/vm-harness-bootstrap
/usr/bin/tar -xf "$share_in_vm/$app_artifact_basename" -C /tmp/vm-harness-bootstrap \
    'minds.app/Contents/Resources/uv/uv'
uv_bin=/tmp/vm-harness-bootstrap/minds.app/Contents/Resources/uv/uv
[ -x "\$uv_bin" ] || { echo "bundled uv missing at \$uv_bin" >&2; exit 1; }

# Install python once into /tmp (writable, throwaway). uv caches the
# download, so subsequent harness re-runs reuse it.
export UV_PYTHON_INSTALL_DIR=/tmp/uv-python
export UV_CACHE_DIR=/tmp/uv-cache
"\$uv_bin" python install --quiet 3.12

exec "\$uv_bin" run --python 3.12 --no-project \
    "$share_in_vm/harness/run-harness.py"
EOF
chmod 700 "$runner"

log "invoking harness inside VM (streaming output)"
set +e
vm_ssh "$ip" "bash '$share_in_vm/harness/run-in-vm.sh'" 2>&1 \
    | tee "$local_results/harness-stdout.log"
harness_rc=${PIPESTATUS[0]}
set -e
log "harness exited with rc=$harness_rc"

log "copying results out of VM"
vm_scp -r "$VM_USER@$ip:$results_in_vm/." "$local_results/" || true

log "results saved to $local_results"
if (( harness_rc == 0 )); then
    log "PASS"
else
    log "FAIL (see $local_results/summary.json)"
fi
exit "$harness_rc"
