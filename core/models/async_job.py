from django.db import models

from . import UUIDModel, ExtendableModel
from .user import User


class AsyncJob(UUIDModel, ExtendableModel):
    """
    Generic handle for a long-running background job, with live progress.
    Domain-free: module data goes in params/metrics/result, never FKs.
    Transition status via filter(id=...).update(...) with an explicit
    updated_at, never save(). params/result are exposed via GraphQL to the
    job's owner - never put credentials or tokens in them.
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

    def __str__(self):
        return f"{self.module}.{self.job_type} [{self.id}] {self.status}"
