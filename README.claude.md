# Sync profiles: scheduled and on-demand LibreNMS → NetBox sync

This fork adds **sync profiles** to the NetBox LibreNMS plugin. A sync profile says which
NetBox devices to sync from LibreNMS, which objects to sync for them, in which order, and when:
on demand (**Run now**, **Dry run**) or on a schedule (every N minutes). Runs are NetBox
background jobs, and each run has a result page that shows, device by device and step by step,
what changed.

This file documents only the fork's additions. The upstream documentation (`README.md`,
`docs/`) is unchanged.

## Where to find it

*LibreNMS → Sync → Sync Profiles* in the NetBox menu, at `/plugins/librenms_plugin/sync-profiles/`.

| Page | What it does |
| --- | --- |
| Sync Profiles (list) | All profiles with their steps, schedule, next run and last run status. |
| Add / Edit | The profile form. *Objects to sync* is a list of every step: tick the steps to run and drag them (or use the arrows) into the order to run them. |
| Profile page | The step pipeline, the device scope, the schedule, and the recent runs. Its buttons open dialogs. |
| **Run now** dialog | Queue a run: a real run or a dry run, for every device of the scope or only the devices picked in the dialog. |
| **Dry run** dialog | The same dialog with *Dry run* ticked. |
| **Schedule** dialog | Turn the schedule on or off, pick an interval (quick picks: 15 min, 1 hour, 6 hours, 12 hours, 1 day) and the first run. |
| **Devices in scope** dialog | The devices the profile syncs now. |
| Run result | Summary counters, the import result, and a device list that can be filtered (All / Changed / Errors / No change / Skipped). Each device expands to show each step's result and the objects it created or updated. While the run is queued or running, the page reloads its result part every 3 seconds and shows a progress bar (step, device, count). A quick view of each run also opens in a dialog from the profile page. |

## Steps

Each step can be ticked once; the order of the ticked steps is the run order.

| Step | What it syncs |
| --- | --- |
| Import new devices | Creates NetBox devices for LibreNMS devices that match the import filters (type, OS, location contains, hostname contains), are not disabled or ignored in LibreNMS, and are not in NetBox yet. Uses the plugin's own import code with its collision checks, the role set on the profile, and the site that the LibreNMS location matches (or the profile's fallback site). Runs once per run; the devices it imports join the scope of every later step. |
| Serial number | The LibreNMS serial, after the `serial` normalization rules. |
| Platform | The platform that the LibreNMS OS matches (exact name or a Platform mapping). |
| Device type | The device type that the LibreNMS hardware matches (part number, model or a Device type mapping). |
| Interfaces | Creates and updates interfaces from LibreNMS ports with the plugin's interface writer: name, type (interface rules apply, including Ignore rules), speed, MTU, MAC, description, enabled state, and the LibreNMS port binding. Virtual chassis ports go to the member their name points to. |
| VLANs | Creates the device's LibreNMS VLANs in the device's site when neither the site nor the global VLANs (no site, no group) have the VID, and sets each bound interface's 802.1Q mode, untagged VLAN and tagged VLANs. |
| IP addresses | Creates the device's IP addresses (global VRF) on the interfaces bound to their LibreNMS ports, or assigns an existing unassigned address. Loopback, link-local and multicast addresses are skipped; an address assigned elsewhere is reported, not moved. |
| Primary IP | Sets the primary IPv4 or IPv6 address to the address LibreNMS polls the device on, when that address is on one of the device's interfaces. |
| Cables | Creates a cable for each LLDP/CDP link whose two ends are interfaces bound in NetBox and free. The cable gets the plugin's cable-sync tag and description (*Settings → Cable Sync*). Existing cables are never replaced. |

Each step runs for **every** device of the scope before the next step starts, so a step that
links devices (Cables) sees the interfaces that the earlier steps made on all of them. Steps
that depend on others need them earlier in the order: IP addresses need Interfaces, Primary IP
needs IP addresses, Cables need Interfaces on both ends.

A step ends as **changed** (it wrote something), **ok** (nothing to change), **skipped**
(nothing it could do, with the reason: no serial in LibreNMS, no matching platform, …) or
**error** (with the message). *Continue on error* decides whether a device's remaining steps
still run after one of its steps fails.

## Scope

A profile syncs the NetBox devices that are linked to its LibreNMS server (the `librenms_id`
custom field has an id for that server), that the user may view, and that match every filter
that is set: sites, roles, platforms, device tags, statuses. An empty filter matches every
device. *Run now* can narrow one run to a few devices.

## Safety

* **Permissions.** A run writes as the user who started it (Run now) or who saved the
  schedule: it sees only the devices that user may view and writes only what that user may
  change or add (NetBox object permissions apply). Starting a run or changing the schedule
  needs the plugin's change permission and *change* on the profile.
* **Change log.** A real run writes inside the NetBox request processors of the request that
  queued it, so every change is in the change log under that user, with its before-state, and
  event rules fire.
* **Transactions.** Every step of every device runs in its own transaction; a failed step is
  rolled back alone. Interfaces and IP addresses also use a savepoint per port or address.
* **Dry run.** Makes every write in one transaction, records it, and rolls it all back at the
  end, so each step sees what the earlier steps would have made (IP addresses on interfaces
  that the Interfaces step would create). Nothing is saved, nothing reaches the change log or
  the event rules. The row locks of a dry run last until it ends. The Import step does not
  import in a dry run; it lists the devices it would import.
* **One run at a time.** Run now refuses while a run of the profile is queued or running; a
  scheduled run that finds another run still running skips itself.
* **Disabled profiles** do not run, from the schedule or from Run now.
* **Nothing is deleted.** No step deletes NetBox objects.

## Schedule

The schedule is a NetBox recurring job attached to the profile (*Operations → Jobs* lists
it too). Saving the profile form or the Schedule dialog replaces the waiting job, so a profile
has at most one; removing the interval or disabling the profile removes it, and deleting the
profile removes its waiting jobs from the queue. A run whose schedule was changed while it
ran does not queue another run. The next run is shown on the profile page and in the list.

## Configuration

One new optional plugin setting:

```python
PLUGINS_CONFIG = {
    "netbox_librenms_plugin": {
        # ...
        "sync_profile_job_timeout": 3600,  # seconds a sync run may take (default 3600)
    }
}
```

After upgrading, run the migration and collect the static files (the step picker and the
result page use `sync_profiles.css` and `sync_profiles.js`), then restart NetBox and its
workers:

```bash
python manage.py migrate netbox_librenms_plugin
python manage.py collectstatic --no-input
```

Runs need a NetBox RQ worker (`manage.py rqworker`), like every NetBox background job.

## Upgrading an installed plugin to this fork

On the NetBox server, in the NetBox virtual environment:

```bash
source /opt/netbox/venv/bin/activate
pip show netbox-librenms-plugin            # the version installed now
cd /opt/netbox/netbox
python manage.py showmigrations netbox_librenms_plugin   # note the last applied migration
pg_dump netbox > ~/netbox-before-librenms-fork.sql        # back up the database first

pip install --upgrade --force-reinstall --no-deps \
  "git+https://gitlab-a.ipotekabank.uz/GetmanskiyKV/netbox-librenms-plugin.git@feature/librenms-sync-jobs"
pip install "google-re2>=1.1.20251105"     # the plugin's own dependency, which --no-deps skips
python manage.py migrate netbox_librenms_plugin
python manage.py collectstatic --no-input
sudo systemctl restart netbox netbox-rq
```

To keep the fork on later NetBox upgrades, replace the plugin's line in
`/opt/netbox/local_requirements.txt` with the same `git+https://…@feature/librenms-sync-jobs`
URL. `PLUGINS_CONFIG` needs no change. The migration also applies the upstream migrations that
the installed release may not have yet (up to `0024`), then adds `0025_syncprofile`.

## REST API

`/api/plugins/librenms_plugin/sync-profiles/` offers the usual NetBox CRUD for profiles. Runs
and schedules are started from the UI; saving a profile through the API does not change its
schedule.

## Known limits

* IP addresses are matched and created in the global VRF; LibreNMS VRFs are not mapped.
* VLANs are created per site; VLAN groups are not used.
* Virtual machines are not synced by profiles; only devices.
* Interfaces that disappeared from LibreNMS are left in NetBox.

## Code map

| Path | Content |
| --- | --- |
| `netbox_librenms_plugin/models.py` (`SyncProfile`) | The profile model; migration `0025_syncprofile`. |
| `netbox_librenms_plugin/sync_profiles/catalog.py` | The step catalog: keys, labels, descriptions, icons. |
| `netbox_librenms_plugin/sync_profiles/steps.py` | The steps and the run context (LibreNMS reads cached per run, permission checks). |
| `netbox_librenms_plugin/sync_profiles/engine.py` | Scope resolution and the run loop; builds the run result. |
| `netbox_librenms_plugin/sync_profiles/recorder.py` | Records the rows each step creates, updates or deletes. |
| `netbox_librenms_plugin/sync_profiles/scheduling.py` | Run now, schedule, unschedule, live progress. |
| `netbox_librenms_plugin/jobs.py` (`SyncProfileJob`) | The background job. |
| `netbox_librenms_plugin/sync_profiles/forms.py` | The profile form, the ordered step widget, the dialog forms. |
| `netbox_librenms_plugin/views/sync_profiles.py` | Pages and dialogs. |
| `netbox_librenms_plugin/templates/netbox_librenms_plugin/sync_profiles/` | Templates. |
| `netbox_librenms_plugin/tests/test_sync_profiles.py` | Tests. |
