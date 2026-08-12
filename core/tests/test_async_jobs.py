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
        self.assertFalse(job.is_terminal)
        self.assertIsNotNone(job.created_at)
        self.assertIsNone(job.started_at)
        self.assertIsNone(job.finished_at)

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

    def test_finish_does_not_overwrite_an_already_terminal_job(self):
        self.reporter.succeed(result={"rows": 1})
        self.reporter.fail("should not apply")
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, AsyncJob.Status.SUCCESS)
        self.assertIsNone(self.job.error)

    def test_updated_at_set_on_update(self):
        before = self.job.updated_at
        self.reporter.advance()
        self.job.refresh_from_db()
        self.assertGreater(self.job.updated_at, before)

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
        # non-admin role: the default helper role implies superuser
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
        # handoff persisted the one-off job into the shared store
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


class AsyncJobGQLTypeTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        from core.schema import AsyncJobGQLType

        cls.gql_type = AsyncJobGQLType
        cls.owner = create_test_interactive_user(username="gql_owner")
        cls.plain_role = create_test_role()
        cls.other = create_test_interactive_user(
            username="gql_other", roles=[cls.plain_role.id]
        )
        cls.viewer = create_test_interactive_user(
            username="gql_viewer",
            roles=[
                create_test_role(
                    perm_names=["gql_query_async_jobs_perms"],
                    name="AsyncJobViewer",
                ).id
            ],
        )
        cls.own_job = AsyncJob.objects.create(
            module="msr_etl",
            job_type="ubr_individuals_import",
            task="msr_etl.jobs.run_ubr_individuals_import",
            user=cls.owner,
            client_mutation_id="cm-gql-1",
        )
        cls.other_job = AsyncJob.objects.create(
            module="msr_etl",
            job_type="ubr_locations_import",
            task="msr_etl.jobs.run_ubr_locations_import",
            user=cls.other,
        )

    def _scoped(self, user):
        info = mock.Mock()
        info.context.user = user
        return self.gql_type.get_queryset(AsyncJob.objects.all(), info)

    def test_anonymous_sees_nothing(self):
        from django.contrib.auth.models import AnonymousUser

        self.assertEqual(self._scoped(AnonymousUser()).count(), 0)

    def test_user_sees_only_own_jobs(self):
        scoped = self._scoped(self.other)
        self.assertEqual(list(scoped), [self.other_job])

    def test_superuser_sees_all(self):
        # the default test-helper role is IMIS admin -> superuser
        self.assertEqual(self._scoped(self.owner).count(), 2)

    def test_right_900102_sees_all(self):
        self.assertFalse(self.viewer.is_superuser)
        self.assertEqual(self._scoped(self.viewer).count(), 2)

    def test_query_field_registered_with_uuid_scalar(self):
        from openIMIS.schema import schema as global_schema

        sdl = str(global_schema)
        self.assertIn("asyncJobs(", sdl)
        self.assertIn("AsyncJobGQLType", sdl)
        # fe-core polling contract
        self.assertIn("clientMutationId", sdl)
        self.assertIn("status_In", sdl)
        self.assertIn("uuid: UUID", sdl)
