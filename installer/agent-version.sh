#!/usr/bin/env bash
# The agent installers' version, apart from the app's (pyproject.toml):
# <installer/AGENT_VERSION>.<commits that changed what the installers ship>, one
# more while those files have uncommitted changes. AGENT_VERSION=x.y.z overrides it.
# It must keep growing past every version already installed (2.3.1 was the last
# one taken from pyproject.toml): an MSI refuses to downgrade.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
if [ -n "${AGENT_VERSION:-}" ]; then
    echo "$AGENT_VERSION"
    exit
fi
paths=(src chrome_engine installer requirements.txt)
n=$(git -C "$root" rev-list --count HEAD -- "${paths[@]}")
[ -z "$(git -C "$root" status --porcelain -- "${paths[@]}")" ] || n=$((n + 1))
echo "$(cat "$root/installer/AGENT_VERSION").$n"
