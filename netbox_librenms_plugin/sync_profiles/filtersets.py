"""Filter set for sync profiles."""

import django_filters
from django.db.models import Q
from netbox.filtersets import NetBoxModelFilterSet

from netbox_librenms_plugin.models import SyncProfile


class SyncProfileFilterSet(NetBoxModelFilterSet):
    """Filter sync profiles by name, state and server."""

    server_key = django_filters.CharFilter(lookup_expr="iexact")

    class Meta:
        model = SyncProfile
        fields = ("id", "name", "enabled", "server_key", "interval")

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(name__icontains=value) | Q(description__icontains=value))
