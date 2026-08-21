#!/usr/bin/env bash
set -euo pipefail

case "${1:-web}" in
  web)
    gunicorn_timeout="${GUNICORN_TIMEOUT_SECONDS:-60}"
    gunicorn_graceful_timeout="${GUNICORN_GRACEFUL_TIMEOUT_SECONDS:-75}"
    [[ "$gunicorn_timeout" =~ ^[1-9][0-9]{0,3}$ && \
       "$gunicorn_graceful_timeout" =~ ^[1-9][0-9]{0,3}$ ]] || {
      echo "Gunicorn timeout values must be positive integer seconds." >&2
      exit 78
    }
    (( gunicorn_graceful_timeout >= gunicorn_timeout + 15 )) || {
      echo "Gunicorn graceful timeout must exceed the request timeout by at least 15 seconds." >&2
      exit 78
    }
    # iCal feeds carry a signed credential in the URL path. Keep useful request
    # telemetry without logging request targets/query strings or bearer material.
    exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 \
      --workers "${WEB_CONCURRENCY:-2}" --timeout "$gunicorn_timeout" \
      --graceful-timeout "$gunicorn_graceful_timeout" \
      --no-control-socket \
      --access-logfile - --access-logformat '%(h)s %(m)s %(s)s %(L)s'
    ;;
  daphne)
    # Bound handshake time, connection lifetime, unauthenticated frame/message
    # allocation, and application shutdown. Access logs are disabled here because
    # request targets can contain attacker-supplied secrets; Caddy and structured
    # application logs retain the safe operational telemetry.
    exec daphne -b 0.0.0.0 -p 8001 \
      --proxy-headers \
      --websocket_connect_timeout "${DAPHNE_WS_CONNECT_TIMEOUT_SECONDS:-10}" \
      --websocket_timeout "${DAPHNE_WS_MAX_LIFETIME_SECONDS:-28800}" \
      --websocket-max-message-size "${DAPHNE_WS_MAX_MESSAGE_BYTES:-65536}" \
      --websocket-max-frame-size "${DAPHNE_WS_MAX_FRAME_BYTES:-65536}" \
      --application-close-timeout "${DAPHNE_APPLICATION_CLOSE_TIMEOUT_SECONDS:-5}" \
      --access-log /dev/null \
      config.asgi:application
    ;;
  worker)
    worker_args=(
      celery -A config worker
      --loglevel="${CELERY_LOG_LEVEL:-info}"
      --concurrency="${CELERY_WORKER_CONCURRENCY:-2}"
      --prefetch-multiplier=1
    )
    if [[ -n "${CELERY_QUEUES:-}" ]]; then
      worker_args+=(--queues="${CELERY_QUEUES}")
    fi
    exec "${worker_args[@]}"
    ;;
  beat)
    exec celery -A config beat --loglevel=info --scheduler django_celery_beat.schedulers:DatabaseScheduler
    ;;
  migrate)
    release_revision="${STARFORGE_RELEASE_REVISION:-}"
    image_revision="${STARFORGE_IMAGE_REVISION:-}"
    if [[ "$image_revision" =~ ^[0-9a-f]{40}$ || \
          "${DJANGO_SETTINGS_MODULE:-}" == "config.settings.production" ]]; then
      if [[ ! "$release_revision" =~ ^[0-9a-f]{40}$ || \
            "$release_revision" != "$image_revision" ]]; then
        echo "Production migration revision does not match the immutable image." >&2
        exit 78
      fi
      if [[ "${STARFORGE_EMPTY_DATABASE_BOOTSTRAP:-}" == "$image_revision" && \
            "$release_revision" == "$image_revision" ]]; then
        [[ "$(python manage.py check_empty_production_database --token)" == "empty" ]] || exit 78
      else
        if [[ "${STARFORGE_MAINTENANCE_CUTOVER:-}" != "$release_revision" ]]; then
          echo "Production migrations require exact host-issued cutover evidence." >&2
          exit 78
        fi
        python -m core.migration_gate \
          --evidence /run/secrets/migration-cutover.evidence \
          --revision "$release_revision" \
          --image-revision "$image_revision" \
          --candidate-image-id "${STARFORGE_CANDIDATE_IMAGE_ID:-}" \
          --helpers-sha256 "${STARFORGE_RELEASE_HELPERS_SHA256:-}" || exit 78
      fi
    fi
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
