import logging

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.apps import CoreConfig
from core.services import send_password_expiry_reminders

logger = logging.getLogger(__name__)


def async_job_jobstore_heartbeat():
    """Periodic no-op wakeup so the scheduler re-reads its job store,
    bounding pickup latency of jobs handed off by web processes."""
    logger.debug("async job store heartbeat")


def schedule_tasks(scheduler):
    scheduler.add_job(
        send_password_expiry_reminders,
        trigger=CronTrigger(
            hour=CoreConfig.password_expiry_email_reminder_hour,
            minute=CoreConfig.password_expiry_email_reminder_minute,
        ),
        id="core_password_expiry_email_reminders",
        max_instances=1,
        replace_existing=True,
    )
    logger.info("Scheduled core password expiry email reminders")

    poll_seconds = int(CoreConfig.async_job_jobstore_poll_seconds)
    if poll_seconds > 0:
        scheduler.add_job(
            async_job_jobstore_heartbeat,
            trigger=IntervalTrigger(seconds=poll_seconds),
            id="core_async_job_jobstore_heartbeat",
            max_instances=1,
            replace_existing=True,
        )
        logger.info(
            "Scheduled async job store heartbeat every %ss", poll_seconds
        )
