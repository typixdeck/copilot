# TypixDeck 官方固件 2026-08-21

官方完整镜像的历史版本，包含 factory 与 font 分区。完整写入会覆盖 NVS；此处不承诺历史功能或板级兼容性。日期来自文件名。

本目录逐字节镜像上游公开的合并产物，未重新编译或修改。来源：[官方原文件](https://github.com/TypixNode/TypixDeck-esp32s3-firmware/blob/fde9dac3b687a92fa2a6a049b5cd953dcb367b23/release/typixdeck_esp32s3_full_20260821.bin)；发布文件所在提交：`fde9dac3b687a92fa2a6a049b5cd953dcb367b23`。

| 项目 | 值 |
| --- | --- |
| 文件 | `typixdeck_esp32s3_full_20260821.bin` |
| 字节数 | `3936704` |
| SHA-256 | `86c61cce1db8830b89daffa010fb91e03fabb2b3c6cf73eb7948c2f7439db4a2` |
| 镜像 | ESP32-S3 合并镜像，偏移 `0x0` |
| 布局 | factory 2 MiB · font 4 MiB · 单应用分区 |
| 设置影响 | 覆盖 NVS，重置设置 |

已核对上游原文件大小、SHA-256、镜像结构和空白 NVS。镜像收录不代表通过 CM4 刷写验收；可写范围由本机写入组件决定，详见 [写入说明](../../../docs/REAL-WRITER.md)。上游项目许可证见 [LICENSE](../LICENSE)，来源与第三方资源说明见 [官方镜像目录](../README.md)。
