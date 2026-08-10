import logging

from django.core.cache import caches
from django.utils import timezone

from core.models import AsyncJob
from core.services.utils import output_exception, output_result_success

logger = logging.getLogger(__name__)

cache = caches["default"]

CACHE_KEY_PREFIX = "async_job_progress_"
CACHE_TTL_SECONDS = 24 * 3600


def progress_cache_key(job_id):
    return f"{CACHE_KEY_PREFIX}{job_id}"


class ProgressReporter:
    """
    Single-writer progress handle injected into async job code, so pipeline
    logic never touches job persistence directly.

    Exactly one execution owns a job at a time, so counters are kept in memory
    and written as absolute values — no atomic JSON increments needed. Every
    write is a targeted AsyncJob.objects.filter(id=...).update(...) plus a
    cache snapshot; the cache is an optimization only (the default LocMemCache
    is per-process) and the DB row stays authoritative.
    """

    def __init__(self, job):
        self.job = job
        self._total = job.total
        self._processed = job.processed or 0
        self._metrics = dict(job.metrics or {})

    def start(self):
        """RUNNING transition; no-op unless the job is still RECEIVED/QUEUED,
        which guards against scheduler misfire replays."""
        now = timezone.now()
        updated = AsyncJob.objects.filter(
            id=self.job.id,
            status__in=[AsyncJob.Status.RECEIVED, AsyncJob.Status.QUEUED],
        ).update(status=AsyncJob.Status.RUNNING, started_at=now, updated_at=now)
        if updated:
            self._snapshot(status=AsyncJob.Status.RUNNING)
        return updated > 0

    def set_total(self, total):
        self._total = total
        self._write(total=total)

    def advance(self, k=1, **metrics):
        self._processed += k
        for name, increment in metrics.items():
            self._metrics[name] = self._metrics.get(name, 0) + increment
        fields = {"processed": self._processed}
        if metrics:
            fields["metrics"] = self._metrics
        self._write(**fields)

    def message(self, text):
        self._write(message=text)

    def succeed(self, result=None):
        self._finish(AsyncJob.Status.SUCCESS, result=result)

    def partial(self, result=None, error=None):
        self._finish(AsyncJob.Status.PARTIAL, result=result, error=error)

    def fail(self, error):
        self._finish(AsyncJob.Status.FAILED, error=str(error))

    def _finish(self, status, result=None, error=None):
        fields = {"status": status, "finished_at": timezone.now()}
        if result is not None:
            fields["result"] = result
        if error is not None:
            fields["error"] = error
        self._write(**fields)

    def _write(self, **fields):
        fields.setdefault("updated_at", timezone.now())
        AsyncJob.objects.filter(id=self.job.id).update(**fields)
        self._snapshot(status=fields.get("status"))

    def _snapshot(self, status=None):
        cache.set(
            progress_cache_key(self.job.id),
            {
                "status": str(status) if status else None,
                "total": self._total,
                "processed": self._processed,
                "metrics": self._metrics,
            },
            CACHE_TTL_SECONDS,
        )


def update_progress(
    user, job_uuid, processed=None, total=None, message=None, metrics=None
):
    """
    Authenticated entry point for externally-executed work (e.g. openFN/
    Lightning workflows) to report progress into a job it does not run
    in-process. Only the job's initiator or a superuser may report; terminal
    jobs are never updated. Metric values are increments, merged additively
    into the stored counters.
    """
    try:
        job = AsyncJob.objects.filter(id=job_uuid).first()
        if job is None:
            raise ValueError(f"AsyncJob {job_uuid} does not exist")
        is_owner = user is not None and job.user_id == getattr(user, "id", None)
        if not (is_owner or getattr(user, "is_superuser", False)):
            raise PermissionError("Only the job initiator or a superuser can report progress")
        if job.is_terminal:
            raise ValueError(f"AsyncJob {job_uuid} is already {job.status}")

        fields = {"updated_at": timezone.now()}
        if processed is not None:
            fields["processed"] = processed
        if total is not None:
            fields["total"] = total
        if message is not None:
            fields["message"] = message
        if metrics:
            merged = dict(job.metrics or {})
            for name, increment in metrics.items():
                merged[name] = merged.get(name, 0) + increment
            fields["metrics"] = merged
        AsyncJob.objects.filter(id=job.id).update(**fields)
        cache.delete(progress_cache_key(job.id))

        job.refresh_from_db()
        return output_result_success(
            {
                "uuid": str(job.id),
                "status": job.status,
                "total": job.total,
                "processed": job.processed,
                "metrics": job.metrics,
                "message": job.message,
            }
        )
    except Exception as exc:
        return output_exception("AsyncJob", "update_progress", exc)
