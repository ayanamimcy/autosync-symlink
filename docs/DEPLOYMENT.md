# Debian / Portainer 部署

## 准备

在被管理的本地 Debian 主机准备 Docker、实际 CloudDrive 挂载、持久化数据目录。Oracle 上的 Portainer 通过私网 Agent 接管该主机。下面所有绑定路径都属于本地 Debian 主机，不属于 Oracle。

建议统一：挂载父目录 `/mnt/clouddrive`；实际挂载示例 `/mnt/clouddrive/115`；输出 `/srv/media/library`；程序状态 `/srv/appdata/autosync-symlink`。

`mount_root` 和 `output_mount_root` 检查的是容器当前挂载命名空间。当前 Compose 只把输出 `/srv/media/library` 绑定进容器；若启用输出挂载检查，应填写 `/srv/media/library`，不能填仅在宿主机挂载的 `/srv/media`。源 `mount_root` 应填写实际 FUSE 子挂载 `/mnt/clouddrive/115`，不是仅作为绑定父目录的 `/mnt/clouddrive`。

输出 bind mount 本身存在，仍不能证明它来自期望的数据盘：如果宿主机数据盘未挂载，Docker 可能绑定系统盘上的同名目录。因此启动 Stack 前还要在宿主机核对输出数据盘的挂载点、设备 UUID 和剩余空间，必要时使用宿主 systemd 启动依赖和前置检查。`create_host_path: false` 只能阻止自动创建不存在的绑定路径，不能识别已存在的空占位目录。

创建目录并仅对程序状态/输出授予选定 UID/GID 写权限，勿对源目录或整盘递归 chmod/chown。默认容器用户为 1000:1000，可在 Stack 环境变量调整。CloudDrive FUSE 必须允许该用户及媒体用户实际读取，例如根据官方挂载配置使用 allow_other，并测试权限。

## 挂载传播

CloudDrive 若在容器中挂载，把专用父目录配置为宿主 shared 挂载，并在 CloudDrive 容器使用 shared/rshared。消费挂载的同步、Plex、Jellyfin 容器绑定该父目录，使用 rslave。

示例消费绑定：

```yaml
- type: bind
  source: /mnt/clouddrive
  target: /mnt/clouddrive
  read_only: true
  bind:
    propagation: rslave
    create_host_path: false
```

不能只加 rslave 就认为宿主传播已经配置。用 `findmnt -o TARGET,FSTYPE,PROPAGATION` 核验，并在重启 CloudDrive 后实际读取。只读嵌套挂载在不同内核/文件系统下行为需实测；同时使用 FUSE 权限限制源写入。无需给同步程序 privileged 或 Docker socket。

## 配置和源标记

1. 备份旧 Windows 服务数据、Y 盘真实元数据及媒体服务数据库；Linux 使用独立新清单和输出。
2. 将 `config.linux.example.json` 复制到 `/srv/appdata/autosync-symlink/config.json`，调整每个实际路径和挂载点。
3. 从宿主机在确认已挂载的三个源根目录初始化 `.media-bridge-ready`，内容分别为 `media-bridge:movie`、`media-bridge:tv`、`media-bridge:bangumi`。已有正确标记可继续使用；不覆盖不匹配标记。
4. 必须在源只读绑定前完成标记初始化。不要为了一个初始化操作把同步容器整体改成特权容器。
5. CloudDrive API 默认关闭；开启前填写地址、Token 或环境变量引用，并实测上游变更。容器的 `127.0.0.1` 指向自己；示例 `host.docker.internal` 通过 host-gateway 指向宿主，宿主 CloudDrive 发布端口须实际可达。

媒体服务器地址也必须调整：Linux 示例中 Plex/Jellyfin 的 `127.0.0.1` 是停用通知时的占位值，启用前要改为可达地址。同一用户自定义网络可用 `http://jellyfin:8096` 等服务名；通过宿主已发布端口访问时可用 `http://host.docker.internal:8096`。只有绑定宿主回环的端口不一定能从 bridge 容器经 host-gateway 访问，应实际测试网络和监听地址。

新增媒体库时，在该库自己的源根目录初始化标记；挂载父目录或另一个媒体库的标记不能替代它。例如，只有配置中的库 id 确实为 `youtube` 时，标记内容才是 `media-bridge:youtube`。缺失标记既可能是未初始化，也可能是挂载异常或标记被删除，应先确认实际挂载可读和源目录正确；服务不会自动创建缺失源目录或绕过标记检查。源可写时可在 Web「常用工具」选择该库初始化，源只读绑定时在宿主机处理。

Plex 的一个通知配置只对应一个 `section_id`。电影、电视剧和番剧若是三个 Section，应创建三条配置，每条只绑定对应的媒体库 ID，不能把三类通知都发给同一个 Section。

## 镜像和 Stack

GitHub Actions 测试通过后自动发布 AMD64/ARM64 镜像。在本地 Debian 目标机器拉取：

```sh
docker pull ghcr.io/ayanamimcy/autosync-symlink:edge
```

先确认 Actions 的 `docker` 任务成功。若 GHCR 包是私有，需要先登录，并在 Portainer 的 Registries 配置有读取权限的凭据。`edge` 随主分支更新，正式使用建议将 `AUTOSYNC_IMAGE` 固定为已发布版本或镜像摘要。

也可在目标机器自行构建，并将 Stack 的 `AUTOSYNC_IMAGE` 设置为该本地标签：

```sh
docker build -t autosync-symlink:0.1.0 .
```

在 Portainer 选择本地 Debian 环境，将 `deploy/portainer-stack.yaml` 作为 Stack 部署，按需设置 PUID/PGID 和 AUTOSYNC_BIND_IP。默认仅绑定宿主回环接口；局域网使用时设置可信接口并限制访问来源。

如果从 Oracle 选择本地环境部署，镜像必须在本地主机已有，或是该主机能认证拉取的仓库镜像。在 Oracle 构建一个镜像不会自动出现在本地。GHCR 多架构镜像会自动匹配 AMD64/ARM64；自行构建时需要匹配目标主机架构。

Plex/Jellyfin 同时挂载 `/srv/media/library` 和 `/mnt/clouddrive` 到同名路径。只有媒体库目录而没有目标源目录时，软链接将无法播放。

## 首次接管与备份

首次只开启创建/更新，后台清理保持关闭。查看每库首次全量成功时间，核对数量、冲突和挂载状态，再验证播放、字幕、云端增改删、挂载重连和整机重启。之后才决定是否开启 `cleanup_enabled`。

`/healthz` 仅确认处理线程存活；健康容器仍可能在等待源标记或挂载。业务状态在认证后的 `/api/status`。

备份 Compose、配置、`data` 全部内容和本地真实元数据。离线备份 SQLite 前停止 Stack；在线备份需 SQLite backup API，不能只复制正在写的主数据库文件。备份包含密钥，应私有保存。Portainer 备份不包含这些业务数据。

管理端更新或断线不影响现有容器运行。旧 Windows 服务与新服务不能同时写同一输出目录。

## 官方参考

- https://docs.docker.com/engine/storage/bind-mounts/
- https://www.clouddrive2.com/en/help.html
- https://docs.portainer.io/admin/environments/add/docker
- https://docs.portainer.io/faqs/getting-started/what-does-portainers-backup-include
