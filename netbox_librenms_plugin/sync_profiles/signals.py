"""Keep a sync profile's queued jobs consistent with the profile."""

from django.db.models.signals import pre_delete


def _drop_schedule(sender, instance, **kwargs):
    """Remove a deleted profile's waiting jobs from the queue; a cascade delete would leave them queued."""
    from core.choices import JobStatusChoices

    from netbox_librenms_plugin.sync_profiles.scheduling import profile_jobs

    for job in profile_jobs(instance).filter(status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES):
        job.delete()


def connect():
    from netbox_librenms_plugin.models import SyncProfile

    pre_delete.connect(_drop_schedule, sender=SyncProfile, dispatch_uid="netbox_librenms_plugin_sync_profile_delete")
