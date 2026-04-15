#!/usr/bin/env bash
# Source this file to export GH_TOKEN from config/gh-token for interactive use.
# launchd jobs can either source it or rely on the Python entrypoints, which load
# the same file automatically.

set -euo pipefail

DEV_ROOT="${DEV_ROOT:-/Volumes/devssd}"
TOKEN_FILE="${DEV_ROOT}/orchestrator/config/gh-token"

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
