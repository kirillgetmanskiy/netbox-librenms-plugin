"""Run a sync profile: resolve its devices and run its steps in order, one transaction per step."""

import logging

from dcim.models import Device
from django.db import transaction
from django.utils import timezone

from netbox_librenms_plugin.sync_profiles import catalog
from netbox_librenms_plugin.sync_profiles.recorder import recording
from netbox_librenms_plugin.sync_profiles.steps import DEVICE_STEPS, StepError, StepSkipped, SyncContext, import_devices

logger = logging.getLogger(__name__)

STATUS_CHANGED = "changed"
STATUS_OK = "ok"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"
STATUSES = (STATUS_CHANGED, STATUS_OK, STATUS_SKIPPED, STATUS_ERROR)


def scoped_devices(profile, user, extra_pks=()):
    """
    Return the devices a profile syncs, as a list ordered by name.

    These are the devices *user* may view that are linked to the profile's LibreNMS server and
    match every set filter, plus the devices in *extra_pks* (the ones this run imported).
    """
    from netbox_librenms_plugin.utils import get_librenms_device_id

    queryset = Device.objects.restrict(user, "view").filter(custom_field_data__has_key="librenms_id")
    if profile.pk:
        if profile.sites.exists():
            queryset = queryset.filter(site__in=profile.sites.all())
        if profile.roles.exists():
            queryset = queryset.filter(role__in=profile.roles.all())
        if profile.platforms.exists():
            queryset = queryset.filter(platform__in=profile.platforms.all())
        if profile.device_tags.exists():
            queryset = queryset.filter(tags__in=profile.device_tags.all())
    if profile.device_statuses:
        queryset = queryset.filter(status__in=profile.device_statuses)
    devices = {
        device.pk: device
        for device in queryset.distinct().select_related("site", "role", "platform", "device_type")
        if get_librenms_device_id(device, profile.server_key, auto_save=False) is not None
    }
    if extra_pks:
        for device in Device.objects.restrict(user, "view").filter(pk__in=extra_pks):
            devices.setdefault(device.pk, device)
    return sorted(devices.values(), key=lambda device: (device.name or "", device.pk))


def _run_step(ctx, key, function, *args):
    """Run one step in its own transaction and return its result entry."""
    entry = {"key": key, "label": catalog.step_label(key), "status": STATUS_OK, "message": "", "changes": None}
    try:
        with transaction.atomic():
            with recording() as recorder:
                entry["message"] = function(ctx, *args) or ""
        entry["changes"] = recorder.as_dict()
        if recorder.total:
            entry["status"] = STATUS_CHANGED
    except StepSkipped as exc:
        entry.update(status=STATUS_SKIPPED, message=str(exc))
    except StepError as exc:
        entry.update(status=STATUS_ERROR, message=str(exc))
    except Exception as exc:
        logger.exception("Sync step %s failed", key)
        entry.update(status=STATUS_ERROR, message=f"{type(exc).__name__}: {exc}")
    return entry


def _cancelled(job_runner):
    if job_runner is None:
        return False
    from netbox_librenms_plugin.import_utils.bulk_import import _is_job_cancelled

    return _is_job_cancelled(job_runner)


class _ProfileRun:
    """The state of one run: the context, the devices it imported, and each device's step results."""

    def __init__(self, profile, ctx, *, limit, on_progress):
        self.profile = profile
        self.ctx = ctx
        self.log = ctx.logger
        self.limit = limit
        self.on_progress = on_progress
        self.imported = []
        self.devices = {}
        self.import_result = None
        self.cancelled = False

    def device_entry(self, device):
        if device.pk not in self.devices:
            self.devices[device.pk] = {
                "pk": device.pk,
                "name": str(device),
                "url": device.get_absolute_url(),
                "steps": [],
                "stopped": False,
            }
        return self.devices[device.pk]

    def run_import(self, key):
        """Run the once-per-run import step; its devices join the scope of the later steps."""
        self.log.info(f"Step: {catalog.step_label(key)}")
        imported_now = []

        def step(context):
            outcome, pks = import_devices(context)
            imported_now.extend(pks)
            self.import_result = outcome
            return outcome["message"]

        entry = _run_step(self.ctx, key, step)
        if entry["status"] == STATUS_ERROR or self.import_result is None:
            self.import_result = {"message": entry["message"], "items": []}
        self.import_result["status"] = entry["status"]
        self.imported.extend(imported_now)
        self.log.info(entry["message"])

    def run_device_step(self, key, position, step_count):
        """Run one per-device step for every device of the scope, before the next step starts."""
        devices = scoped_devices(self.profile, self.ctx.user, extra_pks=self.imported)
        if self.limit is not None:
            devices = [device for device in devices if device.pk in self.limit or device.pk in self.imported]
        label = catalog.step_label(key)
        self.log.info(f"Step {position} of {step_count}: {label}, {len(devices)} devices")
        for index, device in enumerate(devices, start=1):
            if (index == 1 or index % 5 == 0) and _cancelled(self.ctx.job_runner):
                self.log.warning(f"Run cancelled during {label}, at device {index} of {len(devices)}")
                self.cancelled = True
                return
            if self.on_progress is not None:
                self.on_progress(
                    {
                        "step": label,
                        "position": position,
                        "steps": step_count,
                        "done": index - 1,
                        "total": len(devices),
                        "current": str(device),
                    }
                )
            entry = self.device_entry(device)
            if not entry["stopped"]:
                self.run_device(device, key, entry)

    def run_device(self, device, key, entry):
        # Read again: an earlier step may have changed the device (its platform, its type).
        current = Device.objects.filter(pk=device.pk).first()
        if current is None:
            entry["steps"].append(
                {
                    "key": key,
                    "label": catalog.step_label(key),
                    "status": STATUS_SKIPPED,
                    "message": "The device was deleted.",
                    "changes": None,
                }
            )
            entry["stopped"] = True
            return
        step_entry = _run_step(self.ctx, key, DEVICE_STEPS[key], current)
        entry["steps"].append(step_entry)
        if step_entry["status"] == STATUS_ERROR:
            self.log.warning(f"{device}: {step_entry['label']}: {step_entry['message']}")
            if not self.profile.continue_on_error:
                entry["stopped"] = True


def _run_steps(run, steps):
    for position, key in enumerate(steps, start=1):
        if run.cancelled:
            break
        if catalog.STEPS_BY_KEY[key].per_run:
            run.run_import(key)
        else:
            run.run_device_step(key, position, len(steps))


def build_api(server_key):
    """Return the LibreNMS client of one configured server."""
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    return LibreNMSAPI(server_key=server_key)


def _summarize(devices):
    summary = dict.fromkeys(STATUSES, 0)
    summary.update(devices=len(devices), created=0, updated=0, deleted=0, devices_with_errors=0)
    for entry in devices:
        if any(step["status"] == STATUS_ERROR for step in entry["steps"]):
            summary["devices_with_errors"] += 1
        for step in entry["steps"]:
            summary[step["status"]] += 1
            for counter in ("created", "updated", "deleted"):
                summary[counter] += (step.get("changes") or {}).get(counter, 0)
    return summary


def run_profile(profile, *, user, dry_run=False, device_pks=None, job_runner=None, run_logger=None, on_progress=None):
    """
    Run *profile* for its devices and return the result that the run page shows.

    The steps run in the profile's order, and each step runs for every device before the next
    step starts, so a step that links devices (cables) sees what the earlier steps made for all
    of them. The once-per-run import step runs at its place in the order, and the devices it
    imports join the scope of the steps after it.

    Args:
        profile (SyncProfile): The profile to run.
        user (User): The user the run writes as; it writes only what this user may write.
        dry_run (bool): Make every write, record it, and roll it back.
        device_pks (list[int] | None): Limit the run to these devices of the scope.
        job_runner (JobRunner | None): The background job, for cancellation and the job log.
        run_logger (logging.Logger | None): Where progress goes; the job log in a background run.
        on_progress (Callable[[dict], None] | None): Called before each device of each step with the progress.

    Returns:
        dict: The run result, stored as the job's data.

    """
    log = run_logger or logger
    steps = catalog.normalize_steps(profile.steps)
    started = timezone.now()
    api = build_api(profile.server_key)
    ctx = SyncContext(api=api, profile=profile, user=user, dry_run=dry_run, job_runner=job_runner, run_logger=log)
    run = _ProfileRun(profile, ctx, limit=set(device_pks) if device_pks else None, on_progress=on_progress)

    if dry_run:
        # A dry run makes every write so each step sees what the steps before it made, records
        # them, and takes all of them back at the end. Its row locks last until then.
        with transaction.atomic():
            _run_steps(run, steps)
            transaction.set_rollback(True)
    else:
        _run_steps(run, steps)

    devices = list(run.devices.values())
    summary = _summarize(devices)
    log.info(
        f"Done: {summary['devices']} devices, {summary['changed']} steps changed NetBox, "
        f"{summary['error']} failed, {summary['skipped']} skipped"
        + (" (dry run, nothing was saved)" if dry_run else "")
    )
    return {
        "profile_id": profile.pk,
        "profile_name": profile.name,
        "server_key": profile.server_key,
        "dry_run": bool(dry_run),
        "steps": steps,
        "started": started.isoformat(),
        "finished": timezone.now().isoformat(),
        "import": run.import_result,
        "devices": devices,
        "summary": summary,
        "cancelled": run.cancelled,
        "completed": not run.cancelled,
    }
