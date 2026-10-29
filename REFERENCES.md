# 接口文档与协议约定

独立 Arr 入库模块另外使用 GetSubFiles(forceRefresh=true)、GetUploadFileList(getAll=true)、CreateFolder、CopyFile、RenameFile、MoveFile。CopyFile/MoveFile 显式使用 conflictPolicy=Skip(2)，不使用默认 Overwrite(0)。CloudDriveFile 的 fileHashes 字段 70 为 map，Sha1 键为 2；上传 statusEnum 字段 8 的 Finish 为 5。代码只接受可核验的云端 ID、SHA1、大小及上传状态。

- Radarr 导入脚本契约：https://github.com/Radarr/Radarr/blob/master/src/NzbDrone.Core/MediaFiles/ScriptImportDecider.cs
- Sonarr 导入脚本契约：https://github.com/Sonarr/Sonarr/blob/main/src/NzbDrone.Core/MediaFiles/ScriptImportDecider.cs
- CD2 调用方式参考：https://github.com/ayanamimcy/115-media-automation/blob/main/internal/provider/clouddrive/client.go

Arr 脚本接收 sourcePath / destinationFilePath 两个参数，以及 Radarr_SourcePath、Radarr_DestinationPath 或 Sonarr_SourcePath、Sonarr_DestinationPath 环境变量。成功输出 [MoveStatus] MoveComplete；明确选择原生处理时输出 [MoveStatus] DeferMove；失败以非零退出码阻止原生文件传输。部署与实际验证边界见 docs/ARR_IMPORT.md。

- CloudDrive2 官方开发者指南：https://www.clouddrive2.com/api/CloudDrive2_gRPC_API_Guide.html
- CloudDrive2 官方 proto 下载：https://www.clouddrive2.com/api/clouddrive.proto
- 官方媒体插件公开的协议字段定义：https://github.com/cloud-fs/clouddrive-mediaserver-plugin/blob/main/src/CloudDrive.MediaServer/Protocol/CloudDriveMessages.cs
- 官方媒体插件 Token 权限说明：https://github.com/cloud-fs/clouddrive-mediaserver-plugin/releases/tag/v1.0.2
- Plex 定向扫描：https://support.plex.tv/articles/201638786-plex-media-server-url-commands/
- Plex 扫描说明：https://support.plex.tv/articles/200289306-scanning-vs-refreshing-a-library/
- Emby 媒体更新接口：https://dev.emby.media/reference/RestAPI/LibraryService.html
- Jellyfin 对应控制器：https://github.com/jellyfin/jellyfin/blob/master/Jellyfin.Api/Controllers/LibraryController.cs
- Jellyfin 本地元数据：https://jellyfin.org/docs/general/server/metadata/
- Windows 原生目录通知：https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-readdirectorychangesw
- Windows 异步结构：https://learn.microsoft.com/en-us/windows/win32/api/minwinbase/ns-minwinbase-overlapped
- Windows 通知记录：https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-file_notify_information
- Python 官方 Windows 版本：https://www.python.org/downloads/windows/

CloudDrive 服务为 clouddrive.CloudDriveFileSrv，端点 GetToken / GetSystemInfo / GetMountPoints / PushMessage。使用 application/grpc-web+proto 和 Authorization: Bearer。PushMessage.type=4 表示文件变化；payload 字段 5 为 FileSystemChange，其字段 1/2/3/4 分别为变化类型、目录标志、旧路径、新路径。GetMountPointsResult 字段 1 包含重复的挂载点；挂载点字段 1/2/9 分别为挂载路径、虚拟源目录、是否已挂载。客户端对未知字段跳过，对压缩帧、超长帧和非零 gRPC 状态明确报错。

Plex 使用 GET /library/sections/{id}/refresh?path=... 和 X-Plex-Token。Emby/Jellyfin 使用 POST /Library/Media/Updated，数据为 Updates 数组，元素包含 Path 和 UpdateType。Jellyfin 使用 Authorization: MediaBrowser Token="..."；Emby 使用 X-Emby-Token。具体服务器版本、挂载点可见性和实际推送订阅仍需在目标机验收。
