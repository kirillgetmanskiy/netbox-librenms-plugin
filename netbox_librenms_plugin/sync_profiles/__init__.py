"""
Sync profiles: scheduled and on-demand synchronization of LibreNMS devices into NetBox.

A :class:`~netbox_librenms_plugin.models.SyncProfile` names the devices to sync (its scope),
the objects to sync for each device and the order of those steps, and an optional schedule.
``SyncProfileJob`` runs a profile in the background; ``engine.run_profile`` does the work.
"""
