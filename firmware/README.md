# 固件目录

Copilot 0.1.4 启动或点击“刷新目录”时读取：

`https://raw.githubusercontent.com/typixdeck/copilot/main/firmware/index.json`

```text
firmware/
├── index.json
├── typixdeck-official/
│   ├── 2026-09-10/
│   ├── 2026-08-21/
│   ├── 2026-08-15/
│   └── 2026-08-14/
└── typixdeck-diy/
    └── 0.2.0/
        ├── typixdeck-diy-0.2.0-full.bin
        ├── README.md
        └── licenses/
```

官方合并镜像已统一收录，版本说明及许可见 [typixdeck-official/](typixdeck-official/README.md)。Copilot 0.1.5 起官方下载使用此镜像；旧客户端仍能解析索引，官方条目不会重复出现。

每个版本使用独立目录，不覆盖已发布 bin。新增固件时复制这个布局，把条目加入 `index.json` 的 `firmwares` 数组，再执行：

```sh
python3 tools/check-firmware-index.py
PYTHONPATH=src python3 -m unittest discover -s tests
```

提交 bin 和 JSON 到 `main` 后，客户端刷新即可发现。不要提交设备读取备份、NVS 导出、Wi-Fi 凭据或维护日志。完整镜像覆盖 NVS，会清除设置；本目录只存无设备数据的构建产物。

## JSON 字段

顶层固定为 `{"schema": 1, "firmwares": [...]}`，完整示例就是 [index.json](index.json)。

| 字段 | 用途 |
| --- | --- |
| `id` | 唯一版本 ID；小写字母、数字、点、短横线、下划线 |
| `version`, `title`, `summary` | 显示版本、项目名、简短说明 |
| `publisher` | 来源分类：`官方`、`自研`、`第三方`；只是发布者声明 |
| `filename`, `size`, `sha256` | bin 文件名、精确字节数、64 位小写 SHA-256 |
| `download_url` | 相对 `firmware/` 的 `<项目>/<版本>/<文件>.bin` 路径 |
| `source_url`, `commit` | 可追溯来源页面；已知提交用完整 40 位 SHA，未知填空字符串并在说明中注明 |
| `chip`, `board` | 当前固定为 `esp32s3`、`typixdeck` |
| `image_kind`, `flash_offset` | 当前仅 `merged-image`、整数 `0`，不接受 app-only 镜像 |
| `layout`, `nvs_reset` | 分区说明、是否重置设置；仍须交叉检查实际镜像 |
| `capabilities`, `hardware_verified` | 简短能力列表、发布者的硬件验证声明；不代表写入授权 |

索引最多 256 KiB、100 条，每个镜像最多 16 MiB。下载地址只接受本仓库 `firmware/` 下的路径或等价 raw URL，拒绝重定向、外部地址、路径穿越、重复 JSON 字段和重复 ID。来源页面目前接受 `typixdeck`、`TypixNode` 两个 GitHub 组织下的仓库；第三方固件先在本组织整理可追溯发布说明。

## 离线与写入

索引验证后原子保存到 `~/.cache/typix-copilot/registry/firmware-index.json`；失败保留原目录，首次离线时使用随包官方目录。bin 保存为 `~/.cache/typix-copilot/artifacts/<sha256>.bin`，下载和每次复用均核对大小、哈希及镜像结构；失败或取消不覆盖已验证缓存。

已开放固件保持“写入 → 自动复用缓存或下载”的流程。待审核条目提供“下载固件”，写入按钮禁用。在线 JSON 不能覆盖内置固件的固定信息，不能声明命令、串口或授权规则，也不能扩大 root 写入组件的允许列表。新版本开放写入需要板级审核和写入组件升级。当前硬件验收限制见 [真实写入说明](../docs/REAL-WRITER.md)。
