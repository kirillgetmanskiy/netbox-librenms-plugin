"""Start, schedule and unschedule the background runs of a sync profile."""

from datetime import timedelta

from core.choices import JobStatusChoices
from django.core.cache import cache
from django.utils import timezone

PROGRESS_TIMEOUT = 6 * 3600


def _progress_key(job_pk):
    return f"netbox_librenms_plugin_sync_profile_progress_{job_pk}"


def set_progress(job, progress):
    """Store how far a running job got, for the run page."""
    cache.set(_progress_key(job.pk), progress, timeout=PROGRESS_TIMEOUT)


def get_progress(job):
    """Return how far a running job got, or None."""
    return cache.get(_progress_key(job.pk))


def _job_class():
    from netbox_librenms_plugin.jobs import SyncProfileJob

    return SyncProfileJob


def profile_jobs(profile):
    """Return every job of *profile*, newest first."""
    return _job_class().get_jobs(profile).order_by("-created")


def scheduled_job(profile):
    """Return the pending job of the profile's schedule, or None."""
    return (
        profile_jobs(profile)
        .filter(status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES, interval__isnull=False)
        .order_by("scheduled")
        .first()
    )


def active_jobs(profile):
    """Return the jobs of *profile* that are queued now or running (not the future schedule)."""
    return profile_jobs(profile).filter(
        status__in=(JobStatusChoices.STATUS_PENDING, JobStatusChoices.STATUS_RUNNING),
    )


def unschedule(profile):
    """Delete the profile's pending scheduled jobs; ``Job.delete()`` also removes them from the queue."""
    removed = 0
    for job in profile_jobs(profile).filter(status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES, interval__isnull=False):
        job.delete()
        removed += 1
    return removed


def run_now(profile, request, *, dry_run=False, device_pks=None):
    """Queue one run of *profile* now, as the user of *request*."""
    from utilities.request import copy_safe_request

    return _job_class().enqueue(
        instance=profile,
        user=request.user,
        request=copy_safe_request(request),
        dry_run=dry_run,
        device_pks=list(device_pks or []),
    )


def apply_schedule(profile, request):
    """
    Make the queue match the profile's schedule, as the user of *request*.

    An enabled profile with an interval gets exactly one pending scheduled job; any other profile
    gets none. Returns the scheduled job, or None.
    """
    from utilities.request import copy_safe_request

    unschedule(profile)
    if not profile.is_scheduled:
        return None
    start = profile.schedule_start
    now = timezone.now()
    if start is None or start < now:
        start = now + timedelta(minutes=profile.interval)
    return _job_class().enqueue(
        instance=profile,
        user=request.user,
        schedule_at=start,
        interval=profile.interval,
        request=copy_safe_request(request),
        dry_run=False,
        device_pks=[],
    )
