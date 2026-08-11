import importlib.util
import logging

from apscheduler.schedulers.background import BackgroundScheduler

# from apscheduler.executors.pool import ProcessPoolExecutor, ThreadPoolExecutor
from core import get_scheduler_method_ref
from django_apscheduler.jobstores import register_events  # , register_job
from copy import deepcopy

from django.conf import settings

logger = logging.getLogger(__name__)

# Create scheduler to run in a thread inside the application process
scheduler = BackgroundScheduler(deepcopy(settings.SCHEDULER_CONFIG))


def schedule_tasks(task_scheduler):
    """
    Does the actual scheduling and is shared between the start() below and the management command for standalone
    execution
    :param scheduler: scheduler to which we'll add the tasks
    """
    from core.scheduled_tasks import schedule_tasks as schedule_core_tasks

    schedule_core_tasks(task_scheduler)

    # Discover other modules' scheduled_tasks.py (same loop as the assembly's
    # apscheduler_runner), so module periodic jobs also run in the dedicated
    # scheduler process started by manage.py runapscheduler.
    for app_ in getattr(settings, "OPENIMIS_APPS", []):
        if app_ == "core":
            continue
        if not importlib.util.find_spec(f"{app_}.scheduled_tasks"):
            logger.debug("%s has no scheduled_tasks module, skipping", app_)
            continue
        try:
            app_module = __import__(f"{app_}.scheduled_tasks")
            app_module.scheduled_tasks.schedule_tasks(task_scheduler)
            logger.debug("%s tasks scheduled", app_)
        except Exception as exc:
            logger.warning(
                "%s: failed to register scheduled tasks: %s", app_, exc
            )

    if settings.SCHEDULER_JOBS:
        for job in settings.SCHEDULER_JOBS:
            logger.debug("Scheduling job %s", job["method"])
            method = get_scheduler_method_ref(job["method"])
            task_scheduler.add_job(
                *([method] + job.get("args", [])), **(job.get("kwargs", {}))
            )

    if settings.SCHEDULER_CUSTOM:
        for job in settings.SCHEDULER_CUSTOM:
            logger.debug("Calling custom scheduler %s", job["method"])
            method = get_scheduler_method_ref(job["method"])
            method(*([task_scheduler] + job.get("args", [])), **(job.get("kwargs", {})))


def start():
    if settings.DEBUG:
        # Hook into the apscheduler logger
        logging.basicConfig()
        logging.getLogger("apscheduler").setLevel(logging.DEBUG)

    schedule_tasks(scheduler)

    # Add the scheduled jobs to the Django admin interface
    register_events(scheduler)

    scheduler.start()
