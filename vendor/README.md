# ESP32-S3 RAM stub

`esp32s3-v1.2.2.json` is the unchanged official [esp-flasher-stub v1.2.2 release asset](https://github.com/espressif/esp-flasher-stub/releases/tag/v1.2.2), byte-identical to the S3 stub shipped in esptool v5.4.0. Size: 16,780 bytes. SHA256: `8816e0611701e8f7396a9fee9d1d33bc2021751bf24bda54560fb87c62de3f0b` (also matches GitHub's release asset digest). MIT license is included alongside it.

The v1.2.2 source pins esp-stub-lib `d61983fa4d1f66b9c088608c1f702ad877121279`, which includes [USB full-packet termination fix 18225f8](https://github.com/espressif/esp-stub-lib/commit/18225f80349a6eb415b49c2f9c166ee324486f86). The earlier v1.0.0 OTG implementation skips the final zero-length flush when a SLIP frame ends on a 64-byte boundary. CM4's repeated backup failure and the public official image match this boundary at `0xEC000`.

The builder verifies exact bytes and replaces only the S3 generation-2 JSON in the packaged esptool 5.3.1 tree. This avoids unrelated host-tool and dependency changes. The stub override does not change firmware images, board profiles, backup, erase, write, or readback ranges. The host also holds the CDC connection until the ROM transition and rejects unknown pre-existing RAM stubs; see docs/REAL-WRITER.md.

**Candidate, not hardware acceptance:** loader compatibility and package identity can be checked without opening USB. Successful complete backup and independent readback still require controlled CM4 verification. This asset is a RAM maintenance program, not the firmware offered in Copilot's firmware catalog.
