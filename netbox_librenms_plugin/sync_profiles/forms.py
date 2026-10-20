"""Forms for sync profiles: the edit form, the filter form, and the Run now and Schedule dialogs."""

from dcim.choices import DeviceStatusChoices
from dcim.models import Device, DeviceRole, Platform, Site
from django import forms
from extras.models import Tag
from netbox.forms import NetBoxModelFilterSetForm, NetBoxModelForm
from utilities.forms.fields import DynamicModelChoiceField, DynamicModelMultipleChoiceField, TagFilterField
from utilities.forms.rendering import FieldSet
from utilities.forms.widgets import DateTimePicker

from netbox_librenms_plugin.models import SyncProfile
from netbox_librenms_plugin.sync_profiles.catalog import DEFAULT_STEPS, STEP_CHOICES, STEPS, normalize_steps


def server_choices():
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    return [(key, label) for key, label in LibreNMSAPI.get_available_servers().items()]


class OrderedStepsWidget(forms.Widget):
    """
    Every sync step as a row with a check box, in run order; the rows can be dragged or moved.

    The checked boxes post in the order of their rows, so the posted list is the run order.
    """

    template_name = "netbox_librenms_plugin/sync_profiles/widgets/ordered_steps.html"

    def value_from_datadict(self, data, files, name):
        return data.getlist(name) if hasattr(data, "getlist") else data.get(name)

    def format_value(self, value):
        if isinstance(value, str):
            value = [value]
        return [str(item) for item in value or []]

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        selected = normalize_steps(context["widget"]["value"])
        ordered = [step for key in selected for step in STEPS if step.key == key]
        ordered += [step for step in STEPS if step.key not in selected]
        context["widget"]["steps"] = [{"info": step, "checked": step.key in selected} for step in ordered]
        return context


class SyncProfileForm(NetBoxModelForm):
    """Create or edit a sync profile."""

    server_key = forms.ChoiceField(label="LibreNMS server", choices=server_choices)
    steps = forms.MultipleChoiceField(
        label="Objects to sync",
        choices=STEP_CHOICES,
        widget=OrderedStepsWidget,
        help_text="Tick the objects to sync and drag them into the order to sync them in.",
    )
    sites = DynamicModelMultipleChoiceField(queryset=Site.objects.all(), required=False)
    roles = DynamicModelMultipleChoiceField(queryset=DeviceRole.objects.all(), required=False)
    platforms = DynamicModelMultipleChoiceField(queryset=Platform.objects.all(), required=False)
    device_tags = DynamicModelMultipleChoiceField(queryset=Tag.objects.all(), required=False, label="Device tags")
    device_statuses = forms.MultipleChoiceField(choices=DeviceStatusChoices, required=False, label="Device statuses")
    import_role = DynamicModelChoiceField(
        queryset=DeviceRole.objects.all(), required=False, label="Role for new devices"
    )
    import_site = DynamicModelChoiceField(queryset=Site.objects.all(), required=False, label="Site for new devices")
    schedule_start = forms.DateTimeField(required=False, label="First run", widget=DateTimePicker())

    fieldsets = (
        FieldSet("name", "description", "server_key", "enabled", "tags", name="Profile"),
        FieldSet("steps", "continue_on_error", name="Objects to sync"),
        FieldSet("sites", "roles", "platforms", "device_tags", "device_statuses", name="Devices to sync"),
        FieldSet(
            "import_role",
            "import_site",
            "librenms_type",
            "librenms_os",
            "librenms_location",
            "librenms_hostname",
            name="Import of new devices",
        ),
        FieldSet("interval", "schedule_start", name="Schedule"),
    )

    class Meta:
        model = SyncProfile
        fields = (
            "name",
            "description",
            "server_key",
            "enabled",
            "steps",
            "continue_on_error",
            "sites",
            "roles",
            "platforms",
            "device_tags",
            "device_statuses",
            "import_role",
            "import_site",
            "librenms_type",
            "librenms_os",
            "librenms_location",
            "librenms_hostname",
            "interval",
            "schedule_start",
            "tags",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk and not self.initial.get("steps"):
            self.initial["steps"] = list(DEFAULT_STEPS)
        choices = server_choices()
        if len(choices) == 1 and not self.initial.get("server_key"):
            self.initial["server_key"] = choices[0][0]

    def clean_steps(self):
        return normalize_steps(self.cleaned_data.get("steps"))

    def save(self, *args, **kwargs):
        profile = super().save(*args, **kwargs)
        request = getattr(self.instance, "_sync_request", None)
        if request is not None:
            from netbox_librenms_plugin.sync_profiles.scheduling import apply_schedule

            apply_schedule(profile, request)
        return profile


class SyncProfileFilterForm(NetBoxModelFilterSetForm):
    """Filter the list of sync profiles."""

    model = SyncProfile
    enabled = forms.NullBooleanField(
        required=False, widget=forms.Select(choices=[("", "---------"), ("true", "Yes"), ("false", "No")])
    )
    server_key = forms.CharField(required=False, label="LibreNMS server")
    tag = TagFilterField(model)
    fieldsets = (FieldSet("q", "filter_id", "tag"), FieldSet("enabled", "server_key", name="Profile"))


class RunProfileForm(forms.Form):
    """
    The Run now dialog: a real run or a dry run, for every device of the scope or a few of them.

    *scope* is the profile's devices for this user; the device list offers only them.
    """

    dry_run = forms.BooleanField(
        required=False,
        label="Dry run",
        help_text="Make every change, report it, and roll it back. NetBox stays as it is.",
    )
    devices = forms.ModelMultipleChoiceField(
        queryset=Device.objects.none(),
        required=False,
        label="Only these devices",
        help_text="Only devices that match the profile's filters are listed. Leave empty to sync all of them.",
    )

    def __init__(self, *args, scope=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["devices"].queryset = Device.objects.filter(pk__in=[device.pk for device in scope]).order_by(
            "name", "pk"
        )


class ScheduleProfileForm(forms.ModelForm):
    """The Schedule dialog: turn the schedule on or off and set its interval and first run."""

    schedule_start = forms.DateTimeField(required=False, label="First run", widget=DateTimePicker())

    class Meta:
        model = SyncProfile
        fields = ("enabled", "interval", "schedule_start")
