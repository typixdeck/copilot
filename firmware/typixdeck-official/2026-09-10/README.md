# TypixDeck 官方固件 2026-09-10

官方仓库中的完整合并镜像。文件结构与固定哈希已审计；CM4 板级写入与恢复仍待真机验收。日期来自文件名，提交仅标识产物所在仓库版本。

本目录逐字节镜像上游公开的合并产物，未重新编译或修改。来源：[官方原文件](https://github.com/TypixNode/TypixDeck-esp32s3-firmware/blob/fde9dac3b687a92fa2a6a049b5cd953dcb367b23/release/typixdeck_esp32s3_full_20260910.bin)；发布文件所在提交：`fde9dac3b687a92fa2a6a049b5cd953dcb367b23`。

| 项目 | 值 |
| --- | --- |
| 文件 | `typixdeck_esp32s3_full_20260910.bin` |
| 字节数 | `3997320` |
| SHA-256 | `515860212f0812b2c9b0cc98766d5a95d4be599a06d2d36b522af6b20d5b1ec3` |
| 镜像 | ESP32-S3 合并镜像，偏移 `0x0` |
| 布局 | factory 2 MiB · font 4 MiB · 单应用分区 |
| 设置影响 | 覆盖 NVS，重置设置 |

已核对上游原文件大小、SHA-256、镜像结构和空白 NVS。镜像收录不代表通过 CM4 刷写验收；可写范围由本机写入组件决定，详见 [写入说明](../../../docs/REAL-WRITER.md)。上游项目许可证见 [LICENSE](../LICENSE)，来源与第三方资源说明见 [官方镜像目录](../README.md)。
