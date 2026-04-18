#!/usr/bin/env bash
# Source this file to export GH_TOKEN from config/gh-token for interactive use.
# launchd jobs can either source it or rely on the Python entrypoints, which load
# the same file automatically.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TOKEN_FILE="${STATE_ROOT}/config/gh-token"

if ! [ -f "$TOKEN_FILE" ]; then
  echo "gh-token missing: $TOKEN_FILE" >&2
  return 1 2>/dev/null || exit 1
fi

token="$(tr -d '\r' < "$TOKEN_FILE" | sed -n '1p')"
if [ -z "${token}" ]; then
  echo "gh-token empty: $TOKEN_FILE" >&2
  return 1 2>/dev/null || exit 1
fi

export GH_TOKEN="$token"
export GITHUB_TOKEN="$token"
export GH_HOST="${GH_HOST:-github.com}"
