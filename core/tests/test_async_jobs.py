from unittest import mock

from django.test import TestCase
from django.utils import timezone

from core.models import AsyncJob
from core.services import (
    ProgressReporter,
    execute_async_job,
    run_as_celery_job,
    run_as_scheduled_job,
    update_progress,
)
from core.services.asyncJobServices import cache, progress_cache_key
from core.test_helpers import create_test_interactive_user, create_test_role


def _passing_worker(reporter, **params):
    reporter.set_total(2)
    reporter.advance(staged=1)
    reporter.advance(synced=1)


def _partial_worker(reporter, **params):
    reporter.partial(error="1 unit failed")


def _failing_worker(reporter, **params):
    raise RuntimeError("upstream exploded")


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


class ProgressReporterTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_interactive_user(username="reporter_tester")

    def setUp(self):
        self.job = AsyncJob.objects.create(
            module="msr_etl",
            job_type="ubr_individuals_import",
            task="msr_etl.jobs.run_ubr_individuals_import",
            user=self.user,
        )
        self.reporter = ProgressReporter(self.job)

    def test_start_transitions_once(self):
        self.assertTrue(self.reporter.start())
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, AsyncJob.Status.RUNNING)
        self.assertIsNotNone(self.job.started_at)
        # misfire replay guard: second start is a no-op
        self.assertFalse(self.reporter.start())

    def test_set_total_and_advance_write_absolute_values(self):
        self.reporter.set_total(10)
        self.reporter.advance()
        self.reporter.advance(k=2, staged=2)
        self.reporter.advance(staged=1, errors=1)
        self.job.refresh_from_db()
        self.assertEqual(self.job.total, 10)
        self.assertEqual(self.job.processed, 4)
        self.assertEqual(self.job.metrics, {"staged": 3, "errors": 1})

    def test_message(self):
        self.reporter.message("Processing chunk 3 of 11")
        self.job.refresh_from_db()
        self.assertEqual(self.job.message, "Processing chunk 3 of 11")

    def test_succeed(self):
        self.reporter.succeed(result={"rows": 42})
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, AsyncJob.Status.SUCCESS)
        self.assertEqual(self.job.result, {"rows": 42})
        self.assertIsNotNone(self.job.finished_at)

    def test_partial(self):
        self.reporter.partial(error="2 units failed")
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, AsyncJob.Status.PARTIAL)
        self.assertEqual(self.job.error, "2 units failed")

    def test_fail(self):
        self.reporter.fail(RuntimeError("upstream timeout"))
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, AsyncJob.Status.FAILED)
        self.assertEqual(self.job.error, "upstream timeout")

    def test_updated_at_set_on_update(self):
        before = self.job.updated_at
        self.reporter.advance()
        self.job.refresh_from_db()
        self.assertGreater(self.job.updated_at, before)

    def test_cache_snapshot_written(self):
        self.reporter.set_total(5)
        self.reporter.advance(k=3, synced=3)
        snapshot = cache.get(progress_cache_key(self.job.id))
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["total"], 5)
        self.assertEqual(snapshot["processed"], 3)
        self.assertEqual(snapshot["metrics"], {"synced": 3})

    def test_resumes_counters_from_job_row(self):
        AsyncJob.objects.filter(id=self.job.id).update(
            processed=7, metrics={"synced": 7}
        )
        self.job.refresh_from_db()
        reporter = ProgressReporter(self.job)
        reporter.advance(synced=1)
        self.job.refresh_from_db()
        self.assertEqual(self.job.processed, 8)
        self.assertEqual(self.job.metrics, {"synced": 8})


class UpdateProgressServiceTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = create_test_interactive_user(username="progress_owner")
        # non-admin role: the default helper role is IMIS admin, which makes
        # the user a superuser and would pass the ownership check
        cls.other = create_test_interactive_user(
            username="progress_other", roles=[create_test_role().id]
        )

    def setUp(self):
        self.job = AsyncJob.objects.create(
            module="msr_etl",
            job_type="ubr_individuals_import",
            task="msr_etl.jobs.run_ubr_individuals_import",
            user=self.owner,
        )

    def test_owner_can_report(self):
        result = update_progress(
            self.owner, self.job.id, processed=5, total=10,
            message="half way", metrics={"synced": 5},
        )
        self.assertTrue(result["success"], result)
        self.job.refresh_from_db()
        self.assertEqual(self.job.processed, 5)
        self.assertEqual(self.job.total, 10)
        self.assertEqual(self.job.message, "half way")
        self.assertEqual(self.job.metrics, {"synced": 5})

    def test_metrics_merge_additively(self):
        update_progress(self.owner, self.job.id, metrics={"synced": 2})
        update_progress(self.owner, self.job.id, metrics={"synced": 3, "errors": 1})
        self.job.refresh_from_db()
        self.assertEqual(self.job.metrics, {"synced": 5, "errors": 1})

    def test_non_owner_denied(self):
        result = update_progress(self.other, self.job.id, processed=1)
        self.assertFalse(result["success"])
        self.job.refresh_from_db()
        self.assertEqual(self.job.processed, 0)

    def test_terminal_job_not_updated(self):
        AsyncJob.objects.filter(id=self.job.id).update(
            status=AsyncJob.Status.SUCCESS
        )
        result = update_progress(self.owner, self.job.id, processed=1)
        self.assertFalse(result["success"])

    def test_missing_job(self):
        import uuid

        result = update_progress(self.owner, uuid.uuid4(), processed=1)
        self.assertFalse(result["success"])


class ExecuteAsyncJobTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_interactive_user(username="execute_tester")

    def _create_job(self, worker, **kwargs):
        defaults = dict(
            module="msr_etl",
            job_type="ubr_individuals_import",
            task=f"{worker.__module__}.{worker.__qualname__}",
            user=self.user,
        )
        defaults.update(kwargs)
        return AsyncJob.objects.create(**defaults)

    def test_runs_worker_and_auto_succeeds(self):
        job = self._create_job(_passing_worker)
        execute_async_job(job.id)
        job.refresh_from_db()
        self.assertEqual(job.status, AsyncJob.Status.SUCCESS)
        self.assertEqual(job.total, 2)
        self.assertEqual(job.processed, 2)
        self.assertEqual(job.metrics, {"staged": 1, "synced": 1})
        self.assertIsNotNone(job.started_at)
        self.assertIsNotNone(job.finished_at)

    def test_worker_terminal_status_respected(self):
        job = self._create_job(_partial_worker)
        execute_async_job(job.id)
        job.refresh_from_db()
        self.assertEqual(job.status, AsyncJob.Status.PARTIAL)
        self.assertEqual(job.error, "1 unit failed")

    def test_worker_exception_marks_failed(self):
        job = self._create_job(_failing_worker)
        execute_async_job(job.id)
        job.refresh_from_db()
        self.assertEqual(job.status, AsyncJob.Status.FAILED)
        self.assertEqual(job.error, "upstream exploded")

    def test_misfire_replay_is_noop(self):
        job = self._create_job(_passing_worker, status=AsyncJob.Status.SUCCESS)
        execute_async_job(job.id)
        job.refresh_from_db()
        self.assertEqual(job.processed, 0)

    def test_missing_job_does_not_raise(self):
        import uuid

        execute_async_job(uuid.uuid4())


class DispatchTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_interactive_user(username="dispatch_tester")

    def test_run_as_scheduled_job_without_live_scheduler(self):
        from django_apscheduler.models import DjangoJob

        job_uuid = run_as_scheduled_job(
            _passing_worker,
            module="msr_etl",
            job_type="ubr_individuals_import",
            user=self.user,
            params={"district": "101"},
            client_mutation_id="cm-42",
        )
        job = AsyncJob.objects.get(id=job_uuid)
        self.assertEqual(job.status, AsyncJob.Status.QUEUED)
        self.assertEqual(
            job.task,
            "core.tests.test_async_jobs._passing_worker",
        )
        self.assertEqual(job.params, {"district": "101"})
        self.assertEqual(job.client_mutation_id, "cm-42")
        # the DjangoJobStore handoff persisted the one-off job for the
        # dedicated scheduler process to pick up
        self.assertTrue(
            DjangoJob.objects.filter(id=f"async_job_{job_uuid}").exists()
        )

    def test_run_as_celery_job_queues_task(self):
        with mock.patch("core.tasks.execute_async_job_task") as task_mock:
            job_uuid = run_as_celery_job(
                _passing_worker,
                module="msr_etl",
                job_type="ubr_individuals_import",
                user=self.user,
            )
        task_mock.delay.assert_called_once_with(str(job_uuid))
        job = AsyncJob.objects.get(id=job_uuid)
        self.assertEqual(job.status, AsyncJob.Status.QUEUED)

    def test_dotted_path_accepted(self):
        with mock.patch("core.tasks.execute_async_job_task"):
            job_uuid = run_as_celery_job(
                "core.tests.test_async_jobs._passing_worker",
                module="msr_etl",
                job_type="ubr_individuals_import",
                user=self.user,
            )
        job = AsyncJob.objects.get(id=job_uuid)
        self.assertEqual(
            job.task, "core.tests.test_async_jobs._passing_worker"
        )
