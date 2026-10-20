#!/usr/bin/env bash
# Run as ack on cgate. Git authentication and tests run without sudo.
set -euo pipefail
if (( EUID == 0 )); then
  echo 'Run as ack; sudo is requested only for production checks/install.' >&2
  exit 1
fi
if (( $# != 2 )) || [[ "$1" != check && "$1" != apply ]]; then
  echo 'Usage: bash tools/release.sh check|apply COMMIT_OR_TAG' >&2
  exit 1
fi
cd "$(dirname "$0")/.."
test "$(pwd -P)" = /home/ack/cbus-bridge || {
  echo 'Run from the separate cgate checkout.' >&2; exit 1;
}
git fetch origin main
commit=$(git rev-parse --verify --end-of-options "${2}^{commit}")
git merge-base --is-ancestor "$commit" origin/main
stage=$(mktemp -d /tmp/cbus-release.XXXXXX)
trap 'rm -rf "$stage"' EXIT
git archive "$commit" | tar -x -C "$stage"
test -f "$stage/tools/deploy.py"
(
  cd "$stage"
  /opt/cbus_bridge/.venv/bin/python -B -m unittest discover -s tests -v
)
echo "Release: $commit"
sudo python3 "$stage/tools/deploy.py" "$1" "$stage" --commit "$commit"
