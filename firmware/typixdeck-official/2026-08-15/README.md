# TypixDeck 官方固件 2026-08-15

官方历史合并产物，名称标注 UAC / MIC / CDC。实测 factory 为 1 MiB，无 font 分区；与较新完整镜像布局不同，功能与 CM4 兼容性未复测。

本目录逐字节镜像上游公开的合并产物，未重新编译或修改。来源：[官方原文件](https://github.com/TypixNode/TypixDeck-esp32s3-firmware/blob/fde9dac3b687a92fa2a6a049b5cd953dcb367b23/release/typixdeck_uac_mic_cdc_20260815_084438_merged_0x0.bin)；发布文件所在提交：`fde9dac3b687a92fa2a6a049b5cd953dcb367b23`。

| 项目 | 值 |
| --- | --- |
| 文件 | `typixdeck_uac_mic_cdc_20260815_084438_merged_0x0.bin` |
| 字节数 | `464208` |
| SHA-256 | `0ca3d64ec82e5bc1b208b8fa5eec7cb863e21dfa8f99c5753683177434d0f2c3` |
| 镜像 | ESP32-S3 合并镜像，偏移 `0x0` |
| 布局 | factory 1 MiB · 无 font · 单应用分区 |
| 设置影响 | 覆盖 NVS，重置设置 |

已核对上游原文件大小、SHA-256、镜像结构和空白 NVS。镜像收录不代表通过 CM4 刷写验收；可写范围由本机写入组件决定，详见 [写入说明](../../../docs/REAL-WRITER.md)。上游项目许可证见 [LICENSE](../LICENSE)，来源与第三方资源说明见 [官方镜像目录](../README.md)。
