#!/bin/zsh
# Usage: ./agent.sh <command>   (setup | discover | triage | run | status | answer)
cd "$(dirname "$0")" && exec .venv/bin/python -m agent "$@"
