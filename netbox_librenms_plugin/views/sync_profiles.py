"""Views of sync profiles: the NetBox object pages and the Run now, Schedule, Devices and run result dialogs."""

from core.choices import JobStatusChoices
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from netbox.views import generic

from netbox_librenms_plugin.constants import PERM_CHANGE_PLUGIN, PERM_VIEW_PLUGIN
from netbox_librenms_plugin.models import SyncProfile
from netbox_librenms_plugin.sync_profiles import scheduling
from netbox_librenms_plugin.sync_profiles.catalog import STEPS, step_label
from netbox_librenms_plugin.sync_profiles.engine import scoped_devices
from netbox_librenms_plugin.sync_profiles.filtersets import SyncProfileFilterSet
from netbox_librenms_plugin.sync_profiles.forms import (
    RunProfileForm,
    ScheduleProfileForm,
    SyncProfileFilterForm,
    SyncProfileForm,
)
from netbox_librenms_plugin.tables.sync_profiles import SyncProfileTable
from netbox_librenms_plugin.views.mixins import LibreNMSGenericPermissionMixin, LibreNMSGenericWritePermissionMixin

TEMPLATE_DIR = "netbox_librenms_plugin/sync_profiles"
INTERVAL_PRESETS = ((15, "15 min"), (60, "1 hour"), (360, "6 hours"), (720, "12 hours"), (1440, "1 day"))


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _htmx_redirect(request, url):
    """Send the browser to *url*: by an HX-Redirect for a dialog, by a plain redirect otherwise."""
    if _is_htmx(request):
        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response
    return redirect(url)


def _profile_for(request, pk, action):
    """Return the profile *pk* when the user may *action* it and use the plugin, or raise."""
    required = PERM_VIEW_PLUGIN if action == "view" else PERM_CHANGE_PLUGIN
    if not request.user.has_perm(required):
        raise PermissionDenied("You do not have permission to use LibreNMS sync profiles.")
    return get_object_or_404(SyncProfile.objects.restrict(request.user, action), pk=pk)


def run_summary(job):
    """Return the numbers a run row shows, from the job's data."""
    data = job.data if isinstance(job.data, dict) else {}
    summary = data.get("summary") or {}
    duration = None
    if job.started and job.completed:
        duration = int((job.completed - job.started).total_seconds())
    return {
        "job": job,
        "dry_run": bool(data.get("dry_run")),
        "skipped_reason": data.get("skipped"),
        "summary": summary,
        "duration": duration,
        "imported": data.get("import"),
    }


class SyncProfileListView(LibreNMSGenericPermissionMixin, generic.ObjectListView):
    """List the sync profiles."""

    queryset = SyncProfile.objects.prefetch_related("tags")
    table = SyncProfileTable
    filterset = SyncProfileFilterSet
    filterset_form = SyncProfileFilterForm


class SyncProfileView(LibreNMSGenericPermissionMixin, generic.ObjectView):
    """A profile with its steps, scope and schedule, its recent runs, and the dialogs that run it."""

    queryset = SyncProfile.objects.prefetch_related("sites", "roles", "platforms", "device_tags", "tags")
    template_name = f"{TEMPLATE_DIR}/syncprofile.html"

    def get_extra_context(self, request, instance):
        jobs = list(scheduling.profile_jobs(instance)[:15])
        return {
            "runs": [run_summary(job) for job in jobs if job.status != JobStatusChoices.STATUS_SCHEDULED],
            "scheduled_job": scheduling.scheduled_job(instance),
            "active_job": scheduling.active_jobs(instance).first(),
            "can_run": request.user.has_perm(PERM_CHANGE_PLUGIN)
            and SyncProfile.objects.restrict(request.user, "change").filter(pk=instance.pk).exists(),
            "status_labels": [label for value, label in _status_choices() if value in instance.device_statuses],
        }


def _status_choices():
    from dcim.choices import DeviceStatusChoices

    return list(DeviceStatusChoices)


class SyncProfileEditView(LibreNMSGenericWritePermissionMixin, generic.ObjectEditView):
    """Create or edit a profile; saving it also updates its schedule."""

    queryset = SyncProfile.objects.all()
    form = SyncProfileForm

    def alter_object(self, obj, request, url_args, url_kwargs):
        # The form schedules the saved profile as this user.
        obj._sync_request = request
        return obj


class SyncProfileDeleteView(LibreNMSGenericWritePermissionMixin, generic.ObjectDeleteView):
    """Delete a profile and its waiting runs."""

    queryset = SyncProfile.objects.all()


class SyncProfileBulkDeleteView(LibreNMSGenericWritePermissionMixin, generic.BulkDeleteView):
    """Delete several profiles."""

    queryset = SyncProfile.objects.all()
    filterset = SyncProfileFilterSet
    table = SyncProfileTable


class SyncProfileChangeLogView(LibreNMSGenericPermissionMixin, generic.ObjectChangeLogView):
    """Change log of a profile."""

    base_template = f"{TEMPLATE_DIR}/syncprofile.html"


class SyncProfileRunView(LoginRequiredMixin, View):
    """The Run now dialog: queue a real run or a dry run of the profile."""

    template_name = f"{TEMPLATE_DIR}/run_modal.html"

    def get(self, request, pk):
        profile = _profile_for(request, pk, "change")
        form = RunProfileForm(initial={"dry_run": request.GET.get("dry_run") == "1"})
        form.fields["devices"].queryset = form.fields["devices"].queryset.restrict(request.user, "view")
        return render(request, self.template_name, self._context(request, profile, form))

    def post(self, request, pk):
        profile = _profile_for(request, pk, "change")
        form = RunProfileForm(request.POST)
        form.fields["devices"].queryset = form.fields["devices"].queryset.restrict(request.user, "view")
        if not form.is_valid():
            return render(request, self.template_name, self._context(request, profile, form))
        if not profile.enabled:
            form.add_error(None, "The profile is disabled. Enable it in the Schedule dialog or the edit form.")
            return render(request, self.template_name, self._context(request, profile, form))
        if scheduling.active_jobs(profile).exists():
            form.add_error(None, "A run of this profile is already queued or running. Wait for it to finish.")
            return render(request, self.template_name, self._context(request, profile, form))
        job = scheduling.run_now(
            profile,
            request,
            dry_run=form.cleaned_data["dry_run"],
            device_pks=[device.pk for device in form.cleaned_data["devices"]],
        )
        messages.success(
            request,
            f"{'Dry run' if form.cleaned_data['dry_run'] else 'Run'} of {profile} queued. "
            "This page follows its progress.",
        )
        return _htmx_redirect(
            request, reverse("plugins:netbox_librenms_plugin:syncprofile_run", args=[profile.pk, job.pk])
        )

    def _context(self, request, profile, form):
        return {
            "object": profile,
            "form": form,
            "device_count": len(scoped_devices(profile, request.user)),
            "steps": profile.step_infos,
        }


class SyncProfileScheduleView(LoginRequiredMixin, View):
    """The Schedule dialog: turn the schedule on or off, set its interval and its first run."""

    template_name = f"{TEMPLATE_DIR}/schedule_modal.html"

    def get(self, request, pk):
        profile = _profile_for(request, pk, "change")
        return render(request, self.template_name, self._context(profile, ScheduleProfileForm(instance=profile)))

    def post(self, request, pk):
        profile = _profile_for(request, pk, "change")
        profile.snapshot()
        if request.POST.get("action") == "clear":
            data = request.POST.copy()
            data["interval"] = ""
            data["schedule_start"] = ""
        else:
            data = request.POST
        form = ScheduleProfileForm(data, instance=profile)
        if not form.is_valid():
            return render(request, self.template_name, self._context(profile, form))
        profile = form.save()
        job = scheduling.apply_schedule(profile, request)
        if job is not None:
            messages.success(
                request, f"{profile} runs every {profile.interval} minutes from {job.scheduled:%Y-%m-%d %H:%M}."
            )
        else:
            messages.success(request, f"{profile} now runs on demand only.")
        return _htmx_redirect(request, profile.get_absolute_url())

    def _context(self, profile, form):
        return {
            "object": profile,
            "form": form,
            "presets": INTERVAL_PRESETS,
            "scheduled_job": scheduling.scheduled_job(profile),
        }


class SyncProfileDevicesView(LoginRequiredMixin, View):
    """The Devices dialog: the devices the profile syncs now."""

    template_name = f"{TEMPLATE_DIR}/devices_modal.html"

    def get(self, request, pk):
        profile = _profile_for(request, pk, "view")
        devices = scoped_devices(profile, request.user)
        return render(request, self.template_name, {"object": profile, "devices": devices[:500], "total": len(devices)})


class SyncProfileRunResultView(LoginRequiredMixin, View):
    """
    The result of one run: a full page, or the dialog body when requested by htmx.

    While the run is queued or running, the result part reloads itself every few seconds.
    """

    def get(self, request, pk, job_pk):
        profile = _profile_for(request, pk, "view")
        job = get_object_or_404(scheduling.profile_jobs(profile), pk=job_pk)
        context = {
            "object": profile,
            "job": job,
            "run": run_summary(job),
            "data": job.data if isinstance(job.data, dict) else {},
            "progress": scheduling.get_progress(job),
            "finished": job.status
            not in (JobStatusChoices.ENQUEUED_STATE_CHOICES + (JobStatusChoices.STATUS_RUNNING,)),
            "step_label": step_label,
            "all_steps": STEPS,
        }
        context["devices"] = _device_rows(context["data"])
        part = request.GET.get("part")
        if part == "results":
            return render(request, f"{TEMPLATE_DIR}/inc/run_results.html", context)
        if part == "modal":
            return render(request, f"{TEMPLATE_DIR}/run_result_modal.html", context)
        return render(request, f"{TEMPLATE_DIR}/run_result.html", context)


def _device_rows(data):
    """Add the overall status of each device to the run data's device rows."""
    rows = []
    for device in data.get("devices") or []:
        statuses = {step.get("status") for step in device.get("steps") or []}
        if "error" in statuses:
            overall = "error"
        elif "changed" in statuses:
            overall = "changed"
        elif statuses <= {"skipped"}:
            overall = "skipped"
        else:
            overall = "ok"
        rows.append({**device, "overall": overall})
    return rows
