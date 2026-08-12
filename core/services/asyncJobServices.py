import logging
from copy import deepcopy

from django.apps import apps as django_apps
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.models import AsyncJob
from core.services.utils import output_exception, output_result_success
from core.utils import get_scheduler_method_ref, set_current_user

logger = logging.getLogger(__name__)


class ProgressReporter:
    """
    Single-writer progress handle injected into async job code. Counters are
    kept in memory and written as absolute values; the job row is the source
    of truth.
    """

    def __init__(self, job):
        self.job = job
        self._total = job.total
        self._processed = job.processed or 0
        self._metrics = dict(job.metrics or {})

    def start(self):
        """Transition to RUNNING; no-op unless still RECEIVED/QUEUED (misfire guard)."""
        now = timezone.now()
        updated = AsyncJob.objects.filter(
            id=self.job.id,
            status__in=[AsyncJob.Status.RECEIVED, AsyncJob.Status.QUEUED],
        ).update(status=AsyncJob.Status.RUNNING, started_at=now, updated_at=now)
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
        """Guarded like start(): never overwrites an already-terminal job."""
        fields = {"status": status, "finished_at": timezone.now(), "updated_at": timezone.now()}
        if result is not None:
            fields["result"] = result
        if error is not None:
            fields["error"] = error
        AsyncJob.objects.filter(id=self.job.id).exclude(status__in=AsyncJob.TERMINAL_STATUSES).update(**fields)

    def _write(self, **fields):
        fields.setdefault("updated_at", timezone.now())
        AsyncJob.objects.filter(id=self.job.id).update(**fields)


def update_progress(
    user, job_uuid, processed=None, total=None, message=None, metrics=None
):
    """
    Entry point for externally-executed work (e.g. openFN workflows) to report
    into a job. Initiator or superuser only; terminal jobs are never updated;
    metric values are increments.
    """
    try:
        # locked to make the metrics merge below a real read-modify-write
        with transaction.atomic():
            job = AsyncJob.objects.select_for_update().filter(id=job_uuid).first()
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


def execute_async_job(job_uuid):
    """
    Worker entry point: re-loads the job by uuid, no-ops unless still
    RECEIVED/QUEUED, and runs job.task with a ProgressReporter injected.
    """
    job = AsyncJob.objects.filter(id=job_uuid).first()
    if job is None:
        logger.warning("AsyncJob %s does not exist, nothing to execute", job_uuid)
        return
    reporter = ProgressReporter(job)
    if not reporter.start():
        logger.info(
            "AsyncJob %s is already %s, skipping execution (misfire replay guard)",
            job.id,
            job.status,
        )
        return
    if job.user_id:
        set_current_user(job.user)
    try:
        task_fn = get_scheduler_method_ref(job.task)
        task_fn(reporter=reporter, **(job.params or {}))
        job.refresh_from_db()
        if not job.is_terminal:
            reporter.succeed()
    except Exception as exc:
        logger.exception(
            "AsyncJob %s (%s.%s) failed", job.id, job.module, job.job_type
        )
        job.refresh_from_db()
        if not job.is_terminal:
            reporter.fail(exc)


def _task_path(fn):
    if callable(fn):
        return f"{fn.__module__}.{fn.__qualname__}"
    return fn


def _create_job(fn, module, job_type, user, params, client_mutation_id):
    return AsyncJob.objects.create(
        module=module,
        job_type=job_type,
        task=_task_path(fn),
        user=user if getattr(user, "id", None) else None,
        params=params or {},
        client_mutation_id=client_mutation_id,
    )


def _mark_queued(job):
    AsyncJob.objects.filter(id=job.id, status=AsyncJob.Status.RECEIVED).update(
        status=AsyncJob.Status.QUEUED, updated_at=timezone.now()
    )


def _get_live_scheduler():
    from core.scheduler import scheduler as core_scheduler

    if core_scheduler.running:
        return core_scheduler
    try:
        runner = django_apps.get_app_config("apscheduler_runner")
        runner_scheduler = getattr(runner, "scheduler", None)
        if runner_scheduler is not None and runner_scheduler.running:
            return runner_scheduler
    except LookupError:
        pass
    return None


def _persist_to_jobstore(**job_kwargs):
    """
    Persist a one-off job into the DB-backed store via a throwaway paused
    scheduler; the dedicated scheduler process (manage.py runapscheduler)
    picks it up.
    """
    from apscheduler.schedulers.background import BackgroundScheduler

    handoff = BackgroundScheduler(deepcopy(settings.SCHEDULER_CONFIG))
    handoff.start(paused=True)
    try:
        handoff.add_job(execute_async_job, **job_kwargs)
    finally:
        # DjangoJobStore.shutdown() closes the shared DB connection; detach first
        handoff.remove_jobstore("default", shutdown=False)
        handoff.shutdown(wait=False)


def run_as_scheduled_job(
    fn, module, job_type, user=None, params=None, client_mutation_id=None
):
    """
    Create an AsyncJob for fn and run it on APScheduler; returns the job uuid
    immediately. fn is a callable or dotted path, invoked as
    fn(reporter=..., **params).
    """
    job = _create_job(fn, module, job_type, user, params, client_mutation_id)
    job_kwargs = {
        "trigger": "date",
        "args": [str(job.id)],
        "id": f"async_job_{job.id}",
        "misfire_grace_time": 86400,
        "replace_existing": True,
    }
    live_scheduler = _get_live_scheduler()
    if live_scheduler is not None:
        live_scheduler.add_job(execute_async_job, **job_kwargs)
    else:
        _persist_to_jobstore(**job_kwargs)
    _mark_queued(job)
    return job.id


def run_as_celery_job(
    fn, module, job_type, user=None, params=None, client_mutation_id=None
):
    """Same contract as run_as_scheduled_job, executed on the Celery worker."""
    job = _create_job(fn, module, job_type, user, params, client_mutation_id)
    from core.tasks import execute_async_job_task

    execute_async_job_task.delay(str(job.id))
    _mark_queued(job)
    return job.id
