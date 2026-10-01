"""Private, partition-bound NVS preservation for signed DIY upgrades.

No settings are decoded, exported or added to public logs. Only exact known
application bytes may opt into the shared v1 NVS schema. A mismatch is never
silently converted into a reset.
"""
from __future__ import annotations

import hashlib
import struct

from .core import _image_end, _partition_table
from .registry import RegistryError, validate_settings_policy

SECTOR = 0x1000
TABLE_OFFSET = 0x8000
NVS_OFFSET = 0x9000
NVS_SIZE = 0x6000
FACTORY_OFFSET = 0x10000
FACTORY_SIZE = 0x200000


class SettingsError(ValueError):
    pass


def effective_image(firmware, image: bytes, backup: bytes) -> tuple[bytes, int]:
    """Return private effective bytes and preserved byte count, or reject.

    The signed image hash is checked by the writer before this function. The
    backup is read from the selected physical device and durably saved first.
    This routine changes exactly the NVS sectors; every other output byte must
    match the signed image. The writer independently reads back all output.
    """
    try:
        validate_settings_policy(firmware)
    except (RegistryError, TypeError, ValueError):
        raise SettingsError("settings-policy") from None
    if firmware.settings_policy == "reset":
        return image, 0
    try:
        # Full sector includes flags, MD5, entries and unused bytes. Equality of
        # offsets alone cannot certify compatibility with another partition map.
        if (len(image) < NVS_OFFSET + NVS_SIZE or len(backup) < FACTORY_OFFSET + FACTORY_SIZE
                or image[TABLE_OFFSET:TABLE_OFFSET + SECTOR] != backup[TABLE_OFFSET:TABLE_OFFSET + SECTOR]):
            raise SettingsError("settings-layout")
        rows = _partition_table(image)
        expected = [("nvs", NVS_OFFSET, NVS_SIZE), ("phy_init", 0xF000, 0x1000),
                    ("factory", FACTORY_OFFSET, FACTORY_SIZE), ("font", 0x210000, 0x400000)]
        if [(row["label"], row["offset"], row["size"]) for row in rows] != expected:
            raise SettingsError("settings-layout")
        # Exact partition entry types, subtypes and no encrypted flags.
        wanted = [(1, 2), (1, 1), (0, 0), (1, 0x82)]
        for index, (kind, subtype) in enumerate(wanted):
            entry = struct.unpack_from("<HBBII16sI", image, TABLE_OFFSET + 32 * index)
            if (entry[1], entry[2], entry[6]) != (kind, subtype, 0):
                raise SettingsError("settings-layout")
        if image[NVS_OFFSET:NVS_OFFSET + NVS_SIZE] != b"\xff" * NVS_SIZE:
            raise SettingsError("settings-image-data")
        # Validate checksum and appended app digest; hash only the complete app
        # binary, excluding unused factory padding and private NVS data.
        end, is_app = _image_end(backup, FACTORY_OFFSET, FACTORY_OFFSET + FACTORY_SIZE)
        digest = hashlib.sha256(backup[FACTORY_OFFSET:end]).hexdigest()
        if not is_app or digest not in firmware.settings_compatible_apps:
            raise SettingsError("settings-source")
    except SettingsError:
        raise
    except (ValueError, struct.error):
        raise SettingsError("settings-source") from None
    output = bytearray(image)
    output[NVS_OFFSET:NVS_OFFSET + NVS_SIZE] = backup[NVS_OFFSET:NVS_OFFSET + NVS_SIZE]
    return bytes(output), NVS_SIZE
