from django.test import TestCase
from django.utils import timezone

from core.models import AsyncJob
from core.test_helpers import create_test_interactive_user


class AsyncJobModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_interactive_user(username="async_job_tester")

    def _create_job(self, **kwargs):
        defaults = dict(
            module="msr_etl",
            job_type="ubr_individuals_import",
            task="msr_etl.jobs.run_ubr_individuals_import",
            user=self.user,
            params={"district": "101"},
        )
        defaults.update(kwargs)
        return AsyncJob.objects.create(**defaults)

    def test_defaults(self):
        job = self._create_job()
        self.assertEqual(job.status, AsyncJob.Status.RECEIVED)
        self.assertEqual(job.processed, 0)
        self.assertIsNone(job.total)
        self.assertIsNone(job.percent)
        self.assertFalse(job.is_terminal)
        self.assertIsNotNone(job.created_at)
        self.assertIsNone(job.started_at)
        self.assertIsNone(job.finished_at)

    def test_percent_derived_not_stored(self):
        job = self._create_job(total=22, processed=11)
        self.assertEqual(job.percent, 50)
        job.processed = 44
        self.assertEqual(job.percent, 100)

    def test_targeted_update_transition(self):
        job = self._create_job()
        now = timezone.now()
        AsyncJob.objects.filter(id=job.id).update(
            status=AsyncJob.Status.RUNNING, started_at=now, updated_at=now
        )
        job.refresh_from_db()
        self.assertEqual(job.status, AsyncJob.Status.RUNNING)
        self.assertIsNotNone(job.started_at)

    def test_terminal_statuses(self):
        for status in AsyncJob.TERMINAL_STATUSES:
            job = self._create_job(status=status)
            self.assertTrue(job.is_terminal)
        for status in (
            AsyncJob.Status.RECEIVED,
            AsyncJob.Status.QUEUED,
            AsyncJob.Status.RUNNING,
        ):
            job = self._create_job(status=status)
            self.assertFalse(job.is_terminal)

    def test_lookup_by_client_mutation_id(self):
        self._create_job(client_mutation_id="cmid-123")
        self._create_job(client_mutation_id="cmid-456")
        self.assertEqual(
            AsyncJob.objects.filter(client_mutation_id="cmid-123").count(), 1
        )

    def test_json_fields_round_trip(self):
        job = self._create_job(
            metrics={"staged": 3, "synced": 1, "errors": 0},
            result={"note": "done"},
        )
        job.refresh_from_db()
        self.assertEqual(job.params, {"district": "101"})
        self.assertEqual(job.metrics["staged"], 3)
        self.assertEqual(job.result["note"], "done")
