#!/usr/bin/env bash
# Create an isolated target because SWE-agent's LocalDeployment resets it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="$ROOT/targets/go-redis-$STAMP"
mkdir -p "$ROOT/targets"
git clone --depth 1 https://github.com/redis/go-redis.git "$TARGET"
printf '%s\n' "$TARGET"
