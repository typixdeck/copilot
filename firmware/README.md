# 固件目录

Copilot 0.2.6 启动或点击“刷新目录”时读取并验证 JSON 与相邻 `index-v2.json.sig`（Ed25519 原始 64 字节签名）：

`https://raw.githubusercontent.com/typixdeck/copilot/main/firmware/index-v2.json`

```text
firmware/
├── index.json             # 旧客户端 v1 保持不变
├── index.json.sig
├── index-v2.json          # 0.2.6 起使用 schema 2
├── index-v2.json.sig
├── typixdeck-official/
│   ├── 2026-09-10/
│   ├── 2026-08-21/
│   ├── 2026-08-15/
│   └── 2026-08-14/
└── typixdeck-diy/
    ├── 0.2.0/
    ├── 0.3.0/
    ├── 0.4.1/
    ├── 0.4.2/
    └── 0.4.3/
        ├── typixdeck-diy-0.4.3-full.bin
        ├── README.md
        └── licenses/
```

官方合并镜像已统一收录，版本说明及许可见 [typixdeck-official/](typixdeck-official/README.md)。Copilot 0.1.5 起官方下载使用此镜像；旧客户端仍能解析索引，官方条目不会重复出现。

最新公开自研版本为 [DIY 0.4.3 内置应用预发布版](typixdeck-diy/0.4.3/README.md)，新增计算器、日历和 2048，支持触摸和实体键盘。已完成宿主逻辑/UI及构建检查，尚未真机验收。0.4.2 及更早版本保留原文件与哈希。

[DIY 0.4.4 本地候选](typixdeck-diy/0.4.4/README.md) 在 v2 目录声明兼容设置保留策略；当前还未上传或真机验收，必须同时使用 Copilot 0.2.6。v1 目录不加入保留设置的新条目，避免旧写入器清空设置。

每个版本使用独立目录，不覆盖已发布 bin。新增固件时复制这个布局，把条目加入 `index-v2.json` 的 `firmwares` 数组，再执行：

```sh
python3 tools/sign-firmware-index.py --key /安全目录/发布私钥.pem --index index-v2.json
python3 tools/check-firmware-index.py --index index-v2.json
PYTHONPATH=src python3 -m unittest discover -s tests
```

签名工具先核对所有实际 bin、索引结构及哈希，再同步所选 `index-v2.json.sig` 和包内离线副本。私钥必须在仓库外，匹配 `src/typix_copilot/firmware-public-key.pem`；不要将私钥写入命令、环境变量、仓库或 deb。提交版本目录、JSON、签名和更新后的包内副本到同一提交后，支持该目录 schema 的客户端刷新即可发现并发起写入；首次使用 v2 必须升级客户端。JSON 与签名不一致时保留之前的已验证目录。不要提交设备读取备份、NVS 导出、Wi-Fi 凭据或维护日志。完整镜像覆盖 NVS，会清除设置；本目录只存无设备数据的构建产物。

## JSON 字段

新目录顶层固定为 `{"schema": 2, "firmwares": [...]}`，完整示例见 [index-v2.json](index-v2.json)。旧 [index.json](index.json) 仍为 schema 1，字节不变。

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
| `settings_policy` | `reset` 或 `preserve-diy-v1`；保留策略仅用于签名 DIY 设置兼容升级 |
| `settings_compatible_apps` | 保留策略允许的源应用 bin SHA256（包括目标自身）；完整分区表与 NVS 边界也必须一致 |
| `capabilities`, `hardware_verified` | 简短能力列表、发布者的硬件验证声明；不禁用未验证版本的写入 |

索引最多 256 KiB、100 条，每个镜像最多 16 MiB。下载地址只接受本仓库 `firmware/` 下的路径或等价 raw URL，拒绝重定向、外部地址、路径穿越、重复 JSON 字段和重复 ID。来源页面目前接受 `typixdeck`、`TypixNode` 两个 GitHub 组织下的仓库；第三方固件先在本组织整理可追溯发布说明。

## 离线与写入

签名目录原子保存到 `~/.cache/typix-copilot/registry/firmware-index-v2.signed`；失败保留原目录，首次离线使用安装包附带的签名目录；离线内容取决于所安装客户端版本。旧版未签名目录不作为写入凭据。

只有「写入」一个操作：确认后复用缓存或自动下载，校验完成再申请 Polkit 授权。bin 保存为 `~/.cache/typix-copilot/artifacts/<sha256>.bin`，每次复用均核对大小、哈希及镜像结构；对应 `<sha256>.<元数据摘要>.proof` 保存签名目录与版本 ID，两个版本使用相同 bin 时仍分别保留凭据，删除最后一个版本缓存时才移除共享 bin。旧 `<sha256>.proof` 仍可读取，复用后自动补充新版凭据。失败或取消不覆盖已验证缓存。

「本地固件」显示有效缓存版本，支持再次写入和移除缓存，不提供本地文件导入。旧版本从在线索引移除后，已有缓存凭据仍可用于离线写入；该设计意味着从索引移除不是撤销授权。每个版本 ID 和 bin 都应保持不变，发布更改时新增版本目录。需要撤销旧签名时必须发布更新后的信任策略/客户端。

系统助手重新验签、匹配所选镜像、检查板载拓扑和 ESP32-S3 配置，再执行完整备份与独立回读。签名目录不能指定命令、串口、系统文件路径或跳过检查。当前硬件验收限制见 [真实写入说明](../docs/REAL-WRITER.md)。
