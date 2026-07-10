#!/usr/bin/env bash
set -euo pipefail
cd /work/mngr

export SKIP_AUTH=1
# Only Modal is usable in the box; disable every other provider so `mngr list` (which destroy /
# retrieve pipe from) doesn't hard-error on an unreachable provider.
for _p in DOCKER AZURE AWS VULTR LIMA IMBUE_CLOUD GCP OVH; do
  export "MNGR__PROVIDERS__${_p}__IS_ENABLED=false"
done
export MINDS_LATCHKEY_BINARY=/work/mngr/apps/minds/node_modules/.bin/latchkey
# (MNGR__PROVIDERS__MODAL__USER_ID is passed at `docker run` so the box's Modal environment is
#  named after the run -- inherited here by minds and by any later `docker exec`.)

echo ">> activating minds env: ${MINDS_ENV:-staging}"
eval "$(uv run minds env activate "${MINDS_ENV:-staging}")"

BARE="${MINDS_BARE_PORT:-8420}"
echo ">> booting Minds on 0.0.0.0:${BARE} (forward ${MINDS_FORWARD_PORT:-8421}) ..."
exec uv run --package minds minds -vv --format jsonl run \
     --host 0.0.0.0 --port "${BARE}" --no-browser
