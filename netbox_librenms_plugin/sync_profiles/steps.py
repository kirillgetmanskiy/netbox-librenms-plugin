"""
The sync steps a profile runs.

Each per-device step takes the run context and one NetBox device, writes what LibreNMS reports,
and returns a short message. A step raises :class:`StepSkipped` when there is nothing it can do for
the device and :class:`StepError` when it fails. The engine runs every step in its own transaction
and records the rows the step writes, so a step does not count its own changes.
"""

import logging

from dcim.models import Cable, Device, Interface
from django.db import transaction
from ipam.models import VLAN, IPAddress

from netbox_librenms_plugin.sync_profiles import catalog

logger = logging.getLogger(__name__)

PLACEHOLDER_VALUES = frozenset({"", "-", "n/a", "na", "none", "null", "unknown"})


class StepSkipped(Exception):
    """The step has nothing to do for this device; the message says why."""


class StepError(Exception):
    """The step failed for this device; the message says why."""


class SyncContext:
    """What every step of one run shares: the API client, the user, and per-device LibreNMS reads."""

    def __init__(self, *, api, profile, user, dry_run, job_runner=None, run_logger=None):
        self.api = api
        self.server_key = api.server_key
        self.profile = profile
        self.user = user
        self.dry_run = dry_run
        self.job_runner = job_runner
        self.logger = run_logger or logger
        self._device_info = {}
        self._ports = {}
        self._port_stacks = {}
        self._rules = None

    # LibreNMS reads, cached for the run so later steps reuse what earlier steps fetched.

    def librenms_id(self, device):
        from netbox_librenms_plugin.utils import get_librenms_device_id

        librenms_id = get_librenms_device_id(device, self.server_key, auto_save=False)
        if librenms_id is None:
            raise StepSkipped("The device is not linked to this LibreNMS server.")
        return librenms_id

    def device_info(self, device):
        librenms_id = self.librenms_id(device)
        if librenms_id not in self._device_info:
            success, data = self.api.get_device_info(librenms_id, use_cache=False)
            if not success or not isinstance(data, dict):
                reason = str(data) if data else "LibreNMS does not know this device."
                raise StepError(f"Could not read the device from LibreNMS: {reason}")
            self._device_info[librenms_id] = data
        return self._device_info[librenms_id]

    def ports(self, device):
        librenms_id = self.librenms_id(device)
        if librenms_id not in self._ports:
            success, data = self.api.get_ports(librenms_id)
            ports = data.get("ports") if success and isinstance(data, dict) else None
            if not isinstance(ports, list):
                raise StepError(f"Could not read the ports from LibreNMS: {data}")
            self._ports[librenms_id] = [port for port in ports if isinstance(port, dict)]
        return self._ports[librenms_id]

    def port_stack(self, device):
        librenms_id = self.librenms_id(device)
        if librenms_id not in self._port_stacks:
            success, data = self.api.get_port_stack(librenms_id)
            if not success or not isinstance(data, list):
                raise StepError(f"Could not read the port stack from LibreNMS: {data}")
            self._port_stacks[librenms_id] = data
        return self._port_stacks[librenms_id]

    @property
    def rules(self):
        if self._rules is None:
            from netbox_librenms_plugin.interface_rules import InterfaceRuleMatcher

            self._rules = InterfaceRuleMatcher.load()
        return self._rules

    # Permissions: a run writes only what the user who started it may write.

    def restrict(self, model, action):
        return model.objects.restrict(self.user, action)

    def require(self, model, action, obj=None):
        queryset = self.restrict(model, action)
        if obj is None:
            allowed = self.user.has_perm(f"{model._meta.app_label}.{action}_{model._meta.model_name}")
        else:
            allowed = queryset.filter(pk=obj.pk).exists()
        if not allowed:
            raise StepError(f"You do not have permission to {action} {model._meta.verbose_name_plural}.")

    def lock_device(self, device):
        """Return the device row locked for this step, read again so the step writes its current state."""
        locked = Device.objects.select_for_update().filter(pk=device.pk).first()
        if locked is None:
            raise StepSkipped("The device was deleted.")
        return locked

    def members(self, device):
        """Return the devices whose interfaces belong to *device*: its virtual chassis members, or itself."""
        if device.virtual_chassis_id:
            return list(Device.objects.filter(virtual_chassis_id=device.virtual_chassis_id))
        return [device]

    def interface_name_field(self, device):
        from netbox_librenms_plugin.utils import get_interface_name_field

        return get_interface_name_field(obj=device)

    def interface_for_port(self, device, port_id):
        """Return the interface of *device* (or its chassis members) bound to a LibreNMS port, or None."""
        from netbox_librenms_plugin.utils import AmbiguousLibreNMSIdError, find_interface_by_librenms_port_id

        try:
            interface = find_interface_by_librenms_port_id(port_id, self.server_key)
        except AmbiguousLibreNMSIdError:
            return None
        if not isinstance(interface, Interface):
            return None
        if interface.device_id not in {member.pk for member in self.members(device)}:
            return None
        return interface


def _clean_text(value):
    text = "" if value is None else str(value).strip()
    return "" if text.lower() in PLACEHOLDER_VALUES else text


def _save_device_field(ctx, device, field, value, label):
    """Write one device field when it differs, with a change log before-state."""
    ctx.require(Device, "change", device)
    locked = ctx.lock_device(device)
    current = getattr(locked, field)
    if current == value:
        return f"{label} is already up to date."
    locked.snapshot()
    setattr(locked, field, value)
    locked.full_clean()
    locked.save()
    shown_before = current if current not in (None, "") else "empty"
    return f"{label}: {shown_before} → {value}."


# Device fields


def sync_serial(ctx, device):
    from netbox_librenms_plugin.utils import apply_normalization_rules, normalize_serial

    serial = _clean_text(normalize_serial(ctx.device_info(device).get("serial")))
    if not serial:
        raise StepSkipped("LibreNMS has no serial number for this device.")
    manufacturer = device.device_type.manufacturer if device.device_type_id else None
    serial = apply_normalization_rules(serial, "serial", manufacturer=manufacturer) or serial
    return _save_device_field(ctx, device, "serial", serial[:50], "Serial number")


def sync_platform(ctx, device):
    from netbox_librenms_plugin.utils import find_matching_platform

    librenms_os = _clean_text(ctx.device_info(device).get("os"))
    if not librenms_os:
        raise StepSkipped("LibreNMS reports no OS for this device.")
    match = find_matching_platform(librenms_os)
    if match.get("match_type") == "ambiguous":
        raise StepError(f"The OS '{librenms_os}' matches more than one platform. Add a platform mapping.")
    if not match.get("found"):
        raise StepSkipped(f"No NetBox platform matches the OS '{librenms_os}'. Add a platform mapping.")
    platform = match["platform"]
    ctx.require(Device, "change", device)
    locked = ctx.lock_device(device)
    if locked.platform_id == platform.pk:
        return "Platform is already up to date."
    before = locked.platform
    locked.snapshot()
    locked.platform = platform
    locked.full_clean()
    locked.save()
    return f"Platform: {before or 'empty'} → {platform}."


def sync_device_type(ctx, device):
    from netbox_librenms_plugin.utils import match_librenms_hardware_to_device_type

    hardware = _clean_text(ctx.device_info(device).get("hardware"))
    if not hardware:
        raise StepSkipped("LibreNMS reports no hardware for this device.")
    match = match_librenms_hardware_to_device_type(hardware)
    if match is None:
        raise StepError(f"The hardware '{hardware}' matches more than one device type. Add a device type mapping.")
    if not match.get("matched"):
        raise StepSkipped(f"No NetBox device type matches the hardware '{hardware}'. Add a device type mapping.")
    device_type = match["device_type"]
    ctx.require(Device, "change", device)
    locked = ctx.lock_device(device)
    if locked.device_type_id == device_type.pk:
        return "Device type is already up to date."
    before = locked.device_type
    locked.snapshot()
    locked.device_type = device_type
    locked.full_clean()
    locked.save()
    return f"Device type: {before} → {device_type}."


# Interfaces


def sync_interfaces(ctx, device):
    from netbox_librenms_plugin.interface_rules import PortSyncBlocked
    from netbox_librenms_plugin.interface_sync import resolve_or_create_interface_from_port
    from netbox_librenms_plugin.utils import normalize_librenms_port_id, resolve_interface_row_device

    ports = ctx.ports(device)
    if not ports:
        raise StepSkipped("LibreNMS reports no ports for this device.")
    ctx.require(Interface, "change")
    name_field = ctx.interface_name_field(device)
    members = ctx.members(device)
    members_by_position = {member.vc_position: member for member in members if member.vc_position is not None}
    members_by_id = {member.pk: member for member in members}
    addable = ctx.restrict(Interface, "add")
    changeable = ctx.restrict(Interface, "change")
    viewable = ctx.restrict(Interface, "view")

    synced = ignored = 0
    problems = []
    for port in ports:
        if normalize_librenms_port_id(port.get("port_id")) is None:
            continue
        owner = device
        if device.virtual_chassis_id:
            owner = resolve_interface_row_device(
                device,
                port,
                name_field,
                members_by_position=members_by_position,
                members_by_id=members_by_id,
            )
        try:
            # A savepoint per port: one refused port does not undo the ports before it.
            with transaction.atomic():
                resolve_or_create_interface_from_port(
                    owner,
                    port,
                    rules=ctx.rules,
                    server_key=ctx.server_key,
                    interface_name_field=name_field,
                    addable_queryset=addable,
                    changeable_queryset=changeable,
                    viewable_queryset=viewable,
                )
            synced += 1
        except PortSyncBlocked:
            ignored += 1
        except Exception as exc:
            problems.append(f"{port.get(name_field) or port.get('port_id')}: {exc}")

    message = f"{synced} of {len(ports)} ports synced"
    if ignored:
        message += f", {ignored} ignored by interface rules"
    if problems:
        message += f", {len(problems)} refused: " + "; ".join(problems[:10])
        if len(problems) > 10:
            message += f"; and {len(problems) - 10} more"
    if problems and not synced:
        raise StepError(message + ".")
    return message + "."


# Interface relationships


class _RelationshipSync:
    """Link the LAG, parent and bridge edges of one device with the interface sync view's relationship writer."""

    def __init__(self, ctx, device, ports):
        from netbox_librenms_plugin.utils import normalize_librenms_port_id

        self.ctx = ctx
        self.device = device
        self.ports_by_id = {}
        for port in ports:
            port_id = normalize_librenms_port_id(port.get("port_id"))
            if port_id is not None:
                self.ports_by_id[port_id] = port
        self.changeable = ctx.restrict(Interface, "change")
        self.linked = {"lag": 0, "parent": 0, "bridge": 0}
        self.unchanged = 0
        self.skipped = []
        self.failed = []

    def edge(self, field, source_port_id, related_port_id):
        """Set ``source.<field> = related`` for one LibreNMS edge, in its own savepoint."""
        from django.core.exceptions import ValidationError
        from django.db import IntegrityError

        from netbox_librenms_plugin.utils import validation_error_detail
        from netbox_librenms_plugin.views.sync.interfaces import (
            _apply_interface_relationship,
            _lag_aggregate_needs_promotion,
            _parent_child_needs_promotion,
        )

        source = self.ctx.interface_for_port(self.device, source_port_id)
        related = self.ctx.interface_for_port(self.device, related_port_id)
        if source is None or related is None:
            missing = source_port_id if source is None else related_port_id
            self.skipped.append(
                f"{field} link {source_port_id} → {related_port_id}: LibreNMS port {missing} is not in NetBox "
                "(sync interfaces first)"
            )
            return
        label = f"{source.name} → {related.name}"
        try:
            with transaction.atomic():
                source = Interface.objects.select_for_update().get(pk=source.pk)
                related = Interface.objects.select_for_update().get(pk=related.pk)
                promote_related = field == "lag" and _lag_aggregate_needs_promotion(related)
                promote_source = field == "parent" and _parent_child_needs_promotion(source)
                if getattr(source, f"{field}_id") == related.pk and not promote_related and not promote_source:
                    self.unchanged += 1
                    return
                if not self.allowed(source, related, promote_related, label):
                    return
                if not self.rules_allow(field, source_port_id, source, related_port_id, related, label):
                    return
                _apply_interface_relationship(source, field, related, *self.preparations(field))
            self.linked[field] += 1
        except (ValidationError, IntegrityError) as exc:
            detail = validation_error_detail(exc) if isinstance(exc, ValidationError) else exc
            self.failed.append(f"{label}: {detail}")

    def allowed(self, source, related, promote_related, label):
        if not self.changeable.filter(pk=source.pk).exists():
            self.failed.append(f"{label}: you may not change {source.name}")
            return False
        if promote_related and not self.changeable.filter(pk=related.pk).exists():
            self.failed.append(f"{label}: you may not change {related.name}")
            return False
        return True

    def rules_allow(self, field, source_port_id, source, related_port_id, related, label):
        """Apply the interface rules to both ends, as the interface sync view does."""
        from netbox_librenms_plugin.views.sync.interfaces import (
            _lag_aggregate_needs_promotion,
            _parent_child_needs_promotion,
            _promotion_conflict,
            _relationship_decisions,
        )

        decisions, blocked, reason = _relationship_decisions(
            self.ctx.rules,
            (self.ports_by_id.get(source_port_id), source),
            (self.ports_by_id.get(related_port_id), related),
        )
        if decisions is None:
            self.skipped.append(f"{label}: {blocked.name} {reason}")
            return False
        conflict = None
        if field == "lag" and _lag_aggregate_needs_promotion(related):
            conflict = _promotion_conflict(decisions[1], "lag")
        elif field == "parent" and _parent_child_needs_promotion(source):
            conflict = _promotion_conflict(decisions[0], "virtual")
        if conflict:
            self.skipped.append(f"{label}: {conflict}")
            return False
        return True

    @staticmethod
    def preparations(field):
        """Return the ``(prepare_related, prepare_source)`` hooks of the interface sync view for *field*."""
        from netbox_librenms_plugin.views.sync.interfaces import _promote_lag_aggregate, _promote_parent_child

        if field == "lag":
            return (lambda aggregate: _promote_lag_aggregate(aggregate, with_restore=False)), None
        if field == "parent":
            return None, (lambda child: _promote_parent_child(child, with_restore=False))
        return None, None


def sync_interface_relationships(ctx, device):
    ports = ctx.ports(device)
    port_stack = ctx.port_stack(device)
    if not ports or not port_stack:
        raise StepSkipped("LibreNMS reports no port stack for this device.")
    ctx.require(Interface, "change")
    try:
        device_os = _clean_text(ctx.device_info(device).get("os"))
    except StepError:
        # A blank OS scopes the name patterns to none, as the interfaces tab does.
        device_os = ""
    relationships = ctx.api.resolve_port_relationships(
        ports,
        port_stack,
        device_os=device_os,
        interface_name_field=ctx.interface_name_field(device),
    )
    edges = [
        (field, source, related)
        for field, key in (("lag", "lag_members"), ("parent", "sub_interfaces"), ("bridge", "bridge_members"))
        for source, related in (relationships.get(key) or {}).items()
    ]
    if not edges:
        raise StepSkipped("The LibreNMS port stack has no LAG, sub-interface or bridge relationships.")

    sync = _RelationshipSync(ctx, device, ports)
    for field, source, related in edges:
        sync.edge(field, source, related)
    linked = sync.linked
    message = (
        f"{linked['lag']} LAG members, {linked['parent']} sub-interfaces, {linked['bridge']} bridge members linked, "
        f"{sync.unchanged} already in place"
    )
    if sync.skipped:
        message += _problem_list(sync.skipped)
    if sync.failed:
        message += f"; {len(sync.failed)} refused: " + "; ".join(sync.failed[:10])
        if len(sync.failed) > 10:
            message += f"; and {len(sync.failed) - 10} more"
    if not any(linked.values()) and not sync.unchanged:
        if sync.failed:
            raise StepError(message + ".")
        raise StepSkipped(message + ".")
    return message + "."


# VLANs


def _resolve_vlan(device, vid):
    """Return the VLAN a device's interface should use for *vid*: its site's VLAN, else a global one."""
    if device.site_id:
        vlan = VLAN.objects.filter(site_id=device.site_id, vid=vid).first()
        if vlan is not None:
            return vlan
    return VLAN.objects.filter(site__isnull=True, group__isnull=True, vid=vid).first()


def _create_missing_vlans(ctx, device, vlans):
    """Create the device's LibreNMS VLANs that neither its site nor the global VLANs have; return their VIDs."""
    from ipam.choices import VLANStatusChoices

    created = []
    for row in vlans:
        try:
            vid = int(row.get("vlan_vlan"))
        except (TypeError, ValueError):
            continue
        if not 1 <= vid <= 4094 or _resolve_vlan(device, vid) is not None:
            continue
        ctx.require(VLAN, "add")
        name = (_clean_text(row.get("vlan_name")) or f"VLAN{vid}")[:64]
        vlan = VLAN(vid=vid, name=name, site=device.site, status=VLANStatusChoices.STATUS_ACTIVE)
        vlan.full_clean()
        vlan.save()
        created.append(str(vid))
    return created


def _apply_port_vlans(ctx, device, interface, vlan_data, missing_vids):
    """Set the 802.1Q mode and VLANs of one interface from its port; return whether it changed."""
    from dcim.choices import InterfaceModeChoices

    untagged = None
    if vlan_data.get("untagged_vlan"):
        untagged = _resolve_vlan(device, vlan_data["untagged_vlan"])
        if untagged is None:
            missing_vids.add(vlan_data["untagged_vlan"])
    mode = InterfaceModeChoices.MODE_TAGGED if vlan_data["mode"] == "tagged" else InterfaceModeChoices.MODE_ACCESS
    tagged = []
    if mode == InterfaceModeChoices.MODE_TAGGED:
        for vid in vlan_data.get("tagged_vlans") or []:
            vlan = _resolve_vlan(device, vid)
            if vlan is None:
                missing_vids.add(vid)
            else:
                tagged.append(vlan)

    row = Interface.objects.select_for_update().get(pk=interface.pk)
    current_tagged = set(row.tagged_vlans.values_list("pk", flat=True))
    wanted_tagged = {vlan.pk for vlan in tagged}
    untagged_pk = untagged.pk if untagged else None
    if row.mode == mode and row.untagged_vlan_id == untagged_pk and current_tagged == wanted_tagged:
        return False
    row.snapshot()
    row.mode = mode
    row.untagged_vlan = untagged
    row.full_clean()
    row.save()
    if current_tagged != wanted_tagged:
        # The save used the before-state; the tagged VLANs change gets its own.
        row.snapshot()
        row.tagged_vlans.set(tagged)
    return True


def sync_vlans(ctx, device):
    from netbox_librenms_plugin.utils import normalize_librenms_port_id

    librenms_id = ctx.librenms_id(device)
    success, vlans = ctx.api.get_device_vlans(librenms_id)
    if not success:
        raise StepError(f"Could not read the VLANs from LibreNMS: {vlans}")
    created = _create_missing_vlans(ctx, device, vlans)

    name_field = ctx.interface_name_field(device)
    changeable = ctx.restrict(Interface, "change")
    changed_interfaces = 0
    missing_vids = set()
    for port in ctx.ports(device):
        port_id = normalize_librenms_port_id(port.get("port_id"))
        vlan_data = ctx.api.parse_port_vlan_data(port, name_field)
        if port_id is None or not vlan_data.get("mode"):
            continue
        interface = ctx.interface_for_port(device, port_id)
        if interface is None or not changeable.filter(pk=interface.pk).exists():
            continue
        if _apply_port_vlans(ctx, device, interface, vlan_data, missing_vids):
            changed_interfaces += 1

    parts = [f"{len(vlans)} VLANs in LibreNMS"]
    parts.append(f"{len(created)} created" + (f" ({', '.join(created[:20])})" if created else ""))
    parts.append(f"{changed_interfaces} interfaces updated")
    if missing_vids:
        parts.append("VLANs not found in NetBox: " + ", ".join(str(v) for v in sorted(missing_vids)[:20]))
    return "; ".join(parts) + "."


# IP addresses


def _skip_address(address):
    ip = address.ip
    return ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast


def sync_ip_addresses(ctx, device):
    from netbox_librenms_plugin.ip_addressing import parse_librenms_ip_entry
    from netbox_librenms_plugin.utils import normalize_librenms_port_id

    librenms_id = ctx.librenms_id(device)
    success, rows = ctx.api.get_device_ips(librenms_id)
    if not success:
        raise StepError(f"Could not read the IP addresses from LibreNMS: {rows}")
    if not rows:
        raise StepSkipped("LibreNMS reports no IP addresses for this device.")

    created = assigned = unchanged = 0
    problems = []
    for row in rows:
        try:
            address = parse_librenms_ip_entry(row)
        except ValueError:
            continue
        if _skip_address(address):
            continue
        port_id = normalize_librenms_port_id(row.get("port_id"))
        interface = ctx.interface_for_port(device, port_id) if port_id is not None else None
        if interface is None:
            problems.append(f"{address}: no NetBox interface for LibreNMS port {port_id} (sync interfaces first)")
            continue
        existing = IPAddress.objects.filter(address__net_host=str(address.ip), vrf__isnull=True).order_by("pk")
        matches = list(existing[:2])
        if len(matches) > 1:
            problems.append(f"{address}: more than one IP address in NetBox")
            continue
        try:
            with transaction.atomic():
                if not matches:
                    ctx.require(IPAddress, "add")
                    ip = IPAddress(address=str(address), assigned_object=interface)
                    ip.full_clean()
                    ip.save()
                    created += 1
                    continue
                ip = IPAddress.objects.select_for_update().get(pk=matches[0].pk)
                if (
                    ip.assigned_object_id == interface.pk
                    and ip.assigned_object_type_id
                    and isinstance(ip.assigned_object, Interface)
                ):
                    unchanged += 1
                    continue
                if ip.assigned_object_id is not None:
                    problems.append(f"{address}: already assigned to {ip.assigned_object}")
                    continue
                ctx.require(IPAddress, "change", ip)
                ip.snapshot()
                ip.assigned_object = interface
                ip.full_clean()
                ip.save()
                assigned += 1
        except StepError:
            raise
        except Exception as exc:
            problems.append(f"{address}: {exc}")

    message = f"{created} created, {assigned} assigned, {unchanged} already in place"
    if problems:
        message += f"; {len(problems)} skipped: " + "; ".join(problems[:10])
        if len(problems) > 10:
            message += f"; and {len(problems) - 10} more"
    return message + "."


def sync_primary_ip(ctx, device):
    from netbox_librenms_plugin.ip_addressing import parse_host_address

    polled = _clean_text(ctx.device_info(device).get("ip"))
    if not polled:
        raise StepSkipped("LibreNMS reports no polling address for this device.")
    try:
        host = parse_host_address(polled)
    except ValueError:
        raise StepSkipped(f"LibreNMS polls the device on '{polled}', which is not an IP address.") from None
    member_ids = [member.pk for member in ctx.members(device)]
    candidates = list(
        IPAddress.objects.filter(
            address__net_host=str(host),
            assigned_object_type__app_label="dcim",
            assigned_object_type__model="interface",
            assigned_object_id__in=Interface.objects.filter(device_id__in=member_ids).values("pk"),
        )[:2]
    )
    if not candidates:
        raise StepSkipped(f"{host} is not assigned to an interface of this device (sync IP addresses first).")
    if len(candidates) > 1:
        raise StepError(f"{host} is assigned to more than one interface of this device.")
    field = "primary_ip4" if host.version == 4 else "primary_ip6"
    ctx.require(Device, "change", device)
    locked = ctx.lock_device(device)
    if getattr(locked, f"{field}_id") == candidates[0].pk:
        return "Primary IP is already up to date."
    before = getattr(locked, field)
    locked.snapshot()
    setattr(locked, field, candidates[0])
    locked.full_clean()
    locked.save()
    return f"Primary IPv{host.version}: {before or 'empty'} → {candidates[0]}."


# Cables


def _problem_list(problems):
    text = f"; {len(problems)} skipped: " + "; ".join(problems[:10])
    if len(problems) > 10:
        text += f"; and {len(problems) - 10} more"
    return text


class _CableSync:
    """Connect the links of one device: each link is created, already connected, or a problem."""

    def __init__(self, ctx, device):
        from netbox_librenms_plugin.utils import get_cable_sync_settings

        self.ctx = ctx
        self.device = device
        self.settings = get_cable_sync_settings()
        self.tag = None
        self.created = 0
        self.connected = 0
        self.problems = []

    def remote_interface(self, remote_port):
        from netbox_librenms_plugin.utils import find_interface_by_librenms_port_id

        try:
            remote = find_interface_by_librenms_port_id(remote_port, self.ctx.server_key)
        except Exception:
            return None
        return remote if isinstance(remote, Interface) else None

    def link(self, link):
        from netbox_librenms_plugin.utils import coerce_librenms_id

        local_port = coerce_librenms_id(link.get("local_port_id"))
        remote_port = coerce_librenms_id(link.get("remote_port_id"))
        remote_label = f"{link.get('remote_hostname') or '?'} {link.get('remote_port') or ''}".strip()
        if local_port is None:
            return
        local = self.ctx.interface_for_port(self.device, local_port)
        if local is None:
            self.problems.append(f"port {local_port}: no NetBox interface")
            return
        remote = self.remote_interface(remote_port) if remote_port is not None else None
        if remote is None:
            self.problems.append(f"{local.name} → {remote_label}: the remote interface is not in NetBox")
            return
        local = Interface.objects.select_for_update().get(pk=local.pk)
        remote = Interface.objects.select_for_update().get(pk=remote.pk)
        label = f"{local.name} → {remote.device}:{remote.name}"
        if local.cable_id and local.cable_id == remote.cable_id:
            self.connected += 1
        elif local.cable_id or remote.cable_id:
            self.problems.append(f"{label}: an end already has another cable")
        elif local.mark_connected or remote.mark_connected:
            self.problems.append(f"{label}: an end is marked connected without a cable")
        else:
            self.connect(local, remote, label)

    def connect(self, local, remote, label):
        from dcim.choices import LinkStatusChoices

        from netbox_librenms_plugin.utils import get_librenms_cable_tag

        try:
            with transaction.atomic():
                if self.tag is None:
                    self.tag = get_librenms_cable_tag(sync_settings=self.settings)
                cable = Cable(
                    a_terminations=[local],
                    b_terminations=[remote],
                    status=LinkStatusChoices.STATUS_CONNECTED,
                    description=(self.settings.cable_sync_description or "")[:200],
                )
                cable.full_clean()
                cable.save()
                cable.snapshot()
                cable.tags.add(self.tag)
            self.created += 1
        except Exception as exc:
            self.problems.append(f"{label}: {exc}")


def sync_cables(ctx, device):
    librenms_id = ctx.librenms_id(device)
    success, data = ctx.api.get_device_links(librenms_id)
    links = data.get("links") if success and isinstance(data, dict) else None
    if not isinstance(links, list):
        raise StepError(f"Could not read the links from LibreNMS: {data}")
    if not links:
        raise StepSkipped("LibreNMS reports no LLDP/CDP links for this device.")
    ctx.require(Cable, "add")

    cables = _CableSync(ctx, device)
    for link in links:
        if isinstance(link, dict):
            cables.link(link)
    message = f"{cables.created} cables created, {cables.connected} already connected"
    if cables.problems:
        message += _problem_list(cables.problems)
    return message + "."


DEVICE_STEPS = {
    catalog.STEP_SERIAL: sync_serial,
    catalog.STEP_PLATFORM: sync_platform,
    catalog.STEP_DEVICE_TYPE: sync_device_type,
    catalog.STEP_INTERFACES: sync_interfaces,
    catalog.STEP_INTERFACE_RELATIONSHIPS: sync_interface_relationships,
    catalog.STEP_VLANS: sync_vlans,
    catalog.STEP_IP_ADDRESSES: sync_ip_addresses,
    catalog.STEP_PRIMARY_IP: sync_primary_ip,
    catalog.STEP_CABLES: sync_cables,
}


# Import of new devices (runs once per run)


def librenms_import_candidates(ctx):
    """Return the LibreNMS devices that match the profile's import filters and are not in NetBox yet."""
    from virtualization.models import VirtualMachine

    from netbox_librenms_plugin.utils import find_by_librenms_id

    profile = ctx.profile
    filters = {}
    if profile.librenms_type:
        filters["type"] = profile.librenms_type
    if profile.librenms_os:
        filters["os"] = profile.librenms_os
    success, devices = ctx.api.list_devices(filters)
    if not success:
        raise StepError(f"Could not list the LibreNMS devices: {devices}")

    location_text = profile.librenms_location.strip().lower()
    hostname_text = profile.librenms_hostname.strip().lower()
    candidates = []
    for row in devices:
        if row.get("disabled") in (1, "1", True) or row.get("ignore") in (1, "1", True):
            continue
        if location_text and location_text not in str(row.get("location") or "").lower():
            continue
        names = f"{row.get('hostname') or ''} {row.get('sysName') or ''}".lower()
        if hostname_text and hostname_text not in names:
            continue
        device_id = row.get("device_id")
        if not isinstance(device_id, int):
            continue
        try:
            known = find_by_librenms_id(Device, device_id, ctx.server_key) or find_by_librenms_id(
                VirtualMachine, device_id, ctx.server_key
            )
        except Exception:
            known = True
        if known:
            continue
        candidates.append(row)
    return candidates


def import_devices(ctx):
    """
    Import the LibreNMS devices that match the profile's import filters.

    Returns:
        tuple[dict, list[int]]: The step result, and the pks of the NetBox devices it imported.

    """
    from netbox_librenms_plugin.import_utils import (
        bulk_import_devices_shared,
        classify_bulk_precheck,
        detect_collisions_for_device_ids,
    )
    from netbox_librenms_plugin.models import LibreNMSSettings
    from netbox_librenms_plugin.utils import find_matching_site

    profile = ctx.profile
    if profile.import_role_id is None:
        raise StepError("The profile has no role for new devices.")
    candidates = librenms_import_candidates(ctx)
    names = [str(row.get("sysName") or row.get("hostname") or row.get("device_id")) for row in candidates]
    if not candidates:
        return {"message": "No new LibreNMS devices match the import filters.", "items": []}, []
    if ctx.dry_run:
        return {
            "message": f"{len(candidates)} new devices would be imported.",
            "items": [{"name": name, "status": "planned"} for name in names],
        }, []

    settings = LibreNMSSettings.objects.first()
    sync_options = {
        "sync_interfaces": False,
        "sync_cables": False,
        "vc_detection_enabled": True,
        "use_sysname": settings.use_sysname_default if settings else True,
        "strip_domain": settings.strip_domain_default if settings else False,
    }
    libre_cache = {row["device_id"]: row for row in candidates}
    device_ids = list(libre_cache)
    mappings = {}
    for row in candidates:
        mapping = {"device_role_id": profile.import_role_id}
        if profile.import_site_id and not find_matching_site(row.get("location") or "").get("found"):
            mapping["site_id"] = profile.import_site_id
        mappings[row["device_id"]] = mapping

    collisions, unresolved, stack_ambiguities = detect_collisions_for_device_ids(
        device_ids,
        ctx.api,
        libre_devices_cache=libre_cache,
        sync_options=sync_options,
        job=ctx.job_runner,
        vm_device_ids={},
        user=ctx.user,
    )
    outcome = classify_bulk_precheck(collisions, unresolved, stack_ambiguities, device_ids, {})
    if outcome.blocked:
        raise StepError(outcome.block_message)
    result = bulk_import_devices_shared(
        device_ids=outcome.importable_device_ids,
        server_key=ctx.server_key,
        sync_options=sync_options,
        manual_mappings_per_device=mappings,
        libre_devices_cache=libre_cache,
        job=ctx.job_runner,
        user=ctx.user,
    )
    by_id = {row["device_id"]: str(row.get("sysName") or row.get("hostname")) for row in candidates}
    items = []
    imported_pks = []
    for entry in result.get("success", []):
        obj = entry.get("device")
        if isinstance(obj, Device):
            imported_pks.append(obj.pk)
        items.append({"name": str(obj or by_id.get(entry.get("device_id"))), "status": "imported"})
    for entry in result.get("failed", []):
        items.append(
            {
                "name": by_id.get(entry.get("device_id"), str(entry.get("device_id"))),
                "status": "failed",
                "message": str(entry.get("error", "")),
            }
        )
    for entry in result.get("skipped", []):
        items.append(
            {
                "name": by_id.get(entry.get("device_id"), str(entry.get("device_id"))),
                "status": "skipped",
                "message": str(entry.get("reason", entry.get("error", ""))),
            }
        )
    for device_id in outcome.skipped_ids:
        items.append(
            {"name": by_id.get(device_id, str(device_id)), "status": "skipped", "message": outcome.skip_message}
        )
    message = (
        f"{len(result.get('success', []))} imported, {len(result.get('failed', []))} failed, "
        f"{len(result.get('skipped', [])) + len(outcome.skipped_ids)} skipped."
    )
    return {"message": message, "items": items}, imported_pks
