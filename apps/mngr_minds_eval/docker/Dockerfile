# Minds-box: headless Minds (the CONTROLLER) for a given mngr branch, with the mngr_minds_eval
# eval app overlaid on top so ANY mngr branch can run the harness. The box's /work/mngr is both
# what runs Minds and what gets vendored into eval clones.
FROM node:24.15.0-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates build-essential curl rsync openssh-client \
 && rm -rf /var/lib/apt/lists/* \
 && corepack enable

ARG MNGR_REPO=https://github.com/imbue-ai/mngr.git
ARG MNGR_BRANCH=main
# MNGR_REF = the branch's current remote-tip SHA, resolved on the host and passed in. Referencing
# it in this RUN changes the layer's cache key whenever the branch moves -> ALWAYS a fresh clone
# of the tip (no stale cached checkout).
ARG MNGR_REF=unset
RUN echo "mngr ${MNGR_BRANCH} @ ${MNGR_REF}" \
 && git clone --depth 1 --branch "${MNGR_BRANCH}" "${MNGR_REPO}" /work/mngr

# Overlay the eval app from THIS checkout onto the cloned mngr, so the harness is present even on
# an mngr branch that doesn't carry it (and is always this repo's version).
RUN rm -rf /work/mngr/apps/mngr_minds_eval
COPY imbue /work/mngr/apps/mngr_minds_eval/imbue
COPY pyproject.toml README.md /work/mngr/apps/mngr_minds_eval/

WORKDIR /work/mngr
RUN uv sync --all-packages

# node deps (latchkey binary) + compiled CSS; pin pnpm from the branch's manifest.
RUN cd apps/minds \
 && PNPM_VER="$(node -p "let p=require('./package.json'); (p.packageManager&&p.packageManager.split('@')[1])||(p.engines&&p.engines.pnpm)||'10.33.4'")" \
 && corepack prepare "pnpm@${PNPM_VER}" --activate \
 && pnpm install \
 && (pnpm run build:css || echo "build:css failed (non-fatal)")

# Make the mngr_forward proxy bind an env-driven host/port so workspace views reach the host.
RUN python3 - <<'PY'
import pathlib
p = pathlib.Path("/work/mngr/apps/minds/imbue/minds/desktop_client/forward_cli.py")
try:
    s = p.read_text()
    old = '        "forward",\n        "--host",\n        "127.0.0.1",\n'
    new = ('        "forward",\n        "--host",\n        os.environ.get("MINDS_FORWARD_HOST", "127.0.0.1"),\n'
           '        "--port",\n        os.environ.get("MINDS_FORWARD_PORT", "8421"),\n')
    if old in s:
        p.write_text(s.replace(old, new)); print("patched mngr_forward host/port")
    else:
        print("WARN: forward_cli --host pattern not found; leaving default")
except FileNotFoundError:
    print("WARN: forward_cli.py not found; skipping")
PY

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
