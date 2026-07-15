# Minds-box: a full Minds computer for a given mngr branch at an EXACT SHA, with the
# mngr_minds_eval app overlaid so any mngr branch can run the harness. One image, two run modes
# (chosen by BOX_MODE at `docker run`; see entrypoint.sh):
#   headless -- `minds run` serving the Minds HTTP API (used by `launch` to create workspaces)
#   desktop  -- the real Minds Electron app on Xvfb, streamed to the browser via x11vnc + noVNC
#               (used by `visit-batch`: you enter the desktop and use Minds natively)
FROM node:24.15.0-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv

# Base tooling + the desktop stack: Xvfb (virtual display), openbox (window manager), x11vnc +
# noVNC/websockify (browser access), dbus (Electron needs a session bus), and the Chromium/Electron
# runtime libraries. fonts-liberation so text renders.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates build-essential curl rsync openssh-client \
      xvfb openbox x11vnc novnc websockify dbus dbus-x11 xterm fonts-liberation \
      libgtk-3-0 libnss3 libasound2 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libgbm1 \
      libxss1 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libxkbcommon0 libatspi2.0-0 \
 && rm -rf /var/lib/apt/lists/* \
 && corepack enable

ARG MNGR_REPO=https://github.com/imbue-ai/mngr.git
ARG MNGR_BRANCH=main
# MNGR_REF = the exact SHA to check out (resolved on the host: a branch tip, or the pinned commit a
# batch ran on for visit-batch). We shallow-fetch that exact commit, so the box IS that SHA -- not
# merely the tip at build time. The SHA is also the layer cache key, so a different SHA rebuilds.
ARG MNGR_REF=unset
RUN echo "mngr ${MNGR_BRANCH} @ ${MNGR_REF}" \
 && git init /work/mngr \
 && git -C /work/mngr remote add origin "${MNGR_REPO}" \
 && git -C /work/mngr fetch --depth 1 origin "${MNGR_REF}" \
 && git -C /work/mngr checkout FETCH_HEAD

# Overlay the eval app from THIS checkout onto the cloned mngr, so the harness is present even on
# an mngr branch that doesn't carry it (and is always this repo's version).
RUN rm -rf /work/mngr/apps/mngr_minds_eval
COPY imbue /work/mngr/apps/mngr_minds_eval/imbue
COPY pyproject.toml README.md /work/mngr/apps/mngr_minds_eval/

WORKDIR /work/mngr
RUN uv sync --all-packages

# node deps (electron binary via the allowlisted postinstall, latchkey) + compiled CSS; pin pnpm
# from the branch's manifest. ensure-binaries downloads the bundled binaries (restic/git/lima/uv)
# that the app's dev mode expects present.
RUN cd apps/minds \
 && PNPM_VER="$(node -p "let p=require('./package.json'); (p.packageManager&&p.packageManager.split('@')[1])||(p.engines&&p.engines.pnpm)||'10.33.4'")" \
 && corepack prepare "pnpm@${PNPM_VER}" --activate \
 && pnpm install \
 && (pnpm run build:css || echo "build:css failed (non-fatal)") \
 && (node scripts/ensure-binaries.js || echo "ensure-binaries failed (non-fatal)")

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
