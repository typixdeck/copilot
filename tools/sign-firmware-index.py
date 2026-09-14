#!/usr/bin/env python3
"""Validate distribution bytes, sign the catalog and refresh its offline bundle.

The existing publisher key stays outside this repository. Never embed it in a
package, environment variable, argument value, or distribution artifact.
"""
import argparse
from pathlib import Path
import os
import runpy
import tempfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]


def save(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix=".signed-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", type=Path, required=True, help="External Ed25519 PEM private key path")
    args = parser.parse_args()
    if args.key.expanduser().resolve().is_relative_to(ROOT):
        parser.error("Signing key must be outside the repository")
    raw = (ROOT / "firmware/index.json").read_bytes()
    runpy.run_path(str(ROOT / "tools/check-firmware-index.py"))["validate_images"](raw)
    key = serialization.load_pem_private_key(args.key.expanduser().read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        parser.error("Expected Ed25519 key")
    bundle = ROOT / "src/typix_copilot"
    trusted = serialization.load_pem_public_key((bundle / "firmware-public-key.pem").read_bytes())
    if key.public_key().public_bytes_raw() != trusted.public_bytes_raw():
        parser.error("Signing key does not match the installed catalog trust key")
    signature = key.sign(raw)
    trusted.verify(signature, raw)
    save(ROOT / "firmware/index.json.sig", signature)
    save(bundle / "firmware-index.json", raw)
    save(bundle / "firmware-index.json.sig", signature)
    print("Signed firmware/index.json and refreshed the offline bundle")


if __name__ == "__main__":
    main()
