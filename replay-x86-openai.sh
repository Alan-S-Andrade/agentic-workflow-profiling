#!/usr/bin/env bash
# Replay the two GPT Researcher traces and the instrumented go-redis SWE run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
source ~/.bashrc
: "${OPENAI_API_KEY:?Set OPENAI_API_KEY in ~/.bashrc before replaying.}"

export PROFILE_PROXY_UPSTREAM=https://api.openai.com
export WORKFLOW_MODEL=gpt-5.6-luna

WORKFLOW_RUN_TIMEOUT=1800 ./profile-run.sh gpt-researcher "$(cat prompt_for_gpt_researcher.txt)"
WORKFLOW_RUN_TIMEOUT=1800 ./profile-run.sh gpt-researcher "$(cat prompt_for_gpt_researcher_diverse.txt)"

target="$(./prepare-swe-go-redis-target.sh)"
SWE_TARGET="$target" PROFILE_PROCESS_TRACE=1 WORKFLOW_RUN_TIMEOUT=3600 \
  ./profile-run.sh swe-agent "$(cat prompt_for_swe_agent_go_redis.txt)"
