#!/usr/bin/env bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export DOCKER_CONTEXT="${DOCKER_CONTEXT:-colima}"

cd "$(dirname "$0")/.."

if ! docker info >/dev/null 2>&1; then
  colima start --runtime docker --vm-type vz --cpu 1 --memory 2 --disk 10
fi

for _ in {1..90}; do
  if docker info >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

docker info >/dev/null
docker-compose --env-file var/freshrss/.env -f deploy/freshrss.compose.yml up -d --remove-orphans
