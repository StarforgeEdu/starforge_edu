#!/usr/bin/env bash
set -euo pipefail

case "${1:-web}" in
  web)
    # iCal feeds carry a signed credential in the URL path. Keep useful request
    # telemetry without logging request targets/query strings or bearer material.
    exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3 \
      --no-control-socket \
      --access-logfile - --access-logformat '%(h)s %(m)s %(s)s %(L)s'
    ;;
  daphne)
    exec daphne -b 0.0.0.0 -p 8001 config.asgi:application
    ;;
  worker)
    exec celery -A config worker --loglevel=info --concurrency=4
    ;;
  beat)
    exec celery -A config beat --loglevel=info --scheduler django_celery_beat.schedulers:DatabaseScheduler
    ;;
  migrate)
    # Migrate the public schema (shared apps) AND every tenant schema. Bare
    # migrate_schemas does both; running --shared first surfaces shared-app
    # failures with a clearer error before tenant migrations fan out (TD-17).
    python manage.py migrate_schemas --shared
    exec python manage.py migrate_schemas --tenant
    ;;
  shell)
    exec /bin/bash
    ;;
  *)
    exec "$@"
    ;;
esac
