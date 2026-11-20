#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
export HARNESS_DOCKER_TESTS=1
exec .venv/bin/python -m pytest test_docker_integration.py -q "$@"
