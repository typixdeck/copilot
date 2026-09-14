#!/usr/bin/env python3
"""Validate the public index against local distribution bytes; no networking."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from typix_copilot.core import inspect_local
from typix_copilot.registry import REGISTRY_BASE, parse_catalog


def main():
    root = ROOT / "firmware"
    catalog = parse_catalog((root / "index.json").read_bytes())
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
        indexed.add(path)
        print(f"OK {fw.id}: {fw.size} bytes, SHA256 {fw.sha256}")
    if indexed != set(root.rglob("*.bin")):
        raise ValueError("Every distributed .bin must be listed exactly in the index")
    print(f"Validated {len(catalog)} public firmware artifacts")


if __name__ == "__main__":
    main()
