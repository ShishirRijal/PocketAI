#!/bin/sh
# migrations at container start (§11), then whatever role this container plays
set -e
case "${1:-api}" in
  api)
    pocket migrate
    exec uvicorn pocket.main:create_app --factory --host 0.0.0.0 --port 8080 --proxy-headers --forwarded-allow-ips='*'
    ;;
  worker)
    # the api container owns migrations; give it a moment on first boot
    sleep 3
    exec pocket worker
    ;;
  *)
    exec "$@"
    ;;
esac
