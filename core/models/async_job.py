from django.db import models

from . import UUIDModel, ExtendableModel
from .user import User


class AsyncJob(UUIDModel, ExtendableModel):
    """
    Generic handle for any long-running background job, with live progress.
    Domain-free: no foreign keys to business entities. Module-specific data
    goes in params/metrics/result; modules may keep child tables keyed to uuid.

    Worker-side transitions must not use save() — a stale instance would
    override concurrent changes. Use targeted
    AsyncJob.objects.filter(id=...).update(...) with an explicit updated_at,
    since auto_now does not fire on update().
    """

    class Status(models.TextChoices):
        RECEIVED = "RECEIVED", "Received"
        QUEUED = "QUEUED", "Queued"
        RUNNING = "RUNNING", "Running"
        SUCCESS = "SUCCESS", "Success"
        PARTIAL = "PARTIAL", "Partial"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"

    TERMINAL_STATUSES = (
        Status.SUCCESS,
        Status.PARTIAL,
        Status.FAILED,
        Status.CANCELLED,
    )

    module = models.CharField(max_length=80)
    job_type = models.CharField(max_length=120)
    task = models.CharField(max_length=255)
    client_mutation_id = models.CharField(
        max_length=255, blank=True, null=True, db_index=True
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.RECEIVED
    )
    total = models.IntegerField(null=True, blank=True)
    processed = models.IntegerField(default=0)
    message = models.TextField(blank=True, null=True)
    error = models.TextField(blank=True, null=True)
    user = models.ForeignKey(
        User,
        on_delete=models.DO_NOTHING,
        blank=True,
        null=True,
        related_name="async_jobs",
    )
    params = models.JSONField(null=True, blank=True)
    metrics = models.JSONField(null=True, blank=True)
    result = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = True
        db_table = "core_AsyncJob"

    @property
    def is_terminal(self):
        return self.status in self.TERMINAL_STATUSES

    @property
    def percent(self):
        if not self.total:
            return None
        return min(100, round(100 * self.processed / self.total))

    def __str__(self):
        return f"{self.module}.{self.job_type} [{self.id}] {self.status}"
