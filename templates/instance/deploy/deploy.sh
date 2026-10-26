#!/usr/bin/env bash
# Deploy one CI-built image to this VM; roll back if it isn't healthy.
#
#   sudo deploy/deploy.sh <git-sha> <registry/image@sha256:digest>
#
# Run by CI over SSH from the instance directory (MEYDAN_INSTANCE_DIR, default: repo root).
# Also fine by hand, e.g. to roll back:
#   sudo deploy/deploy.sh "$(git rev-parse HEAD)" "$(cat .deploy/previous)"
#
# The image is pinned in compose.override.yaml (gitignored), so a plain
# `docker compose up -d` on the VM keeps running the deployed image instead of building.
set -euo pipefail

SHA=${1:?git sha}
IMAGE=${2:?image@digest}
REGISTRY=${IMAGE%%/*}
cd "${MEYDAN_INSTANCE_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
mkdir -p .deploy

log() { echo "[deploy] $*"; }

# Config files (compose.yaml, Caddyfile, web/) come from git at the same commit as the image.
git fetch --quiet origin main
git reset --quiet --hard "$SHA"

# Registry login with the VM's own service account: no stored credentials.
curl -fsS -H "Metadata-Flavor: Google" \
  "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" |
  python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])" |
  docker login -u oauth2accesstoken --password-stdin "https://$REGISTRY" >/dev/null

pin() {
  printf 'services:\n  app:\n    image: %s\n' "$1" > compose.override.yaml
}

previous=$(cat .deploy/current 2>/dev/null || true)

log "pulling $IMAGE"
pin "$IMAGE"
docker compose pull --quiet app

log "starting"
if docker compose up -d --no-build --remove-orphans --wait --wait-timeout 120; then
  [ -n "$previous" ] && [ "$previous" != "$IMAGE" ] && echo "$previous" > .deploy/previous
  echo "$IMAGE" > .deploy/current
  docker image prune -f >/dev/null
  log "healthy: $SHA"
  exit 0
fi

log "NOT healthy, app logs:"
docker compose logs --tail 50 app || true
if [ -n "$previous" ]; then
  log "rolling back to $previous"
  pin "$previous"
  docker compose up -d --no-build --wait --wait-timeout 120 app
fi
exit 1
