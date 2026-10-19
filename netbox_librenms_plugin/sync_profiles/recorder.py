"""Record the NetBox objects that one sync step creates, changes or deletes."""

from contextlib import contextmanager
from contextvars import ContextVar

from django.db.models.signals import m2m_changed, post_delete, post_save

# Only rows of these apps are reported; the change log, jobs and caches are not sync results.
RECORDED_APPS = frozenset({"dcim", "ipam", "virtualization"})
# Each step keeps at most this many change lines; the counts stay exact.
MAX_RECORDED_LINES = 200

_active = ContextVar("netbox_librenms_plugin_sync_recorder", default=None)


class ChangeRecorder:
    """Collect created / updated / deleted rows while it is active."""

    def __init__(self):
        self.created = 0
        self.updated = 0
        self.deleted = 0
        self.lines = []
        self._seen = set()

    @property
    def total(self):
        return self.created + self.updated + self.deleted

    def add(self, action, instance):
        """Count one write of *instance*; a second write of the same row in one step is one change."""
        key = (action if action == "deleted" else "written", instance._meta.label_lower, instance.pk)
        if key in self._seen:
            return
        # A row this step created and then updated again is reported once, as created.
        if action == "updated" and ("written", instance._meta.label_lower, instance.pk) in self._seen:
            return
        self._seen.add(key)
        setattr(self, action, getattr(self, action) + 1)
        if len(self.lines) < MAX_RECORDED_LINES:
            self.lines.append(
                {
                    "action": action,
                    "model": str(instance._meta.verbose_name),
                    "object": _describe(instance),
                }
            )

    def as_dict(self):
        return {
            "created": self.created,
            "updated": self.updated,
            "deleted": self.deleted,
            "lines": self.lines,
            "truncated": self.total > len(self.lines),
        }


def _describe(instance):
    try:
        text = str(instance)
    except Exception:
        text = f"#{instance.pk}"
    parent = getattr(instance, "device", None) or getattr(instance, "virtual_machine", None)
    if parent is not None and instance._meta.model_name in {"interface", "vminterface"}:
        return f"{parent}: {text}"
    return text[:200]


def _recorded(sender):
    return sender._meta.app_label in RECORDED_APPS


def _on_save(sender, instance, created, raw=False, **kwargs):
    recorder = _active.get()
    if recorder is None or raw or not _recorded(sender):
        return
    recorder.add("created" if created else "updated", instance)


def _on_delete(sender, instance, **kwargs):
    recorder = _active.get()
    if recorder is None or not _recorded(sender):
        return
    recorder.add("deleted", instance)


def _on_m2m(sender, instance, action, reverse, **kwargs):
    recorder = _active.get()
    if recorder is None or action not in {"post_add", "post_remove", "post_clear"} or reverse:
        return
    if not _recorded(type(instance)):
        return
    recorder.add("updated", instance)


def connect():
    """Connect the receivers once; they do nothing unless a recorder is active."""
    post_save.connect(_on_save, dispatch_uid="netbox_librenms_plugin_sync_profile_save")
    post_delete.connect(_on_delete, dispatch_uid="netbox_librenms_plugin_sync_profile_delete")
    m2m_changed.connect(_on_m2m, dispatch_uid="netbox_librenms_plugin_sync_profile_m2m")


@contextmanager
def recording():
    """Make a new recorder active for the duration of the block and yield it."""
    recorder = ChangeRecorder()
    token = _active.set(recorder)
    try:
        yield recorder
    finally:
        _active.reset(token)
