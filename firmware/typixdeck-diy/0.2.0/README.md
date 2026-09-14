# TypixDeck DIY 0.2.0

面向 TypixDeck 0720 板载 ESP32-S3 的本地构建候选，使用 ESP-IDF 5.5.1。已完成编译、组件测试和实际 UI 主机渲染，**尚未刷入真机验证**。

- 传感器：紧凑电池、电源、接口与输入状态。
- 树莓派：新鲜遥测与协作控制；未知 HDMI FPS 显示缺省值，不伪造状态。硬启动和深度休眠尚未开放。
- 应用：本地合成小乐器与时钟；小乐器并非 USB/BLE MIDI 或完整 GM 音源。
- 设置：Wi-Fi、NTP、主题色、亮色/暗色背景。

`typixdeck-diy-0.2.0-full.bin` 是含 bootloader、分区表、应用和字体的合并镜像，描述偏移为 `0x0`。采用 8 MiB Flash、2 MiB factory 和 4 MiB font 布局，头部 DIO/80 MHz。完整写入覆盖 NVS，清除偏好及已保存的 Wi-Fi 设置。发布产物的 NVS 区间为空白，不含设备备份或用户凭据。

大小：`3997320` 字节。SHA-256：

```text
ced53b5e9efd7b77a1061e20bf03d573edac53d2db235bc70d78212e8797f184
```

源码基于 [TypixNode 官方工程](https://github.com/TypixNode/TypixDeck-esp32s3-firmware/tree/fde9dac3b687a92fa2a6a049b5cd953dcb367b23)，应用改造来自本地 `diy-esp32s3-firmware` 工作副本。该候选尚无对应的改造源码提交，因此索引 `commit` 留空；上游提交不代表此产物的可重复构建证明。发布于此处供 Copilot 发现、下载和检查，不代表已获得板级写入批准。

上游工程 MIT、ESP-IDF Apache 2.0、TinyUSB MIT、FreeType FTL 与 Special Elite Apache 2.0 声明保留在 [licenses/](licenses/)。本软件部分基于 FreeType 团队的工作。字体资源包含上游提供的阿里巴巴普惠体子集，使用和分发须遵守其字体许可；这些第三方资源不因本目录发布而改为项目许可。
