# 公开 BT 离线下载与 qB 接种

可选模块 `python -m bridge.arr_download`，与 `bridge.arr_import`、软链接同步分别运行和配置。复用同一 Dockerfile 和镜像，服务本身不挂载媒体文件，也不调用旧的独立媒体编排项目。下载代理是新增本地实现；真实 CD2/115、qB 5.1.4 和 Arr 的端到端下载验收仍需单任务验证。

## 工作流程

Arr 的 qB 下载客户端指向代理 → 代理保存原始种子/磁力 → qB 取得原始文件清单并停止 → CD2 创建隔离目录并提交 115 离线 → 核验完整云端文件集合。随后入库与 qB 接种独立运行：

- `import_after=cloud_ready`：云端文件集合核验、稳定后即可由 Arr 通过已有 Import Using Script 复制、改名入库，同时 qB 完整分块校验；校验 100% 后请求做种。
- `import_after=qb_checked`（未配置时的默认值）：等 qB 完整校验 100% 后允许入库，同时请求做种。入库不等待做种时长、分享率或实际上传量。

两种模式都保留原始做种文件。入库后 Arr 移除已完成下载记录，只隐藏代理的交付记录，后台 qB 校验、做种继续进行。qB 做种读取原文件，CD2 入库复制并改名副本；两者可以并行。

磁力用 qB 5.1.x 的 `stopCondition=MetadataReceived` 获取元数据；上传的 .torrent 以 `stopped=true`、`skip_checking=false` 添加。qB 暂停会清除停止条件，因此元数据尚未取得时通过代理暂停后恢复，会安全移除该空元数据任务，再以 MetadataReceived 重新添加。不会直接恢复不带停止条件的磁力。原始 .torrent 和元数据保存在私有下载任务数据库中。

每个 infohash 的做种目录单独隔离：

| 路径视图 | 示例 |
| --- | --- |
| qB / Arr | `/downloads/.arr-offline/<infohash>/原始种子目录/视频.mkv` |
| CD2 | `/115open/pt/download/.arr-offline/<infohash>/原始种子目录/视频.mkv` |

现有 `/downloads` → `/115open/pt/download` 入库映射能直接覆盖这些子目录。整理只处理副本，原做种目录保留。不涉及独立 `/cloud_downloads` 挂载。

## 开关与配置

配置由 `--config /settings/arr-download.json` 指定，持久化使用 `--data-dir /data`，数据库为 `downloads.sqlite3`。

```json
{
  "enabled": false,
  "host": "0.0.0.0",
  "port": 8097,
  "username": "arr-download",
  "password": "${ARR_DOWNLOAD_PASSWORD}",
  "qb": {
    "url": "http://host.docker.internal:8080",
    "username": "${ARR_DOWNLOAD_QB_USER}",
    "password": "${ARR_DOWNLOAD_QB_PASSWORD}"
  },
  "cloud": {
    "url": "http://100.78.216.82:19798",
    "token": "${CD2_DOWNLOAD_TOKEN}"
  },
  "account_root": "/115open",
  "cloud_download_root": "/115open/pt/download",
  "qb_download_root": "/downloads",
  "offline_categories": ["radarr-offline-test", "sonarr-offline-test"],
  "seed_after_check": true,
  "import_after": "cloud_ready",
  "poll_seconds": 5,
  "settle_seconds": 20,
  "timeout_seconds": 86400,
  "max_errors": 12
}
```

- `enabled`：是否对所选分类接管离线；关闭后，新普通任务可直通真实 qB。已有未完成或失败任务保持代理控制，不能通过切换模式让同一种子直接开始下载。
- `offline_categories`：只接管明确列出的分类；其他分类走原 qB。存在活动离线任务时，直通请求也需要能识别 v1 hash 以避免接管冲突，不接受此时无法解析的远程 .torrent URL。
- `seed_after_check`：创建任务时记录是否在完整校验后启动做种；false 则保持已完成、停止状态。修改配置只影响新任务。
- `import_after`：入库放行条件，取值为 `cloud_ready` 或 `qb_checked`，未配置时默认后者。创建任务时记录，修改配置只影响新任务。
- 入库模式继续使用原 `arr-import.json` 的独立配置。

`cloud_ready` 允许先入库，但云端文件名、长度和 SHA1 就绪不等同于 BT 分块校验通过。如果后台 qB 校验失败，任务状态报告失败及交付警告，不启动做种，也不自动删除已入库副本。可检查文件后通过重试接口重新校验。若希望校验失败的文件完全不进入媒体库，选择 `qb_checked`。

代理密码使用独立、至少 24 字符的随机密钥。真实 qB 登录信息与 CD2 token 只提供给代理容器；Arr 填写代理自己的用户名/密码。配置通过环境变量引用密钥，部署文件不包含真实凭据。保护配置、环境文件和数据目录，数据目录建议 0700；下载数据库使用 0600。

CD2 token 需要 `allow_list`、`allow_create_folder`、`allow_get_transfer_tasks`、`allow_add_offline_download`、`allow_list_offline_downloads`。范围应覆盖配置的下载目录；入库服务的复制、改名权限仍由原入库 token 配置。下载代理不需要删除文件权限。

## 部署与接入 HTPC

同一源码构建镜像自动包含本模块，不需要更改 Actions 或 Dockerfile。未发布新源码前，旧 `edge` 镜像不含本模块；应先本地构建，或待对应 Actions 发布成功后使用固定镜像标签。

```sh
docker build -t autosync-symlink:arr-download .
```

下载代理需要与 Arr 位于同一 Compose 网络，入口为 `python -m bridge.arr_download --config /settings/arr-download.json --data-dir /data`。只绑定配置文件和私有数据目录；健康检查使用 `http://127.0.0.1:8097/healthz`。复用当前 HTPC 的 env 文件、PUID/PGID、host-gateway 设置。

原生 CD2 和 host 网络 qB 必须能被**新代理容器**访问。已有仅针对 arr-import IP 放行的防火墙规则不会自动覆盖新容器。先验证 TCP 19798 / 8080 可达，再调整此服务的最小放行范围。

```sh
python -m bridge.arr_download --config /settings/arr-download.json --check
```

初始使用独立测试分类。Arr 中增加 qB 类型的测试下载客户端，Host 为 `arr-download`，Port 为 `8097`，用户名 `arr-download`，密码为 ARR_DOWNLOAD_PASSWORD，分类填对应 offline-test 分类。保留原下载客户端。确认测试任务只送到新客户端后，再将 enabled 设为 true 并重启下载代理，手动选择单个公开 v1 种子。校验和入库期间不手动移动或改名其做种文件。

磁力需要能找到元数据来源；仅有 infohash、但无元数据节点的任务不能完成接种。115 离线状态成功后，还要逐项确认种子中的全部文件，包括 NFO、字幕等；文件名、目录、长度必须与原种子一致。目录结构不同或附属文件缺失时保持等待，最终超时，不猜测最大视频、不跳过校验。

## Arr API 与任务恢复

代理实现 Arr 使用的 qB v2 登录、版本、配置读取、分类、添加、种子列表、属性、文件、tracker、做种限制及开始/停止/移除操作，不是通用 qB WebUI 的完整替代。其他管理接口拒绝转发。首次添加后立即提供待处理任务属性，供 Arr 正常记录 downloadId。满足 `import_after` 后，代理向 Arr 提供完成的 115 下载交付视图，包括完成状态、文件清单及原始保存路径。真实 qB WebUI 保留其实际校验、做种进度；代理交付进度不表示 qB 已经完成校验。

代理暂停不保证暂停云盘服务端已经提交的离线下载。交付就绪前，取消会停止并移除代理拥有的 qB 任务，始终 `deleteFiles=false`；云端已提交任务和文件保留。交付就绪后，Arr 的移除操作只隐藏下载交付记录，保持后台 qB 任务；需要结束做种时在真实 qB 中管理。代理拒绝对托管任务使用 `deleteFiles=true`。已有同 hash 的普通 qB 种子不会被接管。失败、取消、重启都不自动删除云端文件或改变现有 qB 种子的保存路径。

使用代理登录后的 SID 访问：

- `GET /v1/downloads/<40位hash>`：任务状态、阶段和无凭据错误信息；`delivery_status` 表示 waiting/ready/blocked/removed，`seeding_status` 表示后台接种阶段或失败，`qb_checked` 表示完整校验是否通过。Arr 清理交付记录后仍可查询后台任务。
- `POST /v1/downloads/<40位hash>/retry`：恢复已有失败任务，保留离线提交意图。
- `GET /v1/diagnostics`：qB 请求/失败计数、最近成功时间、最近错误的接口和阶段，以及应用信息缓存的年龄。仅输出脱敏信息；不查询媒体文件或创建任务。

### qB 短时卡顿与 503

`app/version`、`app/webapiVersion` 使用曾经真实读取成功的版本，缓存一小时；`app/preferences` 只缓存白名单设置五秒，不缓存认证字段。过期刷新遇到故障或另一个刷新仍在进行时，最多额外使用六十秒的旧值，响应头 `X-Arr-App-Cache: stale` 明确标记该情况。并发首次读取同一接口只发一个上游请求，失败后短暂退避三秒。无有效缓存时保留真实的 503；不会编造版本号。

种子列表、文件完成度和写操作不使用这个缓存，qB 不可用时仍会明确报错，不会错误放行入库或做种。添加/开始/删除等请求丢失响应时不自动重放。`qb.timeout_seconds` 可选，默认十五秒，允许三到六十秒；修复不依赖增加默认超时时间。

普通 qB 请求使用独立的线程 HTTP 处理器并共享认证 Cookie，仅登录/刷新认证串行，慢请求不再占用整个客户端的网络锁。队列渲染读取数据库快照，不等待后台持锁执行 CD2 操作。HTTP 监听队列为 128，以承受多个 Arr 实例的突发轮询。

故障日志区分等待响应头和读取响应体，记录接口路径、异常类型和耗时，不打印请求参数、响应原文或凭据。`response_headers` 包含建立连接及等待响应头的时间，本身不能进一步证明是网络丢包或 qB 主线程阻塞；应与直连延迟、监听队列和系统负载一起判断。

### CD2 的文件夹选项与原始种子布局

qB 添加参数显式为 `contentLayout=Original`；核验同时使用原始 metainfo 和 qB 实际文件清单，不能靠更改全局布局选项猜测修正云端路径。

CD2 网页包含“为每个文件创建文件夹”选项，其帮助文字针对上传文本文件时以文件名创建目录。当前公开的 `AddOfflineFileRequest` 只有 `urls`、`toFolder`、`checkFolderAfterSecs`，没有这个开关。此模块传递磁力链接，不上传文本列表；不要添加未核实的 protobuf 字段或假定网页选项能控制 API 的落盘布局。若 115 返回 `任务目录/视频.mkv/视频.mkv`，当前版本会等待目录与原种子对齐，不自动移动或删除云端文件。

请求超时后先按 hash 查询 115 离线列表，避免盲目重复提交。若提交结果不确定且列表始终没有任务，不自动再次提交；需在 CD2 中核实并处理原离线任务，再重试。任务取消后不自动重新创建同 hash 任务。

qB 完整校验需要读取全部数据，缓存未命中可能从 115 读取一遍。做种也会经 CD2 读取数据后使用本机上行上传。校验成功只能说明文件完整；实际上传速度和上传量还取决于云盘读取、本机上行及是否有下载者。

## 验收与当前边界

第一版支持公开、纯 BT v1、UTF-8 路径的单个磁力或 .torrent 请求，以及其中的单文件/多文件包。不接管 PT/private=1、v2/hybrid、符号链接、填充文件、下载时改名、选择性下载或批量 .torrent 请求。纯磁力只有取得元数据并确认非私有后，才提交 CD2 离线。HTTP .torrent URL 请由 Arr 获取种子后作为 multipart 上传。

本地测试覆盖完整离线/校验/做种状态机、两种入库放行条件、交付清理后后台继续校验/做种、附属文件缺失、校验失败及重试、云端数据改变、暂停/取消、同 hash 冲突、重启及响应丢失恢复、接口鉴权和错误脱敏。网络接口在本机模拟服务验证。上线前仍需验证真实 qB stopped/metadata/recheck 状态变化、115 离线目录布局、完整包接种、Arr 完成入库及源文件保留。

额外故障注入覆盖响应头/响应体超时、慢请求与队列读取隔离、并发 Cookie 过期只登录一次、冷缓存突发请求合并、有限旧值回退和恢复、无效 HTTP 200 响应不缓存、真实队列故障保留 503，以及写响应丢失不重复提交。已有发布版作为负对照时不能通过慢请求隔离测试。

## 协议依据

- [CloudDrive 官方 proto](https://www.clouddrive2.com/api/clouddrive.proto)：AddOfflineFiles、ListOfflineFilesByPath，离线完成状态为 2。
- [qB 5.1.4 添加参数](https://github.com/qbittorrent/qBittorrent/blob/release-5.1.4/src/webui/api/torrentscontroller.cpp)：stopped、stopCondition、skip_checking、contentLayout。
- [qB 停止条件](https://github.com/qbittorrent/qBittorrent/blob/release-5.1.4/src/base/bittorrent/torrent.h)：MetadataReceived、FilesChecked。
- [qB stop 实现](https://github.com/qbittorrent/qBittorrent/blob/release-5.1.4/src/base/bittorrent/torrentimpl.cpp)：手动停止会清除 stopCondition。
- [Radarr qB v2 客户端](https://github.com/Radarr/Radarr/blob/v6.4.4.10685/src/NzbDrone.Core/Download/Clients/QBittorrent/QBittorrentProxyV2.cs)。
- [BEP 3](https://www.bittorrent.org/beps/bep_0003.html)：原始 info 字节的 infohash、文件集合和分块校验。
