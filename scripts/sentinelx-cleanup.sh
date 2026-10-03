#!/usr/bin/env bash
# SentinelX cleanup — Phase 0, authorized by the owner.
# Records full inspect state for rollback, then removes ONLY the container.
# NEVER deletes /opt/sentinelx-worker and NEVER prunes docker resources.
set -euo pipefail

CONTAINER="sentinelx-worker"
AUDIT_DIR="${1:-/opt/pv-growth/audit}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p "$AUDIT_DIR"
INSPECT_OUT="$AUDIT_DIR/sentinelx-worker-inspect-$STAMP.json"

if ! docker inspect "$CONTAINER" > "$INSPECT_OUT" 2>/dev/null; then
  echo "container $CONTAINER not found (already removed?)"
  exit 0
fi

echo "inspect state saved: $INSPECT_OUT"

# capture runtime state too (for rollback reference)
docker inspect "$CONTAINER" --format \
  'image={{.Config.Image}} restart={{.HostConfig.RestartPolicy.Name}} net={{.HostConfig.NetworkMode}}' \
  | tee "$AUDIT_DIR/sentinelx-worker-summary-$STAMP.txt"

if [ "${CONFIRM:-}" != "yes" ]; then
  echo "dry-run only. Re-run with CONFIRM=yes to actually remove."
  exit 0
fi

docker stop "$CONTAINER"
docker rm "$CONTAINER"
echo "removed container $CONTAINER (files under /opt/sentinelx-worker preserved)"
echo "verify neighbors still healthy:"
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'pv-reseller-dashboard|akhbot-app' || true
