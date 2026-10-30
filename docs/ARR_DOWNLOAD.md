# 115 离线下载（虚拟 qBittorrent）

可选模块 `python -m bridge.arr_download`。它对 Radarr/Sonarr 表现为一个 qBittorrent 下载客户端，背后没有真实的 qB：收到的种子直接交给 115 离线下载（经 CD2 API），队列里的进度、完成和失败都来自 115 离线任务列表和离线目录里实际出现的文件。和 `bridge.arr_import`、软链接同步分开运行、分开配置，用同一个镜像。

```
Arr ──qB API──► arr-download ──AddOfflineFiles──► 115 离线
                    │  ListOfflineFilesByPath / GetSubFiles 轮询
                    ▼
 Arr 队列显示完成 → Arr 导入 → Import Using Script → arr-import（CD2 云端移动/复制 + 改名）
```

从添加到 Arr 可以入库，115 有缓存的资源一般是一两分钟（实测 276 MB 公开种子约 1.5 分钟），整个过程不经过本机下载或上传。

## 工作流程

1. Arr 调用 `torrents/add`（磁力或 .torrent）。代理解析 infohash，只接受配置里列出的分类，拒绝私有种子和 BT v2。
2. 后台在 `cloud_download_root/.arr-offline/<infohash>` 建目录，提交离线。提交前先记下“已提交”：回复丢失或重启后按离线列表对账，不会重复提交。
3. 离线进行中：`percendDone` 作为进度，Arr 队列显示下载中。
4. 离线完成后核验离线目录：
   - 有 .torrent 时，种子里每个 ≥1 MiB 的文件都要以相同文件名和大小出现（115 有时不建种子根目录、会丢 .txt/.url 之类的小文件，所以不按完整路径比）；
   - 只有磁力时，目录里文件总大小要达到离线任务报告的大小；
   - 文件都要有云端 ID，并且列表（路径、大小、SHA1）连续 `settle_seconds` 不变。
5. 就绪后对 Arr 报 `stoppedUP`、进度 100%。`content_path` 指向种子根目录或单文件（目录里有多个顶层项时指向任务目录本身），Arr 照常导入。
6. 可选的预读（`warm.enabled`）：报完成之前，代理通过和 Arr 相同的 CD2 挂载打开每个视频（≥50 MB），先确认挂载上的大小正确，再读开头 `head_mb` 和结尾 `tail_mb`。Arr 导入前会用 ffprobe 读这两段；服务器到 115 网络差时 CD2 读取会失败，挂载上的文件随之显示成 0 字节，ffprobe 失败后 Radarr 以“无法确定是否为样片”永久拒绝。预读成功才报完成，失败每 `retry_seconds` 重试，期间 Arr 看到“检查中”；超过 `timeout_seconds` 仍未成功也报完成，交给 Arr 尝试，不当作下载失败。预读在后台线程里进行，不阻塞其他任务。需要把下载目录只读挂进代理容器（Compose 里的 `/downloads`）。
7. 115 离线失败（状态 3）、提交被拒、提交后 10 分钟仍不在离线列表、或超过 `timeout_seconds`：报 `error`。配置了 `arr` 时，代理调用 Arr API 把这次抓取标记为失败（`POST /api/v3/history/failed/{id}`），Arr 会拉黑这个种子，并按“失败下载处理”设置重新搜索。Arr 的 qB 客户端本身不会把 `error` 当作失败，所以不配置 `arr` 的话需要在 Arr 队列里手动“移除并拉黑”。

路径对照：

| 视图 | 示例 |
| --- | --- |
| Arr（`arr_download_root`） | `/downloads/.arr-offline/<infohash>/种子名/视频.mkv` |
| CD2（`cloud_download_root`） | `/115open/pt/download/.arr-offline/<infohash>/种子名/视频.mkv` |

`arr_download_root` 必须是 Arr 容器里 `cloud_download_root` 对应的挂载路径，入库规则（`arr-import.json`）的 `source_root`/`cloud_source_root` 要能覆盖它。

## 入库：移动还是复制

没有做种，下载目录里的文件入库后就没用了。每个种子报 `ratio_limit=0`（已达分享率），所以：

- Arr 下载客户端设置里**打开“移除已完成”（Remove Completed）**时，Arr 用 Move 模式导入，`arr-import` 在云端直接移动、改名，不留副本；导入后 Arr 发 `torrents/delete`（`deleteFiles=true`），`delete_files=true` 时代理把这个任务的离线目录（剩下的 NFO、样片等）送进网盘回收站。
- 不打开时 Arr 用 Copy 模式，`arr-import` 云端复制，下载目录保留，需要自己清理。
- 想先复制、核验后再删源：Remove Completed 保持打开，在 hook 配置（`arr-import-hook.json`）里设 `"transfer_mode": "copy"`。导入一律云端复制，之后 Arr 的删除请求再由 `delete_files` 把离线目录送进回收站。

`delete_files` 只会删除 `cloud_download_root/.arr-offline/<infohash>` 这一个目录，用的是 `DeleteFile`（进回收站），不是永久删除。

## 配置

`--config /settings/arr-download.json`，数据目录 `--data-dir /data`（`downloads.sqlite3`，表 `offline_jobs`）。示例：[deploy/arr-download.htpc.example.json](../deploy/arr-download.htpc.example.json)。

| 配置 | 含义 |
| --- | --- |
| `enabled` | 关闭后拒绝新任务（Arr 添加时报错），已有任务暂停轮询 |
| `username` / `password` | Arr 里填写的下载客户端账号；密码至少 24 字符。也接受 `Authorization: Bearer <password>` |
| `cloud.url` / `cloud.token` | CD2 地址和 API 令牌。**要用 Arr 挂载所在的那个 CD2**，否则挂载侧的目录缓存看不到新文件 |
| `account_root` | 115 账号在 CD2 里的根，如 `/115open` |
| `cloud_download_root` / `arr_download_root` | 同一个下载目录在 CD2 和 Arr 里的路径 |
| `categories` | 走离线的分类，Arr 下载客户端的 Category 填其中之一 |
| `send_trackers` | 默认 true：提交给 115 的磁力带上公开 tracker（只有 infohash、名称和 http/udp tracker）。私有种子一律拒绝 |
| `delete_files` | 默认 false：是否响应 Arr 的 `deleteFiles=true` |
| `arr` | 可选，`[{category, url, api_key}]`：失败时通知对应的 Arr |
| `warm` | 可选预读：`enabled`（默认 false）、`mount_root`（代理容器里的挂载路径，默认同 `arr_download_root`）、`head_mb`/`tail_mb`（默认 16/4）、`timeout_seconds`（默认 1800）、`retry_seconds`（默认 60） |
| `poll_seconds` / `settle_seconds` / `timeout_seconds` / `max_errors` | 轮询间隔、目录稳定时间、单任务总期限、连续错误上限 |

配置支持 `${环境变量}` 引用。旧版配置的 `qb_download_root`、`offline_categories` 仍然认；`qb`、`seed_after_check`、`import_after` 不再使用，启动日志会列出被忽略的项。旧版数据库里的任务（`downloads` 表）不会被读取或修改。

CD2 令牌需要：列目录、新建目录、添加/查看/删除离线任务；`delete_files=true` 时还要删除权限。

## 部署

[HTPC overlay](../deploy/arr-import.htpc.compose.yaml) 里有 `arr-download` 服务（只读根文件系统，只挂配置和数据目录，不需要 FUSE）。它要能访问宿主机的 CD2（`host.docker.internal:19798`），Arr 要能访问它（同一个 Compose 网络，`arr-download:8097`）。

```sh
python -m bridge.arr_download --config /settings/arr-download.json --check
```

Arr 设置：

1. 下载客户端 → 添加 qBittorrent：Host `arr-download`，Port `8097`，用户名/密码同配置，Category 填 `categories` 里的一个。建议打开 Remove Completed。
2. PT/私有索引器不能走 115 离线：在这些索引器的设置里把 Download Client 指定为原来的真实 qB，或者用标签把它们和离线客户端分开。代理收到私有种子会直接拒绝。
3. 失败自动换种：在 `arr` 里填 Arr 地址和 API Key，并在 Arr 的 Download Clients → Failed Download Handling 里打开 Redownload。

## 管理接口

登录后（Cookie 或 Bearer）：

- `GET /v1/downloads`：全部任务的状态、进度、消息。
- `GET /v1/downloads/<infohash>`：单个任务。
- `POST /v1/downloads/<infohash>/retry`：重试失败任务；已提交过的直接回到下载中继续查询，不重复提交。

## 边界

- Arr 移除任务时，代理调用 `RemoveOfflineFiles`（`deleteFiles=false`）删掉 115 的离线记录：还在离线中的会停止，已有文件不受影响（文件只按 `delete_files` 处理）。这样以后重新抓同一个种子不会被 115 以“任务已存在”拒绝。
- 提交时 115 已有同一 infohash 的离线记录（比如手动离线过），会被明确拒绝（CD2 报 gRPC 13，115 错误码 10008）。代理会删掉那条旧记录（不删文件）后重新提交一次；再被拒才报失败。
- `torrents/topPrio` 回 409（相当于 qB 关闭了队列），Arr 会忽略；暂停、恢复、分享率限制等写操作接受但不起作用。
- 只支持 BT v1 磁力和 .torrent；不支持 HTTP 种子 URL（Arr 会先下载种子再上传，不受影响）。
