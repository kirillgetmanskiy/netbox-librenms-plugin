"""The catalog of sync steps a profile can run, in their recommended order."""

from dataclasses import dataclass


@dataclass(frozen=True)
class StepInfo:
    """One kind of object a profile can sync."""

    key: str
    label: str
    description: str
    icon: str
    # Whether the step runs once per profile run instead of once per device.
    per_run: bool = False


STEP_IMPORT_DEVICES = "import_devices"
STEP_SERIAL = "serial"
STEP_PLATFORM = "platform"
STEP_DEVICE_TYPE = "device_type"
STEP_INTERFACES = "interfaces"
STEP_INTERFACE_RELATIONSHIPS = "interface_relationships"
STEP_VLANS = "vlans"
STEP_IP_ADDRESSES = "ip_addresses"
STEP_PRIMARY_IP = "primary_ip"
STEP_CABLES = "cables"

STEPS = (
    StepInfo(
        STEP_IMPORT_DEVICES,
        "Import new devices",
        "Create NetBox devices for LibreNMS devices that match the import filters and are not in NetBox yet. "
        "Imported devices join the scope of the later steps.",
        "mdi-database-import",
        per_run=True,
    ),
    StepInfo(STEP_SERIAL, "Serial number", "Copy the device serial number from LibreNMS.", "mdi-barcode"),
    StepInfo(
        STEP_PLATFORM,
        "Platform",
        "Set the platform that the LibreNMS OS matches (exact name or a platform mapping).",
        "mdi-layers-outline",
    ),
    StepInfo(
        STEP_DEVICE_TYPE,
        "Device type",
        "Set the device type that the LibreNMS hardware matches (part number, model or a device type mapping).",
        "mdi-server",
    ),
    StepInfo(
        STEP_INTERFACES,
        "Interfaces",
        "Create and update interfaces from LibreNMS ports: name, type, speed, MTU, MAC, description and status. "
        "Interface rules (ignore / set type) apply.",
        "mdi-ethernet",
    ),
    StepInfo(
        STEP_INTERFACE_RELATIONSHIPS,
        "Interface relationships",
        "Set LAG membership, sub-interface parents and bridge membership from the LibreNMS port stack. "
        "Interface rules apply. Run after Interfaces.",
        "mdi-link-variant",
    ),
    StepInfo(
        STEP_VLANS,
        "VLANs",
        "Create the device's VLANs in its site and set the 802.1Q mode and VLANs of its interfaces.",
        "mdi-lan",
    ),
    StepInfo(
        STEP_IP_ADDRESSES,
        "IP addresses",
        "Create IP addresses from LibreNMS and assign them to the device's interfaces.",
        "mdi-ip-network",
    ),
    StepInfo(
        STEP_PRIMARY_IP,
        "Primary IP",
        "Set the primary IPv4/IPv6 address to the address LibreNMS polls the device on.",
        "mdi-star-outline",
    ),
    StepInfo(
        STEP_CABLES,
        "Cables",
        "Connect interfaces with cables from LLDP/CDP links when both ends exist in NetBox and are free.",
        "mdi-cable-data",
    ),
)

STEPS_BY_KEY = {step.key: step for step in STEPS}
STEP_CHOICES = tuple((step.key, step.label) for step in STEPS)
DEFAULT_STEPS = [STEP_SERIAL, STEP_PLATFORM, STEP_INTERFACES, STEP_IP_ADDRESSES]


def step_label(key):
    """Return the label of a step key, or the key itself when the catalog no longer has it."""
    info = STEPS_BY_KEY.get(key)
    return info.label if info else key


def normalize_steps(value):
    """Return the known, distinct step keys of *value* in their given order."""
    if not isinstance(value, (list, tuple)):
        return []
    seen = []
    for key in value:
        if key in STEPS_BY_KEY and key not in seen:
            seen.append(key)
    return seen
