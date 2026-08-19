#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
hub_credentials="${TECHHUB_CREDENTIALS_FILE:-$HOME/tech-hub/credentials.env}"

if [[ ! -r "$hub_credentials" ]]; then
  echo "tech-hub credentials are not readable: $hub_credentials" >&2
  exit 1
fi

set -a
# Existing hub identities; this file remains the single source of RIKKA_TOKEN.
. "$hub_credentials"
. "$project_dir/credentials.env"
set +a

if [[ -z "${RIKKA_TOKEN:-}" ]]; then
  echo "RIKKA_TOKEN is missing from tech-hub credentials" >&2
  exit 1
fi

export TECHHUB_TOKEN="$RIKKA_TOKEN"
exec "$project_dir/.venv/bin/python" "$project_dir/server.py"
