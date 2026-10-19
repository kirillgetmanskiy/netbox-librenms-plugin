"""Table of sync profiles."""

import django_tables2 as tables
from netbox.tables import NetBoxTable, columns

from netbox_librenms_plugin.models import SyncProfile

STEPS_TEMPLATE = """
{% for step in record.step_infos %}<span class="badge text-bg-secondary me-1" title="{{ step.description }}">
<span class="text-muted">{{ forloop.counter }}.</span> {{ step.label }}</span>{% endfor %}
"""

SCHEDULE_TEMPLATE = """
{% if record.is_scheduled %}<i class="mdi mdi-calendar-clock text-success"></i> every {{ record.interval }} min
{% else %}<span class="text-muted">On demand</span>{% endif %}
"""

NEXT_RUN_TEMPLATE = """
{% with next=record.next_run %}{% if next %}{{ next|date:"Y-m-d H:i" }}{% else %}<span class="text-muted">—</span>{% endif %}{% endwith %}
"""

LAST_RUN_TEMPLATE = """{% load helpers %}
{% with job=record.last_job %}{% if job %}
<a href="{% url 'plugins:netbox_librenms_plugin:syncprofile_run' pk=record.pk job_pk=job.pk %}">{% badge job.get_status_display bg_color=job.get_status_color %}</a>
<small class="text-muted">{{ job.completed|default:job.started|date:"Y-m-d H:i" }}</small>
{% else %}<span class="text-muted">Never</span>{% endif %}{% endwith %}
"""


class SyncProfileTable(NetBoxTable):
    """List of sync profiles with their steps, schedule and last run."""

    name = tables.Column(linkify=True)
    enabled = columns.BooleanColumn()
    server_key = tables.Column(verbose_name="LibreNMS server")
    steps = columns.TemplateColumn(template_code=STEPS_TEMPLATE, orderable=False, verbose_name="Objects to sync")
    schedule = columns.TemplateColumn(template_code=SCHEDULE_TEMPLATE, orderable=False, verbose_name="Schedule")
    next_run = columns.TemplateColumn(template_code=NEXT_RUN_TEMPLATE, orderable=False, verbose_name="Next run")
    last_run = columns.TemplateColumn(template_code=LAST_RUN_TEMPLATE, orderable=False, verbose_name="Last run")
    tags = columns.TagColumn(url_name="plugins:netbox_librenms_plugin:syncprofile_list")
    actions = columns.ActionsColumn(actions=("edit", "delete"))

    class Meta(NetBoxTable.Meta):
        model = SyncProfile
        fields = (
            "pk",
            "id",
            "name",
            "enabled",
            "server_key",
            "steps",
            "schedule",
            "next_run",
            "last_run",
            "description",
            "tags",
            "created",
            "last_updated",
            "actions",
        )
        default_columns = ("pk", "name", "enabled", "server_key", "steps", "schedule", "next_run", "last_run")
