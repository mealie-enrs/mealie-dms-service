#!/usr/bin/env sh
set -eu

ROLE="${DMS_ROLE:-api}"

case "$ROLE" in
  api)
    exec uvicorn dms.main:app --host 0.0.0.0 --port 8000
    ;;
  worker)
    exec celery -A dms.celery_app:celery_app worker --loglevel=INFO
    ;;
  scheduler)
    exec celery -A dms.celery_app:celery_app beat --loglevel=INFO
    ;;
  *)
    echo "Unknown DMS_ROLE=$ROLE; expected one of: api, worker, scheduler"
    exit 1
    ;;
esac
