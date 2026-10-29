# AutoSync Symlink

为 CloudDrive2 媒体源生成本地视频软链接或 STRM，并复制字幕、NFO 和封面。提供 Web 配置、日志、持久扫描状态、媒体库通知和保守清理，面向 Debian + Docker + Portainer，也保留 Windows 适配。

保存配置时页面会立即显示任务状态。若正在扫描，配置在当前任务结束后优先保存并应用，期间不会中断扫描；看到“配置已保存并应用”前，请勿重启服务，尚未应用的配置任务不会跨重启保留。校验或应用失败会显示具体原因。

当前版本 `0.1.0`。实际验证范围见 [验证记录](docs/VERIFICATION.md)；正式媒体源仍需在目标主机验收。

可选的 [Radarr/Sonarr → CD2 入库整理模块](docs/ARR_IMPORT.md) 独立部署、独立启停：保留 qB 下载到 115 挂载目录，通过导入脚本调用 CD2 云端复制、改名并保留做种源。与软链接同步配置分开，默认不接管；第一阶段不包含离线下载适配或已有媒体批量改名。真实 CD2/115 与 Arr Docker 环境仍需小样本验收。

## 本地启动

需要 Python 3.11 或更新版本；日常同步只依赖标准库，封面使用 Pillow。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/demo.py
.venv/bin/python run.py --data-dir .local-demo/data serve
```

打开 `http://127.0.0.1:18095`，管理密钥在 `.local-demo/data/admin.key`。演示仅使用项目内隔离文件夹，视频是不可播放的占位文件。Ctrl+C 停止。

运行 `.venv/bin/python run.py serve` 会在 `data/` 创建配置，默认停用所有媒体库、CloudDrive API 和通知。Web 界面为 `http://127.0.0.1:8095`；默认配置监听所有接口，正式使用前限制到可信网络。

## 已实现

- 可配置启动和通知重连时的全量对账、增量目录补扫及定时补扫；目录枚举和文件处理支持独立配置的多线程，扫描状态持久保存。
- Linux inotify 递归监听，新增子目录补监听、重挂载重订阅、溢出触发全量；Windows 原生目录事件；macOS 或 `poll` 模式使用元数据轮询。
- 视频只建立链接，不读取视频内容。字幕、NFO、图片以真实文件原子复制；周期性验证可识别大小和 mtime 未变的元数据变化。
- 支持 RMVB 视频（`.rmvb` / `.RMVB`），可生成软链接或 STRM。更新程序后再次同步即可补建此前跳过的 RMVB 文件；无需重建已有链接。
- 未托管文件和被刮削器修改的本地文件保留；源目录标记、Linux 挂载身份、扫描错误、缺失宽限期及删除比例保护共同限制清理。
- SQLite 持久通知队列，支持 Plex、Jellyfin、Emby，路径映射及失败重试。
- 同挂载卷不覆盖转存；需要明确的源、目标、稳定时间和源标记，默认无转存任务。
- 管理密钥登录、脱敏 JSON 配置、日志、任务和 1600×900 封面。

后台自动清理默认关闭。手动“执行清理”是明确的清理操作，仍受到所有删除保护约束；“同步”只创建或更新。`metadata_verify_seconds` 为 0 时每次检查元数据内容，仅对本地或少量文件使用。

## 多线程与速度

在 Web「配置管理 → 扫描策略与并发」调整扫描周期、启动/重连补扫开关以及目录/文件线程数，点击「保存并应用」，下一次扫描生效。旧配置启动时自动补齐默认值，不需要重建数据库或媒体库。

```json
"sync": {
  "scan_workers": 4,
  "file_workers": 4
}
```

以上是需要调整的两个字段，完整配置见示例。两个字段均支持 1–32，设置为 1 即串行；默认目录 4、文件 4，分别加速子目录枚举和链接/STRM/元数据处理。可逐步调到 8，比较日志中的扫描耗时；实际速度仍取决于目录结构、CloudDrive 缓存和云盘限流。

两个阶段依次执行，每阶段的待执行任务数量受限；清理在全部文件任务结束后串行执行。文件线程会同时读取元数据，内存使用也会增加（默认每文件读取上限 32 MiB），不读取视频内容。不同媒体库仍依次处理。

媒体库卡片展示当次扫描的实时阶段：枚举目录时显示已发现的媒体和元数据文件数，文件处理阶段显示已处理/待处理总数、变更和问题数。顶部统计为累计托管数。进度在内存中实时更新，约每秒及阶段切换时保存计数，避免每个文件都额外提交数据库事务。

`首次全量尚未完成` 表示还没有一次完整且无待处理项的全量扫描。存在读取错误、等待稳定的文件或元数据超限时会记为“有待处理项”，在日志中查看原因。`sync.metadata_max_mb` 是单个字幕/NFO/图片的大小上限，默认 32 MiB，允许 1–256；超限文件会跳过，其余文件继续处理。需要同步大图时按实际大小调整该参数，同时考虑文件线程数对应的内存使用。

## 使用 CloudDrive2 API 扫描

在 Web「配置管理 → 扫描策略与并发」将「扫描数据来源」切换为「CloudDrive2 API」。也可将以下字段合并到现有配置的 `sync`，保留其他字段：

```json
"sync": {
  "scan_backend": "clouddrive",
  "cloud_list_workers": 2,
  "cloud_list_interval_ms": 500
}
```

- `scan_backend` 支持 `filesystem`（挂载盘遍历，默认）和 `clouddrive`（CD2 API）。旧配置自动补齐为 `filesystem`，升级不会自行改变扫描来源。两种后端共用原清单，无需重新初始化或重建媒体库。
- 配置 `cloud.url` 和有效 Token（或登录信息），Token 需要「列出文件」权限。`cloud.enabled` 只控制文件通知订阅，API 扫描不依赖这个开关。若需实时更新，仍须启用通知。
- 为每个库填写 CD2 中的虚拟 `cloud_path`，例如 `/115open/pt/movie`；`source` 仍填写容器内挂载路径，例如 `/srv/media/cloud/115/pt/movie`。留空时尝试通过挂载点 API 映射，需要额外的「获取挂载点」权限；映射失败会报错，不会回退扫盘。
- `GetSubFiles` 按目录流式返回路径、类型、大小、修改时间，省去挂载层逐文件属性查询。全量仍需递归列举目录；视频只创建链接/STRM，不读取内容。元数据复制与一致性检查、源健康标记检查仍使用挂载盘，源/输出挂载路径要求不变。
- `cloud_list_workers` 范围 1–4，默认 2；`cloud_list_interval_ms` 为同一扫描内目录请求的共享最小启动间隔，范围 100–60000 毫秒，默认 500。文件处理并发仍由 `file_workers` 控制；`scan_workers` 仅影响挂载盘后端。不要为了提速盲目提高并发或缩短间隔。
- 普通全量和事件局部扫描均不强制刷新目录（`forceRefresh=false`），也不改变 CD2 的目录缓存策略。缓存过期/缺失仍可能访问上游，API 不保证比热缓存的挂载盘更快，也不保证减少 115 请求数。API 超时、限流、无权限、缺少成功 trailer 或非法路径会终止本次扫描；不会重试该请求或自动回退扫盘。HTTP 429 / gRPC 状态 8 会让同一 CD2 地址在本进程内进入 60 秒冷却，期间新扫描也不发送 API 请求；冷却结束不主动重扫，后续通知、定时或手动任务仍可能再次扫描。
- 清理不以缓存缺项直接判定删除：先对缺失路径沿途的父目录定向刷新，共用父目录在同次校验中只刷新一次；目录明确不含该路径后才进入原有宽限期/比例/空快照/指纹保护。刷新失败不修改缺失时间或删除文件；实际删除的那次扫描也要重新确认。预览清理同样可能产生这些定向刷新请求。排除目录不再参与清理，已有输出会保留。
- CD2 修改时间与挂载盘属性不一致时，元数据复制会保守报错并保留旧输出；请检查实际 CD2 版本与挂载缓存，不要关闭一致性检查。该后端经本地模拟 gRPC-Web 服务验证，实际云端速度和版本兼容性需按小库验证。

保留每 12 小时一次全量、关闭启动/重连补扫的配置仍有效。若要避免本地监听启动遍历和轮询，请同时使用 `watch_mode=off`，配合 CD2 文件通知；API 后端不会替代本地监听机制。

## 跳过启动全量扫描

需要包含本功能的新镜像/代码。将以下字段合并到现有配置的 `sync` 中，保留其他字段；不要用片段替换整个配置：

```json
"sync": {
  "interval_seconds": 43200,
  "scan_on_startup": false,
  "scan_on_reconnect": false,
  "watch_mode": "off"
}
```

- `scan_on_startup=false`：启动和应用配置时不主动安排全量扫描，CloudDrive 首次连接、本地监听首次就绪也不触发全量；首次定时全量等待完整周期，此例为启动后 12 小时。
- `scan_on_reconnect=false`：通知断线恢复后不额外全量补扫。断线期间遗漏的变化由下一轮定时全量或手动同步补齐；启动和重连两个开关默认均为 `true`，旧配置保持原行为。
- `watch_mode=off`：关闭本地 inotify/Windows 通知和目录轮询，不影响 CloudDrive gRPC 推送。开启本地递归监听仍需读取目录建立监听，轮询仍需遍历源目录；仅关闭启动全量不能消除这部分读取。
- 使用该组合实时同步时，需单独配置并开启 `cloud.enabled`、有效 Token 和每个库正确的 `cloud_path`，确认文件通知订阅成功；否则只有定时/手动扫描会发现变化。事件流本身仍可能产生请求，不保证完全不访问云盘。

手动同步、真实文件变更和监听溢出等纠错事件不受启动开关限制。旧版本不识别新增字段，必须先更新代码/镜像。该策略不会停止已经运行的扫描。

## 正式媒体源

参考 `config.linux.example.json`，修改 `source/output/mount_root/output_mount_root/cloud_path` 后保存为 `data/config.json`。Linux 示例中的 `115` 是挂载名称占位，必须与实际 CloudDrive 挂载相符。

在独立新输出目录及新运行状态下首次导入。源标记需要先初始化；容器默认把源只读挂载，因此应在宿主机完成初始化，或由管理员在已确认挂载的源目录创建精确内容为 `media-bridge:<library-id>` 的 `.media-bridge-ready` 文件。不能在未挂载的空目录中创建标记。

每个媒体库的源根目录都需要自己的标记；新增 YouTube 等媒体库后也需要初始化。缺少标记时，先确认实际挂载和配置路径，再初始化；标记内容中的 id 必须与配置一致。Linux 输出支持文件名中的冒号；Windows 输出仍拒绝冒号、盘符和数据流。相对路径中的反斜杠、NUL、绝对路径和 `..` 会被拒绝，错误会指出具体路径。

```sh
.venv/bin/python run.py --data-dir data initialize --library movie
.venv/bin/python run.py --data-dir data check
.venv/bin/python run.py --data-dir data preview --library movie
.venv/bin/python run.py --data-dir data serve
```

CLI 检查、初始化、预览与扫描不能和使用同一数据目录的服务并行运行；服务运行中通过 Web 提交任务。

迁移旧 Windows 配置时自动补齐新字段；已有清单的源/输出/模式仍不可随意修改。Windows→Linux 使用新的 `data` 和新输出目录，旧数据库和元数据先备份，不直接搬移 Windows 软链接。

## Docker / Portainer

Debian 部署步骤、挂载传播、只读源初始化和媒体容器路径见 [部署说明](docs/DEPLOYMENT.md)。本地 `compose.yaml` 支持构建；`deploy/portainer-stack.yaml` 默认使用 `ghcr.io/ayanamimcy/autosync-symlink:edge`。首次使用前确认对应 [Actions 构建](https://github.com/ayanamimcy/autosync-symlink/actions)成功。

```sh
docker build -t autosync-symlink:0.1.0 .
docker compose config --quiet
docker compose up -d
```

启动前必须准备可写的数据目录、输出目录、配置文件和正确的 CloudDrive 挂载；Compose 不会替你创建缺失的宿主挂载路径。`AUTOSYNC_BIND_IP` 默认是 `127.0.0.1`，需要局域网访问时改为目标主机的可信接口 IP。

容器进程健康检查为 `/healthz`，不代表云盘已挂载或全量已完成。请通过认证后的 `/api/status` 同时检查监控连接、全量成功时间和错误。

## 自动打包与发布

[GitHub Actions](https://github.com/ayanamimcy/autosync-symlink/actions) 在推送 `main`、推送 `v*` 标签、提交 PR 或手动运行时启动。测试通过后生成源码 `.tar.gz`、`.zip` 和 `SHA256SUMS`，保存在运行的 `source-packages` 附件中，保留 14 天。

- `main`：发布 Linux AMD64/ARM64 多架构镜像 `ghcr.io/ayanamimcy/autosync-symlink:edge` 和 `sha-<完整提交 SHA>`。
- 版本标签（例如 `v0.1.0`）：发布镜像 `:0.1.0`、`:0.1`，正式版本同时更新 `:latest`；创建 GitHub Release 并附上源码包和校验文件。
- 预发布标签（例如 `v0.2.0-rc.1`）：发布对应版本镜像和预发布 Release，不更新 `latest`。
- PR：执行测试和构建验证，不推送镜像。

流程使用 GitHub 自带的 `GITHUB_TOKEN`，无需另存 Docker 密码。GHCR 包若是私有，需在 Portainer 配置有读取权限的 Registry 凭据；生产部署建议固定版本或镜像摘要。自动打包不会更新正在运行的服务。

本地生成同样的源码包：`python scripts/package.py --version 0.1.0`。打包采用源码白名单，排除 `data`、演示目录、`.env`、密钥和数据库。

## 测试

```sh
.venv/bin/python -m unittest discover -s tests -v
```

测试覆盖真实本地进程/HTTP、首次扫描、增量更新、重启恢复、挂载替换、不完整快照、同名冲突、元数据保护、删除熔断、通知重试及发布包隔离。Linux 原生事件测试须在 Linux 运行，Windows API 测试须在 Windows 运行；云端执行结果见 Actions。

## 当前边界

- 目录结构映射不等于智能识别或自动标准化命名；命名、原盘结构、多版本合并仍需媒体库小样本验证。
- 云端远程变化能否触发事件取决于 CloudDrive2 会员文件通知、挂载和提供商行为；可选 gRPC 通道需要真实服务验收。不能把 inotify 已连接视作上游通知完整可靠。
- Windows Plex 对文件软链接有兼容性限制。STRM 不作为 Plex 的通用替代。
- 转存不实现跨卷复制或 CloudDrive 服务端移动 API，不会因移动失败而退化成下载视频。
- 不与另一同步程序同时写同一输出目录。备份 `data`、Compose 及本地真实元数据；Portainer 备份不包含业务数据。

处理流程、平台监控和数据保护策略见 [设计说明](docs/DESIGN.md)。
