from celery import Celery
from celery.schedules import crontab

from dms.config import settings


celery_app = Celery(
    "dms",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.imports = ("dms.tasks",)
celery_app.conf.task_default_queue = "dms-default"
celery_app.conf.task_routes = {"dms.tasks.*": {"queue": "dms-default"}}
celery_app.conf.beat_schedule = {
    "cleanup-stale-uploads-hourly": {
        "task": "dms.tasks.cleanup_stale_uploads",
        "schedule": crontab(minute=0, hour="*"),
    },
    "daily-integrity-check": {
        "task": "dms.tasks.daily_integrity_check",
        "schedule": crontab(minute=30, hour=2),
    },
}
