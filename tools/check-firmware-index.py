#!/usr/bin/env python3
"""Validate the public index against local distribution bytes; no networking."""
from pathlib import Path
import argparse
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from typix_copilot.core import inspect_local, load_catalog, _image_end
from typix_copilot.cache import _known
from typix_copilot.registry import REGISTRY_BASE, parse_catalog


def validate_images(data):
    root = ROOT / "firmware"
    catalog = parse_catalog(data)
    by_id = {fw.id: fw for fw in catalog}
    for bundled in load_catalog():
        if bundled.id not in by_id or by_id[bundled.id].download_url != _known(bundled):
            raise ValueError(f"Bundled download must match the mirror index: {bundled.id}")
    indexed = set()
    for fw in catalog:
        relative = fw.download_url.removeprefix(REGISTRY_BASE)
        parts = Path(relative).parts
        if len(parts) != 3 or parts[1] != fw.version:
            raise ValueError("Use firmware/<project>/<version>/<filename>.bin")
        path = root
        for part in parts:
            path = path / part
            if path.is_symlink():
                raise ValueError("Distribution symlinks are not permitted")
        result = inspect_local(path)
        if result["size"] != fw.size or result["sha256"] != fw.sha256:
            raise ValueError(f"Index size/hash mismatch: {fw.id}")
        if result["kind"] != "merged-image":
            raise ValueError(f"Expected a merged ESP32-S3 image: {fw.id}")
        if fw.settings_policy == "preserve-diy-v1":
            image = path.read_bytes()
            end, app = _image_end(image, 0x10000, min(len(image), 0x210000))
            if not app or hashlib.sha256(image[0x10000:end]).hexdigest() not in fw.settings_compatible_apps:
                raise ValueError(f"The target DIY app must be in its signed settings allowlist: {fw.id}")
        indexed.add(path)
        print(f"OK {fw.id}: {fw.size} bytes, SHA256 {fw.sha256}")
    # Legacy clients retain the frozen v1 catalog. Only v2 covers later images;
    # publishing a new file must not mutate v1 bytes or authorize an old writer.
    if json.loads(data)["schema"] == 2 and indexed != set(root.rglob("*.bin")):
        raise ValueError("Every distributed .bin must be listed exactly in the index")
    print(f"Validated {len(catalog)} public firmware artifacts")


def main():
    from typix_copilot.authority import verify_catalog
    root = ROOT / "firmware"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", choices=("index.json", "index-v2.json"), default="index.json")
    args = parser.parse_args()
    data = (root / args.index).read_bytes()
    signature = (root / (args.index + ".sig")).read_bytes()
    verify_catalog(data, signature)
    validate_images(data)
    bundle = ROOT / "src/typix_copilot"
    if ((bundle / ("firmware-" + args.index)).read_bytes() != data
            or (bundle / ("firmware-" + args.index + ".sig")).read_bytes() != signature):
        raise ValueError("Bundled signed catalog differs: run tools/sign-firmware-index.py")
    print("Signature and bundled offline catalog verified")


if __name__ == "__main__":
    main()
