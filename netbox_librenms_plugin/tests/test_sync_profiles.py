"""Sync profiles: the step catalog, the profile model, the run engine, the schedule and the pages."""

import pytest
from core.choices import JobStatusChoices
from dcim.models import Device, Interface, Platform
from django.core.exceptions import ValidationError
from django.urls import reverse
from ipam.models import IPAddress

from netbox_librenms_plugin.sync_profiles import catalog
from netbox_librenms_plugin.tests.conftest import configured_server_key, make_device, make_superuser


class FakeAPI:
    """A LibreNMS client that answers from dicts, keyed by LibreNMS device id."""

    def __init__(self, server_key, *, info=None, ports=None, ips=None):
        self.server_key = server_key
        self.info = info or {}
        self.ports = ports or {}
        self.ips = ips or {}
        self.vlans = {}
        self.links = {}
        self.calls = []

    def get_device_info(self, device_id, use_cache=True, cache_only=False):
        self.calls.append(("info", device_id))
        if device_id not in self.info:
            return False, None
        return True, self.info[device_id]

    def get_ports(self, device_id, with_vlans=True):
        self.calls.append(("ports", device_id))
        return True, {"ports": self.ports.get(device_id, [])}

    def get_device_ips(self, device_id):
        return True, self.ips.get(device_id, [])

    def get_device_vlans(self, device_id):
        return True, self.vlans.get(device_id, [])

    def get_device_links(self, device_id):
        return True, {"links": self.links.get(device_id, [])}

    def parse_port_vlan_data(self, port, name_field):
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        return LibreNMSAPI.parse_port_vlan_data(self, port, name_field)


def _port(port_id, name, **extra):
    port = {
        "port_id": port_id,
        "ifName": name,
        "ifDescr": name,
        "ifAlias": "",
        "ifType": "ethernetCsmacd",
        "ifSpeed": 1_000_000_000,
        "ifMtu": 1500,
        "ifAdminStatus": "up",
        "ifPhysAddress": "",
    }
    port.update(extra)
    return port


@pytest.fixture
def server_key():
    return configured_server_key()


@pytest.fixture
def fake_api(monkeypatch, server_key):
    """Replace the LibreNMS client the engine builds with one FakeAPI the test fills."""
    api = FakeAPI(server_key)
    monkeypatch.setattr("netbox_librenms_plugin.sync_profiles.engine.build_api", lambda server_key: api)
    return api


def _linked_device(name, librenms_id, server_key):
    device = make_device(name)
    device.custom_field_data["librenms_id"] = {server_key: librenms_id}
    device.save()
    return device


def _profile(server_key, steps, **fields):
    from netbox_librenms_plugin.models import SyncProfile

    profile = SyncProfile(name=fields.pop("name", "profile"), server_key=server_key, steps=steps, **fields)
    profile.full_clean()
    profile.save()
    return profile


def _run(profile, **kwargs):
    from netbox_librenms_plugin.sync_profiles.engine import run_profile

    return run_profile(profile, user=make_superuser(), **kwargs)


def _steps_of(result, device):
    entry = next(row for row in result["devices"] if row["pk"] == device.pk)
    return {step["key"]: step for step in entry["steps"]}


class TestCatalog:
    def test_normalize_keeps_known_distinct_steps_in_order(self):
        assert catalog.normalize_steps(["interfaces", "bogus", "serial", "interfaces"]) == ["interfaces", "serial"]

    def test_normalize_rejects_a_non_list(self):
        assert catalog.normalize_steps("serial") == []


@pytest.mark.django_db
class TestProfileValidation:
    def test_an_unknown_step_is_refused(self, server_key):
        with pytest.raises(ValidationError) as excinfo:
            _profile(server_key, ["serial", "nope"])
        assert "steps" in excinfo.value.message_dict

    def test_at_least_one_step_is_required(self, server_key):
        with pytest.raises(ValidationError):
            _profile(server_key, [])

    def test_the_import_step_needs_a_role(self, server_key):
        with pytest.raises(ValidationError) as excinfo:
            _profile(server_key, ["import_devices"])
        assert "import_role" in excinfo.value.message_dict

    def test_the_interval_has_a_minimum(self, server_key):
        with pytest.raises(ValidationError) as excinfo:
            _profile(server_key, ["serial"], interval=1)
        assert "interval" in excinfo.value.message_dict

    def test_an_unconfigured_server_is_refused(self):
        with pytest.raises(ValidationError) as excinfo:
            _profile("not-a-server", ["serial"])
        assert "server_key" in excinfo.value.message_dict


@pytest.mark.django_db
class TestScope:
    def test_only_devices_linked_to_the_server_are_in_scope(self, server_key):
        from netbox_librenms_plugin.sync_profiles.engine import scoped_devices

        linked = _linked_device("scope-linked", 11, server_key)
        make_device("scope-unlinked")
        other = make_device("scope-other-server")
        other.custom_field_data["librenms_id"] = {"some-other-server": 12}
        other.save()

        devices = scoped_devices(_profile(server_key, ["serial"]), make_superuser())

        assert [device.pk for device in devices] == [linked.pk]

    def test_the_status_filter_narrows_the_scope(self, server_key):
        from netbox_librenms_plugin.sync_profiles.engine import scoped_devices

        active = _linked_device("scope-active", 21, server_key)
        planned = _linked_device("scope-planned", 22, server_key)
        planned.status = "planned"
        planned.save()

        devices = scoped_devices(_profile(server_key, ["serial"], device_statuses=["active"]), make_superuser())

        assert [device.pk for device in devices] == [active.pk]


@pytest.mark.django_db
class TestRun:
    def test_device_fields_are_written(self, server_key, fake_api):
        platform = Platform.objects.create(name="junos", slug="junos")
        device = _linked_device("run-fields", 31, server_key)
        fake_api.info[31] = {"device_id": 31, "serial": " AB123 ", "os": "junos"}

        result = _run(_profile(server_key, ["serial", "platform"]))

        device.refresh_from_db()
        assert (device.serial, device.platform_id) == ("AB123", platform.pk)
        steps = _steps_of(result, device)
        assert steps["serial"]["status"] == "changed"
        assert steps["platform"]["status"] == "changed"
        assert result["summary"]["updated"] >= 1

    def test_a_second_run_reports_no_change(self, server_key, fake_api):
        device = _linked_device("run-again", 32, server_key)
        fake_api.info[32] = {"device_id": 32, "serial": "SER32"}
        profile = _profile(server_key, ["serial"])
        _run(profile)

        result = _run(profile)

        assert _steps_of(result, device)["serial"]["status"] == "ok"

    def test_a_dry_run_reports_the_change_and_saves_nothing(self, server_key, fake_api):
        device = _linked_device("run-dry", 33, server_key)
        fake_api.info[33] = {"device_id": 33, "serial": "SER33"}

        result = _run(_profile(server_key, ["serial"]), dry_run=True)

        device.refresh_from_db()
        assert device.serial == ""
        assert result["dry_run"] is True
        assert _steps_of(result, device)["serial"]["status"] == "changed"

    def test_a_dry_run_lets_later_steps_see_earlier_writes(self, server_key, fake_api):
        device = _linked_device("run-dry-chain", 44, server_key)
        fake_api.ports[44] = [_port(4401, "eth0")]
        fake_api.ips[44] = [{"port_id": 4401, "ipv4_address": "10.44.0.1", "ipv4_prefixlen": 24}]

        result = _run(_profile(server_key, ["interfaces", "ip_addresses"]), dry_run=True)

        steps = _steps_of(result, device)
        assert steps["ip_addresses"]["status"] == "changed"
        assert steps["ip_addresses"]["changes"]["created"] == 1
        assert not Interface.objects.filter(device=device).exists()
        assert not IPAddress.objects.filter(address="10.44.0.1/24").exists()

    def test_a_missing_value_is_a_skip_not_an_error(self, server_key, fake_api):
        device = _linked_device("run-skip", 34, server_key)
        fake_api.info[34] = {"device_id": 34, "serial": "-", "os": ""}

        result = _run(_profile(server_key, ["serial", "platform"]))

        steps = _steps_of(result, device)
        assert steps["serial"]["status"] == "skipped"
        assert steps["platform"]["status"] == "skipped"

    def test_interfaces_then_ip_addresses_then_primary_ip(self, server_key, fake_api):
        device = _linked_device("run-chain", 35, server_key)
        fake_api.info[35] = {"device_id": 35, "ip": "10.35.0.1"}
        fake_api.ports[35] = [_port(3501, "ge-0/0/0"), _port(3502, "ge-0/0/1", ifAdminStatus="down")]
        fake_api.ips[35] = [
            {"port_id": 3501, "ipv4_address": "10.35.0.1", "ipv4_prefixlen": 24},
            {"port_id": 3502, "ipv4_address": "127.0.0.1", "ipv4_prefixlen": 8},
        ]

        result = _run(_profile(server_key, ["interfaces", "ip_addresses", "primary_ip"]))

        steps = _steps_of(result, device)
        assert [step["status"] for step in steps.values()] == ["changed", "changed", "changed"]
        interfaces = {interface.name: interface for interface in Interface.objects.filter(device=device)}
        assert set(interfaces) == {"ge-0/0/0", "ge-0/0/1"}
        assert interfaces["ge-0/0/1"].enabled is False
        ip = IPAddress.objects.get(address="10.35.0.1/24")
        assert ip.assigned_object == interfaces["ge-0/0/0"]
        assert not IPAddress.objects.filter(address__net_host="127.0.0.1").exists()
        device.refresh_from_db()
        assert device.primary_ip4_id == ip.pk

    def test_vlans_are_created_in_the_site_and_set_on_interfaces(self, server_key, fake_api):
        from ipam.models import VLAN

        device = _linked_device("run-vlans", 41, server_key)
        fake_api.ports[41] = [
            _port(4101, "ge-0/0/1", ifVlan="10", ifTrunk=None, vlans=[{"vlan": 10, "untagged": 1}]),
            _port(
                4102,
                "ge-0/0/2",
                ifVlan="10",
                ifTrunk="dot1Q",
                vlans=[{"vlan": 10, "untagged": 1}, {"vlan": 20, "untagged": 0}],
            ),
        ]
        fake_api.vlans[41] = [
            {"vlan_vlan": 10, "vlan_name": "USERS"},
            {"vlan_vlan": 20, "vlan_name": "VOICE"},
        ]

        result = _run(_profile(server_key, ["interfaces", "vlans"]))

        assert _steps_of(result, device)["vlans"]["status"] == "changed"
        users = VLAN.objects.get(site=device.site, vid=10)
        voice = VLAN.objects.get(site=device.site, vid=20)
        assert users.name == "USERS"
        access = Interface.objects.get(device=device, name="ge-0/0/1")
        trunk = Interface.objects.get(device=device, name="ge-0/0/2")
        assert (access.mode, access.untagged_vlan_id) == ("access", users.pk)
        assert (trunk.mode, trunk.untagged_vlan_id) == ("tagged", users.pk)
        assert list(trunk.tagged_vlans.values_list("pk", flat=True)) == [voice.pk]

    def test_cables_connect_two_free_interfaces_once(self, server_key, fake_api):
        from dcim.models import Cable

        left = _linked_device("run-cable-a", 42, server_key)
        right = _linked_device("run-cable-b", 43, server_key)
        fake_api.ports[42] = [_port(4201, "xe-0/0/0")]
        fake_api.ports[43] = [_port(4301, "Ethernet1")]
        fake_api.links[42] = [{"local_port_id": 4201, "remote_port_id": 4301, "remote_hostname": "b"}]
        fake_api.links[43] = [{"local_port_id": 4301, "remote_port_id": 4201, "remote_hostname": "a"}]

        result = _run(_profile(server_key, ["interfaces", "cables"]))

        assert Cable.objects.count() == 1
        local = Interface.objects.get(device=left, name="xe-0/0/0")
        remote = Interface.objects.get(device=right, name="Ethernet1")
        assert local.cable_id == remote.cable_id is not None
        assert _steps_of(result, left)["cables"]["status"] == "changed"
        assert "1 already connected" in _steps_of(result, right)["cables"]["message"]

    def test_steps_run_in_the_profile_order(self, server_key, fake_api):
        """IP addresses before interfaces finds no interface, so the order matters."""
        device = _linked_device("run-order", 36, server_key)
        fake_api.ports[36] = [_port(3601, "eth0")]
        fake_api.ips[36] = [{"port_id": 3601, "ipv4_address": "10.36.0.1", "ipv4_prefixlen": 24}]

        result = _run(_profile(server_key, ["ip_addresses", "interfaces"]))

        entry = next(row for row in result["devices"] if row["pk"] == device.pk)
        assert [step["key"] for step in entry["steps"]] == ["ip_addresses", "interfaces"]
        assert not IPAddress.objects.filter(address="10.36.0.1/24").exists()
        assert Interface.objects.filter(device=device, name="eth0").exists()

    def test_stop_on_error_skips_the_device_remaining_steps(self, server_key, fake_api):
        device = _linked_device("run-stop", 37, server_key)
        # No device info: the serial step fails with an error.

        result = _run(_profile(server_key, ["serial", "interfaces"], continue_on_error=False))

        entry = next(row for row in result["devices"] if row["pk"] == device.pk)
        assert [step["key"] for step in entry["steps"]] == ["serial"]
        assert entry["steps"][0]["status"] == "error"
        assert result["summary"]["devices_with_errors"] == 1

    def test_a_limited_run_syncs_only_the_chosen_devices(self, server_key, fake_api):
        chosen = _linked_device("run-chosen", 38, server_key)
        _linked_device("run-not-chosen", 39, server_key)
        fake_api.info[38] = {"device_id": 38, "serial": "S38"}
        fake_api.info[39] = {"device_id": 39, "serial": "S39"}

        result = _run(_profile(server_key, ["serial"]), device_pks=[chosen.pk])

        assert [row["pk"] for row in result["devices"]] == [chosen.pk]
        assert Device.objects.get(name="run-not-chosen").serial == ""

    def test_a_user_without_change_permission_writes_nothing(self, server_key, fake_api, django_user_model):
        from netbox_librenms_plugin.sync_profiles.engine import run_profile

        device = _linked_device("run-perm", 40, server_key)
        fake_api.info[40] = {"device_id": 40, "serial": "S40"}
        viewer = django_user_model.objects.create(username="sync-viewer")

        result = run_profile(_profile(server_key, ["serial"]), user=viewer, device_pks=None)

        # A user without view permission sees no devices at all.
        assert result["devices"] == []
        device.refresh_from_db()
        assert device.serial == ""


@pytest.mark.django_db
class TestSchedule:
    def _request(self, rf):
        request = rf.post("/")
        request.user = make_superuser()
        request.id = "test-request"
        return request

    def test_an_interval_schedules_one_job_and_a_change_replaces_it(self, server_key, rf):
        from netbox_librenms_plugin.sync_profiles import scheduling

        profile = _profile(server_key, ["serial"], interval=60)
        first = scheduling.apply_schedule(profile, self._request(rf))
        assert first.status == JobStatusChoices.STATUS_SCHEDULED
        assert first.interval == 60

        profile.interval = 30
        profile.save()
        second = scheduling.apply_schedule(profile, self._request(rf))

        assert list(scheduling.profile_jobs(profile).values_list("pk", flat=True)) == [second.pk]
        assert scheduling.scheduled_job(profile).pk == second.pk
        assert profile.next_run == second.scheduled

    def test_a_disabled_profile_has_no_schedule(self, server_key, rf):
        from netbox_librenms_plugin.sync_profiles import scheduling

        profile = _profile(server_key, ["serial"], interval=60)
        scheduling.apply_schedule(profile, self._request(rf))
        profile.enabled = False
        profile.save()

        assert scheduling.apply_schedule(profile, self._request(rf)) is None
        assert not scheduling.profile_jobs(profile).exists()

    def test_deleting_a_profile_removes_its_waiting_jobs(self, server_key, rf):
        from core.models import Job

        from netbox_librenms_plugin.sync_profiles import scheduling

        profile = _profile(server_key, ["serial"], interval=60)
        job = scheduling.apply_schedule(profile, self._request(rf))

        profile.delete()

        assert not Job.objects.filter(pk=job.pk).exists()


@pytest.mark.django_db
class TestPages:
    @pytest.fixture
    def client_su(self, client):
        client.force_login(make_superuser())
        return client

    def test_list_detail_and_dialogs_render(self, client_su, server_key):
        profile = _profile(server_key, ["serial", "interfaces"], name="pages")

        for url in (
            reverse("plugins:netbox_librenms_plugin:syncprofile_list"),
            reverse("plugins:netbox_librenms_plugin:syncprofile", args=[profile.pk]),
            reverse("plugins:netbox_librenms_plugin:syncprofile_add"),
            reverse("plugins:netbox_librenms_plugin:syncprofile_edit", args=[profile.pk]),
            reverse("plugins:netbox_librenms_plugin:syncprofile_run_now", args=[profile.pk]),
            reverse("plugins:netbox_librenms_plugin:syncprofile_schedule", args=[profile.pk]),
            reverse("plugins:netbox_librenms_plugin:syncprofile_devices", args=[profile.pk]),
        ):
            response = client_su.get(url)
            assert response.status_code == 200, url

    def test_the_edit_form_keeps_the_dragged_order(self, client_su, server_key):
        from netbox_librenms_plugin.models import SyncProfile

        response = client_su.post(
            reverse("plugins:netbox_librenms_plugin:syncprofile_add"),
            {
                "name": "ordered",
                "server_key": server_key,
                "enabled": "on",
                "continue_on_error": "on",
                "steps": ["ip_addresses", "serial", "interfaces"],
            },
        )

        assert response.status_code == 302, response.content.decode()[:2000]
        assert SyncProfile.objects.get(name="ordered").steps == ["ip_addresses", "serial", "interfaces"]

    def test_run_now_queues_a_job_and_opens_its_page(self, client_su, server_key):
        from netbox_librenms_plugin.sync_profiles import scheduling

        profile = _profile(server_key, ["serial"], name="run-now")

        response = client_su.post(
            reverse("plugins:netbox_librenms_plugin:syncprofile_run_now", args=[profile.pk]),
            {"dry_run": "on"},
            HTTP_HX_REQUEST="true",
        )

        job = scheduling.profile_jobs(profile).get()
        assert response.status_code == 204
        assert response["HX-Redirect"] == reverse(
            "plugins:netbox_librenms_plugin:syncprofile_run", args=[profile.pk, job.pk]
        )
        assert job.status == JobStatusChoices.STATUS_PENDING

        page = client_su.get(response["HX-Redirect"])
        assert page.status_code == 200
        assert b"every 3s" in page.content

    def test_a_finished_run_shows_its_devices(self, client_su, server_key):

        profile = _profile(server_key, ["serial"], name="finished")
        from core.models import Job, ObjectType

        job = Job.objects.create(
            object_type=ObjectType.objects.get_for_model(profile),
            object_id=profile.pk,
            name="LibreNMS Sync Profile",
            status=JobStatusChoices.STATUS_COMPLETED,
            job_id="00000000-0000-0000-0000-000000000001",
            log_entries=[{"level": "warning", "message": "shown-log-line", "timestamp": "2026-10-20T08:58:41+00:00"}],
            data={
                "dry_run": False,
                "summary": {"devices": 1, "created": 0, "updated": 1, "ok": 0, "skipped": 0, "error": 0, "changed": 1},
                "devices": [
                    {
                        "pk": 1,
                        "name": "shown-device",
                        "url": "/dcim/devices/1/",
                        "steps": [{"key": "serial", "label": "Serial number", "status": "changed", "message": "x"}],
                    }
                ],
            },
        )

        for part in ("", "?part=modal", "?part=results"):
            page = client_su.get(
                reverse("plugins:netbox_librenms_plugin:syncprofile_run", args=[profile.pk, job.pk]) + part
            )
            assert page.status_code == 200
            assert b"shown-device" in page.content
            assert b"every 3s" not in page.content
            assert b"shown-log-line" in page.content

        # NetBox's own job page links to the profile's Jobs view.
        assert client_su.get(reverse("core:job", args=[job.pk])).status_code == 200
        assert (
            client_su.get(reverse("plugins:netbox_librenms_plugin:syncprofile_jobs", args=[profile.pk])).status_code
            == 200
        )


@pytest.mark.django_db
class TestJob:
    def _request(self, rf, user):
        from utilities.request import copy_safe_request

        request = rf.post("/")
        request.user = user
        request.id = "00000000-0000-0000-0000-00000000abcd"
        return copy_safe_request(request)

    def _run_scheduled(self, profile, user, rf):
        """Queue a scheduled run as the schedule would, then run it the way the worker does."""
        from django.utils import timezone

        from netbox_librenms_plugin.jobs import SyncProfileJob

        request = self._request(rf, user)
        job = SyncProfileJob.enqueue(
            instance=profile,
            user=user,
            schedule_at=timezone.now(),
            interval=60,
            request=request,
            dry_run=False,
            device_pks=[],
        )
        SyncProfileJob.handle(job, request=request, dry_run=False, device_pks=[])
        return job

    def test_a_job_run_writes_the_change_log_as_the_user(self, server_key, fake_api, rf):
        from core.models import ObjectChange

        from netbox_librenms_plugin.jobs import SyncProfileJob

        user = make_superuser()
        device = _linked_device("job-run", 51, server_key)
        fake_api.info[51] = {"device_id": 51, "serial": "JOB51"}
        profile = _profile(server_key, ["serial"], name="job")

        job = SyncProfileJob.enqueue(
            instance=profile, user=user, immediate=True, request=self._request(rf, user), dry_run=False, device_pks=[]
        )

        job.refresh_from_db()
        device.refresh_from_db()
        assert job.status == JobStatusChoices.STATUS_COMPLETED, job.error
        assert device.serial == "JOB51"
        assert job.data["summary"]["updated"] == 1
        assert any("Running sync profile" in entry["message"] for entry in job.log_entries)
        change = ObjectChange.objects.filter(changed_object_id=device.pk, changed_object_type__model="device").latest(
            "time"
        )
        assert change.user == user
        assert change.prechange_data["serial"] == ""

    def test_a_scheduled_run_queues_its_next_run(self, server_key, fake_api, rf):
        from netbox_librenms_plugin.sync_profiles import scheduling

        user = make_superuser()
        profile = _profile(server_key, ["serial"], name="job-scheduled", interval=60)

        job = self._run_scheduled(profile, user, rf)

        next_job = scheduling.scheduled_job(profile)
        assert next_job is not None and next_job.pk != job.pk
        assert next_job.interval == 60

    def test_a_run_of_a_removed_schedule_does_not_queue_another(self, server_key, fake_api, rf):
        from netbox_librenms_plugin.sync_profiles import scheduling

        user = make_superuser()
        profile = _profile(server_key, ["serial"], name="job-unscheduled")  # no interval any more

        self._run_scheduled(profile, user, rf)

        assert scheduling.scheduled_job(profile) is None

    def test_a_disabled_profile_is_not_run(self, server_key, fake_api, rf):
        from netbox_librenms_plugin.jobs import SyncProfileJob

        user = make_superuser()
        device = _linked_device("job-disabled", 52, server_key)
        fake_api.info[52] = {"device_id": 52, "serial": "JOB52"}
        profile = _profile(server_key, ["serial"], name="job-disabled", enabled=False)

        job = SyncProfileJob.enqueue(
            instance=profile, user=user, immediate=True, request=self._request(rf, user), dry_run=False, device_pks=[]
        )

        job.refresh_from_db()
        device.refresh_from_db()
        assert device.serial == ""
        assert job.data["skipped"] == "The profile is disabled."


@pytest.mark.django_db
def test_a_running_job_shows_its_live_log(client, server_key):
    from core.models import Job, ObjectType
    from django.utils import timezone

    from netbox_librenms_plugin.sync_profiles import scheduling

    client.force_login(make_superuser())
    profile = _profile(server_key, ["serial"], name="live-log")
    job = Job.objects.create(
        object_type=ObjectType.objects.get_for_model(profile),
        object_id=profile.pk,
        name="LibreNMS Sync Profile",
        status=JobStatusChoices.STATUS_RUNNING,
        job_id="00000000-0000-0000-0000-000000000002",
    )
    scheduling.append_live_log(job, {"level": "info", "message": "live-line", "timestamp": timezone.now()})

    page = client.get(
        reverse("plugins:netbox_librenms_plugin:syncprofile_run", args=[profile.pk, job.pk]) + "?part=results"
    )

    assert b"live-line" in page.content
    assert b"every 3s" in page.content
