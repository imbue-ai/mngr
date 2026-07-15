#!/usr/bin/env bash
set -euo pipefail
cd /work/mngr

export SKIP_AUTH=1
# Only Modal is usable in the box; disable every other provider so mngr's discovery/list (which
# minds runs internally) doesn't hard-error on an unreachable provider.
for _p in DOCKER AZURE AWS VULTR LIMA IMBUE_CLOUD GCP OVH; do
  export "MNGR__PROVIDERS__${_p}__IS_ENABLED=false"
done
export MINDS_LATCHKEY_BINARY=/work/mngr/apps/minds/node_modules/.bin/latchkey
# (MNGR__PROVIDERS__MODAL__USER_ID is passed at `docker run`, scoping this box to one Modal env.)

echo ">> activating minds env: ${MINDS_ENV:-staging}"
eval "$(uv run minds env activate "${MINDS_ENV:-staging}")"

# The box IS a computer: a virtual display + window manager, streamed to the browser via noVNC,
# running the real Minds Electron app (dev mode: it spawns its own backend internally -- on a port
# of its choosing -- and inherits this environment, including the box's Modal env). Everything
# talks to it either through the desktop (humans) or from inside the container (the launch CLI,
# which discovers the backend port by probing the container's own listeners).
echo ">> desktop: Xvfb + noVNC + Minds (Electron)"
export DISPLAY=:99
Xvfb :99 -screen 0 "${DESKTOP_RESOLUTION:-1920x1080x24}" -nolisten tcp &
sleep 1
openbox &
# -defer/-wait bound the framebuffer poll/encode rate (~25fps) -- the encoder is the main
# idle-view CPU cost under software rendering.
x11vnc -display :99 -forever -shared -nopw -quiet -listen localhost -rfbport 5900 -defer 40 -wait 40 &
websockify --web=/usr/share/novnc 0.0.0.0:6080 localhost:5900 &
dbus-uuidgen --ensure
cd /work/mngr/apps/minds
export MINDS_DISABLE_CRASHPAD=1
export ELECTRON_DISABLE_SECURITY_WARNINGS=1
# --no-sandbox: no setuid/userns sandbox in the container (we run as root).
# --disable-gpu: no GPU; software rendering (SwiftShader) is fine for the Minds UI.
exec dbus-run-session -- ./node_modules/.bin/electron . \
     --no-sandbox --disable-gpu --disable-dev-shm-usage
