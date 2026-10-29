# qB 下载 + CD2 云端入库整理

独立运行的可选模块。qB 继续下载到 CD2 挂载的 115 目录；Radarr/Sonarr 继续搜索、选种、识别媒体、生成名称和登记入库。只将导入时的视频复制和改名交给 CD2 API，保留 qB 源文件做种。

第一阶段不包含 CD2 离线下载的 qB 兼容接口、已有媒体批量改名或媒体根目录迁移。它们后续可单独接入。参考 [115-media-automation](https://github.com/ayanamimcy/115-media-automation) 的 CloudDrive 调用方式，这里的流程入口改为 Arr 的 Import Using Script，不依赖原项目的搜索、同步、命名和自动导入循环。

## 前置条件

- 源、目标、任务暂存目录必须在同一个 115 账号，均为 CD2 的普通未加密目录。不支持跨账号、跨云盘或上传本地文件。
- CD2 支持 gRPC-Web、上传任务查询、强制刷新目录，并为文件提供云端 ID、SHA1。缺少 SHA1 时等待并最终失败，不用大小代替内容核验。先运行只读 probe 验证实际版本。
- Radarr/Sonarr 支持 Media Management → Importing → Import Using Script。不是 Connect 中的 Custom Script/Webhook。
- Arr 容器有 Python 3.11+，能执行 hook。脚本仅使用标准库；不假定现有镜像已自带 Python。应通过镜像支持的安装方式或自定义镜像提供 Python，避免重建后丢失。
- Arr 容器能读取源、目标挂载。Arr 在导入脚本之前可能创建电影/季目录，之后可能设置时间、权限和处理字幕，所以媒体目标挂载通常需要写权限。整理服务自身不挂载视频目录。

## 独立配置与启动

复制 [服务配置示例](../deploy/arr-import.example.json)，另存到主机的 `arr-import.json`。它与 AutoSync 的 `config.json` 分开。示例路径均为占位，请调整：

| 配置 | 含义 |
| --- | --- |
| `enabled` | 是否接管匹配规则的入库，初始为 false |
| `token` | 整理服务自身的 Bearer 密钥，至少 24 字符 |
| `cloud.url / cloud.token` | 原生 CD2 的 API 地址与 CD2 API 令牌 |
| `account_root` | CD2 中该账号的虚拟根目录，如 `/115open` 或 `/115` |
| `staging_root` | 同账号内独立暂存目录，不能与下载/媒体目录重叠 |
| `rules[].source_root / destination_root` | **Arr 容器内**的下载目录与媒体根目录 |
| `rules[].cloud_source_root / cloud_destination_root` | 上述目录对应的 **CD2 API 虚拟路径** |
| `rules[].enabled` | 分别启停 Radarr、Sonarr 或其他不歧义的路径规则 |
| `rules[].mode` | 默认 `cd2`；显式 `native` 规则只返回原生导入，不需要云端路径字段 |
| `settle_seconds` | 远程身份/上传状态通过后的稳定等待时间 |
| `timeout_seconds` | 单任务等待上传与整理的总期限，默认两小时 |
| `max_errors` | 连续 API 异常次数上限，达到后停止并等待显式重试 |

例如 Arr 中 `/cloud/115/downloads/a.mkv` 可以映射到 API 的 `/115open/downloads/a.mkv`。qB 报告的目录经过现有 Remote Path Mapping 后，需与规则中的 Arr 源目录一致。

明确匹配到规则，且全局或该规则为 false 时，hook 输出 `DeferMove`，交回 Arr 原生导入。**停用意味着恢复原生复制，可能传输完整视频**。路径未匹配、服务不可达或 CD2 操作失败都返回失败，不自动回退。修改配置后重启独立服务；旧任务的映射变化会导致任务停止。

停用前先处理完已有任务。已登记的同一文件不会因关闭开关而切到原生导入，以免两条传输流程并行；已有任务需恢复原规则后继续或由管理员确认处理。

生成随机密钥：

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

配置支持 `${ARR_IMPORT_TOKEN}`、`${CD2_IMPORT_TOKEN}` 环境变量引用。CD2 令牌限定到该账号，按需授权目录/文件查询、上传任务查询、新建目录、复制、移动、重命名，不需要删除源文件权限。不要将令牌放入 URL 或命令参数。

先检查配置与远程文件（在项目根目录运行）：

```sh
python3 -m bridge.arr_import --config /path/to/arr-import.json --check
python3 -m bridge.arr_import --config /path/to/arr-import.json --probe '/115open/downloads/release/video.mkv'
```

`--check` 不连接云盘；`--probe` 只读查询文件和相关上传任务，输出 `remote_ready`。它不会创建目录、复制文件或读取视频内容，不代替 qB 完成状态及稳定时间核验。

路径与 probe 通过后，将 `enabled` 设为 true，再启动：

```sh
python3 -m bridge.arr_import --config /path/to/arr-import.json --data-dir /path/to/arr-import-data
```

数据目录与软链接同步服务分开。备份 `imports.sqlite3`，其中的源摘要、复制意图及发布意图用于重启恢复。CLI 默认数据目录为 `arr-import-data`。

## Docker 部署

使用 [独立 Compose](../deploy/arr-import.compose.yaml)，默认拉取 GitHub Actions 发布的镜像，单独运行整理服务，不在部署主机 build。准备权限为 600 的环境文件：

```dotenv
ARR_IMPORT_TOKEN=替换为独立随机密钥
CD2_IMPORT_TOKEN=替换为CD2令牌
ARR_IMPORT_CONFIG_FILE=/srv/appdata/arr-import/arr-import.json
ARR_IMPORT_DATA_DIR=/srv/appdata/arr-import/data
ARR_DOCKER_NETWORK=实际的Arr共享网络
PUID=1000
PGID=1000
```

提前创建服务配置和数据目录，让指定 UID/GID 可读配置、可写数据。Compose 不会自动创建缺失绑定。与两个 Arr 加入同一用户自定义网络后，hook 使用 `http://arr-import:8096`。宿主发布端口默认仅绑定回环。

```sh
docker compose --env-file /path/to/arr-import.env -f deploy/arr-import.compose.yaml config --quiet
docker compose --env-file /path/to/arr-import.env -f deploy/arr-import.compose.yaml pull
docker compose --env-file /path/to/arr-import.env -f deploy/arr-import.compose.yaml up -d
```

Linux 示例用 `host.docker.internal:host-gateway` 访问原生 CD2。CD2 必须监听容器可达的宿主地址；只监听宿主回环不一定可达。沿用并验证 qB/Arr 已有的 FUSE 权限、挂载传播和 CD2 重启后的挂载可见性。整理容器不需要 FUSE、privileged 或 Docker socket。

使用 `${CONFIGS}=/srv/htpc`、`/downloads`、`/movies`、`/tv` 的已有 HTPC Stack，可直接使用 [HTPC 配套说明](ARR_IMPORT_HTPC.md) 与 overlay，无需创建外部 Docker 网络。

需要本地源码构建时，可先执行 `docker build -t autosync-symlink:arr-import .`，再设置 `ARR_IMPORT_IMAGE=autosync-symlink:arr-import`。生产使用可将该变量固定到 Actions 成功发布的 `sha-<完整提交 SHA>`。

## 安装 Arr 导入脚本

1. 将 [scripts/arr_import.py](../scripts/arr_import.py) 放入两个 Arr 容器持久化配置目录，如 `/config/scripts/arr_import.py`，并 `chmod 755`。解释器为 `#!/usr/bin/env python3`。
2. 将 [hook 配置示例](../deploy/arr-import-hook.example.json) 保存为容器内 `/config/arr-import-hook.json`，仅让 Arr 用户读取。可通过 `ARR_IMPORT_CONFIG` 指定其他路径。
3. hook 的 token 与整理服务 token 一致。若引用 `${ARR_IMPORT_TOKEN}`，也需在 **Arr 容器内**提供此变量；或在只读配置文件内填写实际密钥。CD2 令牌只交给整理服务。
4. 两个 Arr 中启用高级设置，打开 Media Management → Importing → Import Using Script，将 Script Import Path 指向 `/config/scripts/arr_import.py`。Arr 自动传入源、目标两个参数。
5. 保留 qB 下载器和 Completed Download Handling。媒体库根目录与下载暂存目录分开。

hook 默认最多等待 180 秒，避免长期占用 Arr 导入流程；其超时后服务端任务仍保留。Arr 下次导入会复用相同任务，继续查询。只有远程核验成功、目标在 Arr 自己的挂载中可见且源大小/mtime 未改变，才输出 `[MoveStatus] MoveComplete`。

## 故障恢复

带服务 Bearer 认证的接口：

- `POST /v1/imports`：hook 提交 app、source、destination、size、mtime_ns、download_id。
- `GET /v1/imports/{id}`：查询状态。
- `POST /v1/imports/{id}/retry`：显式重试失败任务。
- `GET /healthz`：无需认证，只检查工作线程，不代表云盘就绪。

也可在 Arr 容器运行（密钥从 hook 配置读取）：

```sh
python3 /config/scripts/arr_import.py --status 任务ID
python3 /config/scripts/arr_import.py --retry 任务ID
```

复制使用每个任务独享的 `staging_root/{任务ID}`；先核验副本大小与 SHA1，再改名，最后以 Skip 冲突策略放入正式目录。源文件永远保留。发布响应丢失时，可根据已持久化的发布意图和目标内容恢复；无法证明归属的既存目标会拒绝，即使其大小相同。

提交复制后不反复发起复制任务。响应超时、结果不确定时先等待；任务超时失败后，应检查 CD2 实际状态，再显式重试。源内容改变或目标冲突不自动覆盖。空的任务暂存目录会保留，确认完成后可手动清理；不要删除处理中的目录或丢弃 SQLite。

## 首次验收与边界

先用新的目标目录测试一部电影和两集电视剧：上传未完成不复制，完成后名称和 Arr 入库记录正确，源文件能继续校验/做种；中断重启能恢复；重名不覆盖；Arr 容器和媒体服务器能访问、播放目标。

自动化测试使用模拟 CD2/115，真实 CD2 版本对 SHA1、上传状态、缓存可见性的行为尚需目标主机验收。缺少远程身份信息时应适配接口，不应放宽为只看大小。远程 SHA1 是提供商元数据，不读取本地视频计算摘要；也不将一次列表中没有上传任务视作上传已完成。

整季包由 Sonarr 枚举每个文件，服务逐文件执行，不自行挑最大视频或猜集号。当前只接管主视频，字幕/NFO 等附属文件与时间、权限仍按 Arr 原生流程处理，可能读取少量挂载数据。已有媒体批量改名、目录迁移、升级旧文件删除、回收站行为不经本模块统一拦截；特别是升级时 Arr 可能在导入脚本之前处理旧文件，先使用新目标验收并保留原媒体备份。

qB 直接写挂载目录仍经过本机下载、缓存和上传。这里优化后续入库复制，不承诺整个下载链路零流量或云端每次操作都瞬间完成。
