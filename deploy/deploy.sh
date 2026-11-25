#!/usr/bin/env bash
# Run on the server (by GitHub Actions over SSH, or manually): update to origin/main and restart.
set -euo pipefail
cd /home/ubuntu/invest-bot
git fetch --quiet origin main
git checkout -q -B main origin/main   # also moves off any feature branch
docker compose --profile prod up -d --build --remove-orphans
docker image prune -f >/dev/null
sleep 5
docker compose ps --format '{{.Name}} {{.Status}}'
