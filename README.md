# AutoSync Symlink

为 CloudDrive2 媒体源生成本地视频软链接或 STRM，并复制字幕、NFO 和封面。提供 Web 配置、日志、持久扫描状态、媒体库通知和保守清理，面向 Debian + Docker + Portainer，也保留 Windows 适配。

当前版本 `0.1.0`。实际验证范围见 [验证记录](docs/VERIFICATION.md)；正式媒体源仍需在目标主机验收。

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

- 每次启动全量对账、增量目录补扫及定时补扫；目录枚举和文件处理支持独立配置的多线程，扫描状态持久保存。
- Linux inotify 递归监听，新增子目录补监听、重挂载重订阅、溢出触发全量；Windows 原生目录事件；macOS 或 `poll` 模式使用元数据轮询。
- 视频只建立链接，不读取视频内容。字幕、NFO、图片以真实文件原子复制；周期性验证可识别大小和 mtime 未变的元数据变化。
- 支持 RMVB 视频（`.rmvb` / `.RMVB`），可生成软链接或 STRM。更新程序后再次同步即可补建此前跳过的 RMVB 文件；无需重建已有链接。
- 未托管文件和被刮削器修改的本地文件保留；源目录标记、Linux 挂载身份、扫描错误、缺失宽限期及删除比例保护共同限制清理。
- SQLite 持久通知队列，支持 Plex、Jellyfin、Emby，路径映射及失败重试。
- 同挂载卷不覆盖转存；需要明确的源、目标、稳定时间和源标记，默认无转存任务。
- 管理密钥登录、脱敏 JSON 配置、日志、任务和 1600×900 封面。

后台自动清理默认关闭。手动“执行清理”是明确的清理操作，仍受到所有删除保护约束；“同步”只创建或更新。`metadata_verify_seconds` 为 0 时每次检查元数据内容，仅对本地或少量文件使用。

## 多线程与速度

在 Web「配置管理 → 同步并发」调整目录扫描线程数和文件处理线程数，点击「保存并应用」，下一次扫描生效。旧配置启动时自动补齐默认值，不需要重建数据库或媒体库。

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
