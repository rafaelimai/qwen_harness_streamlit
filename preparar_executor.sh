#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
docker info >/dev/null
exec docker build -t "${HARNESS_RUNNER_IMAGE:-qwen-harness-runner:1}" sandbox/
