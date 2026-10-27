# 本地验证记录

日期：2026-10-27，Asia/Tokyo。版本：0.1.0。

## 本次通过

- macOS ARM64，Python 3.14.4，Pillow 12.3.0。
- `python -m unittest discover -s tests -v`：共 94 项，92 项通过、2 项平台专属测试跳过，无失败。
- 跳过项：真实 Linux inotify、真实 Windows ReadDirectoryChangesW；事件解析和补扫调度的可移植测试已运行。
- 真实子进程/HTTP 测试：启动服务、健康检查、管理登录、首次全量、元数据复制、后续文件新增与更新、默认不自动清理、终止与重启后状态恢复。
- 模拟挂载替换时停止写入、不完整或无权限扫描不清理、保留人工修改、同大小同 mtime 的元数据更新、删除熔断和通知重试。
- `docker compose config --quiet` 及 Portainer Stack 的 Compose 解析通过。
- `node --check bridge/static/app.js`、Python compileall、两份 JSON 示例校验通过。
- 发布包测试覆盖可重复生成、源码白名单、运行时数据排除及源码符号链接拒绝。
- 实际生成 49 文件的源码 tar.gz/zip，SHA256 校验和 ZIP 完整性检查通过；未包含运行时数据、管理密钥或本机路径。
- Actions YAML 解析和 7 个 shell 步骤的 Bash 语法检查通过；这不替代 GitHub 上的实际运行。
- 隔离演示服务实际启动，生成 1 个视频软链接和 2 个真实元数据文件；认证状态接口确认首次全量成功，待发通知为 0。示例视频是占位内容，不能用于播放验收。

## 本次未验证

- Docker 引擎尚未启动：不能声称镜像已经成功构建或容器已运行。
- Debian/Linux 原生内核事件尚未在本地环境执行；仓库包含对应测试和 Linux/macOS CI。云端构建与测试的实时结果以 GitHub Actions 为准。
- 真实 CloudDrive FUSE、会员上游通知、gRPC 账户权限和重挂载传播。
- Plex/Jellyfin/Emby 的真实入库、播放、字幕及硬件转码。
- Windows 回归测试；之前部署版本的 Windows 测试记录不等于本版本测试结果。
- Web 浏览器视觉与交互验收；本次只验证实际 HTTP 和接口流程。

本记录仅描述本地验证，未覆盖实际服务器部署。
