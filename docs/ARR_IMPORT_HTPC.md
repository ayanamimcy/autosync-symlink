# 接入现有 HTPC Stack

本示例对应现有 `${CONFIGS}=/srv/htpc` 布局，使用 GitHub Actions 发布的同一个镜像，独立运行入库整理服务，不在 NAS 本地 build，也不启动自动软链接服务。

## 路径对应

按宿主 `/media/media` 对应 CD2 `/115open` 生成以下映射；CD2 中各子目录的大小写应完全一致，启用前用 probe 核对。

| 用途 | 宿主机路径 | Arr 容器路径 | CD2 API 路径 |
| --- | --- | --- | --- |
| qB 下载 | `/media/media/pt/download` | `/downloads` | `/115open/pt/download` |
| 云端下载 | `/media/media/CloudDownload` | `/cloud_downloads` | `/115open/CloudDownload` |
| 电影 | `/media/media/pt/movie` | `/movies` | `/115open/pt/movie` |
| 剧集 | `/media/media/pt/tv` | `/tv` | `/115open/pt/tv` |
| 本地下载 | `/srv/htpc/downloads` | `/local` | 不经过 CD2 整理 |

配套 JSON 已分别配置两个 Arr 的 `/downloads` 和 `/cloud_downloads` 云端整理规则，并为 `/local` 配置明确的 `mode: native` 原生导入规则。未匹配的其他路径仍会拒绝，不自动复制。`mode: native` 需要包含本次新增支持的镜像版本；旧的首版入库适配镜像不支持该配置。

## 文件准备

将以下文件放到宿主机：

| 仓库文件 | 保存到宿主机 |
| --- | --- |
| `deploy/arr-import.htpc.example.json` | `/srv/htpc/arr-import/arr-import.json` |
| `scripts/arr_import.py` | `/srv/htpc/arr-import/hook/arr_import.py` |
| `deploy/arr-import-hook.example.json` | `/srv/htpc/arr-import/hook/arr-import-hook.json` |

提前创建 `/srv/htpc/arr-import/data`。配置和数据目录的属主使用 `/data/env/htpc.env` 中的 PUID/PGID，配置文件仅供运行用户读取，hook 脚本执行 `chmod 755`。

将两个新变量追加到主机上的 `/data/env/htpc.env`，不要提交实际令牌：

```dotenv
ARR_IMPORT_TOKEN=至少24字符的独立随机密钥
CD2_IMPORT_TOKEN=你的CD2令牌
```

原有 `ROOT`、`CONFIGS`、`DOWNLOADS`、`CLOUD_DOWNLOADS`、`MOVIES`、`TV`、`PUID`、`PGID`、`TZ` 等变量保留。可选 `ARR_IMPORT_IMAGE=ghcr.io/ayanamimcy/autosync-symlink:sha-完整提交SHA` 固定已发布版本。

服务配置中的 CD2 URL 为 `http://host.docker.internal:19798`，指向原生 CD2。CD2 必须监听 Docker 可达的宿主地址；仅绑定宿主回环时应先调整监听或使用可达的内网地址。hook 配置的 URL 则为 `http://arr-import:8096`，两者不要混用。

## 合并 Compose

[deploy/arr-import.htpc.compose.yaml](../deploy/arr-import.htpc.compose.yaml) 是 **overlay**，应与原有 HTPC Compose 使用同一个项目合并。它不会替换原有媒体挂载、PUID/PGID、端口、qB、字幕、音乐等服务。

命令行方式（将 `/path/to/htpc.yaml` 替换为正在使用的 Compose，并沿用原先的 `-p` 项目名，如有）：

```sh
docker compose --env-file /data/env/htpc.env -f /path/to/htpc.yaml -f deploy/arr-import.htpc.compose.yaml config --quiet
docker compose --env-file /data/env/htpc.env -f /path/to/htpc.yaml -f deploy/arr-import.htpc.compose.yaml pull arr-import
docker compose --env-file /data/env/htpc.env -f /path/to/htpc.yaml -f deploy/arr-import.htpc.compose.yaml up -d arr-import radarr sonarr
```

`env_file:` 只负责注入容器环境，不会自动为 Compose 文件中的 `${CONFIGS}` 等表达式提供值；上述 `--env-file` 用于 Compose 插值。不要输出完整的渲染配置，其中可能含有原 Stack 的密码。

若使用 Portainer 编辑已有 Stack：将 overlay 的 `arr-import` 服务加入原 `services`，再将 overlay 中 `radarr`、`sonarr` 的 environment 与两个文件挂载追加到原有服务，**保留它们原来的 image、environment 和 volumes**。Stack 环境变量需包含现有路径变量与两个新令牌。不要把 overlay 单独部署成只有半个 Radarr/Sonarr 定义的新 Stack。

服务默认加入原 Compose 的默认网络，两个 Arr 用服务名连接，无需 `links` 或外部网络。整理服务的调试端口仅发布到宿主 `127.0.0.1:18096`，避开常见的 8096 占用；容器内部仍为 8096。

两个 Arr 的 Python 由 [LinuxServer 官方 package-install mod](https://github.com/linuxserver/docker-mods/tree/universal-package-install) 安装：`DOCKER_MODS=linuxserver/mods:universal-package-install`、`INSTALL_PACKAGES=python3`。这不需要自建镜像，但容器首次启动/重建需要能拉取 mod 和软件包。若已有 mods/packages，将新增项以 `|` 合并，避免覆盖原有设置。不要把整理服务设置为 LinuxServer 风格的 PUID 环境变量：它使用 Compose 的 `user: PUID:PGID` 实际切换用户。

## 启用顺序

先等待对应 Actions 镜像发布成功，再启动服务。JSON 初始 `enabled=false`，此时不要先启用 Arr 导入脚本，否则已匹配云端规则会恢复原生复制。

只读验证配置及一条已上传完成的实际文件：

```sh
docker compose --env-file /data/env/htpc.env -f /path/to/htpc.yaml -f deploy/arr-import.htpc.compose.yaml exec arr-import python -m bridge.arr_import --config /settings/arr-import.json --check
docker compose --env-file /data/env/htpc.env -f /path/to/htpc.yaml -f deploy/arr-import.htpc.compose.yaml exec arr-import python -m bridge.arr_import --config /settings/arr-import.json --probe '/115open/pt/download/实际目录/实际视频.mkv'
```

确认远程 ID、SHA1、大小及上传状态后，将服务 JSON 的 `enabled` 改为 true，重启 arr-import。在两个 Arr 容器确认 `python3 --version` 和 `/config/scripts/arr_import.py` 可执行，再将 Media Management → Importing → Script Import Path 设为 `/config/scripts/arr_import.py` 并启用 Import Using Script。

qB 保持 host 网络、原 WebUI 端口和下载位置。Arr 入库路径依然是 `/downloads`、`/cloud_downloads`、`/movies`、`/tv`，不改成宿主路径或 CD2 API 路径。先用新目标做电影、单集小样本验收，再扩大范围。
