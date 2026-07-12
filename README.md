# astrbot_plugin_bktools（BKtools）

当前版本：`v1.6.1`，要求 AstrBot `v4.10.4+`，已验证兼容 AstrBot `v4.25.5`。

## v1.6.1 短视频路由修复

- 普通 `v.douyin.com/短码` 不再被提前认作用户主页。
- 自动解析会先展开抖音短链，最终地址为 `/video/` 时解析单作品，为 `/user/` 时解析主页作品。
- 主页列表自动兼容 `data.items`、`data.works`、`data.aweme_list` 等常见响应结构。
- 自定义主页接口可通过 `short_video.profile_items_path` 指定作品列表字段路径。

## v1.6.0 版本检查与安全更新

- `/bk版本`：任何已启用会话均可查看当前版本和官方仓库。
- `/bk检查更新`：管理员检查 GitHub Releases；没有 Release 时自动读取仓库默认分支版本。
- `/bk更新插件`：管理员从 `jiuhunwl/astrbot_plugin_bktools` 下载并安装更新。
- 更新前验证插件名称、目标版本、必要文件和压缩包路径，拒绝 Zip Slip 路径穿越。
- 替换前自动备份旧文件；安装异常时回滚。更新完成后必须完整重启 AstrBot。
- 后台 `updates.self_update_enabled` 可关闭自更新，`max_download_mb` 可限制更新包体积。

### v1.5.1 兼容性修复

- 修复 AstrBot v4.25.5 的配置对象在 `copy.deepcopy()` 时触发 `'NoneType' object is not callable`，导致所有命令和自动解析无法运行的问题。
- 插件初始化及群级配置合并统一使用普通 Python 配置快照，不再依赖框架配置对象的复制协议。

## v1.5.0 稳定性升级

- 解析任务具有等待、解析、构建、发送、完成、取消和失败状态，停止命令可中断请求及重试等待。
- 解析接口仅对连接失败、超时和 5xx 重试；连续失败达到阈值后临时熔断，避免故障接口拖垮群消息处理。
- 同一会话、同一结果使用幂等发送键，合并转发失败后只回退一次完整 JSON，不逐作品发送。
- JSON 超过文本长度时发送一个 `bktools-result.json` 文件；文件仍无法发送时仅返回一次简短错误。
- HTTP 会话统一复用，接口响应、JSON 文件和运行时缓存均有资源上限。
- 群级高级覆盖不能修改 `security`、`group_control` 和 `runtime_limits`。

新增后台配置：`http_reliability`（重试与熔断）和 `runtime_limits`（响应、JSON 与缓存上限）。旧配置无需迁移。

一个 AstrBot 多功能解析插件，支持短视频解析、网易云搜歌与多平台音乐链接解析。  
插件基于可配置接口工作（默认示例为 [BugPk-Api](https://api.bugpk.com/)），接口地址和 JSON 字段路径都可在 AstrBot WebUI 自定义。

- 仓库地址：<https://github.com/jiuhunwl/astrbot_plugin_bktools>

---

## 功能展示

| 功能 | 说明 |
| --- | --- |
| 短视频解析 | 解析标题、作者、封面、视频/图集、背景音乐等信息，支持消息集合转发 |
| 网易云搜歌 | 支持关键词搜索网易云歌曲并二次点歌 |
| 音乐链接解析 | 支持 QQ 音乐、汽水音乐、酷我、网易云等分享链接解析 |
| 自动触发 | 消息中出现短视频/音乐链接时可自动识别并解析 |
| 消息可定制 | 支持开场语、封面显示、原链接附带、发送方式等配置 |
| 群级控制 | 默认所有群启用，可改为群白名单，并为每个群独立覆盖功能和参数 |
| 安全与治理 | 管理命令权限、URL/SSRF 防护、单群并发限制、任务取消和缓存回收 |

> 注意：关键词搜索仅支持网易云；其他平台仅支持分享链接解析。

---

## 环境要求

1. 已部署 [AstrBot](https://github.com/AstrBotDevs/AstrBot) `v4.10.4` 或更高版本（单群模板配置需要）
2. 安装依赖 `aiohttp`（见 `requirements.txt`）

---

## 安装方法

### 方式一：本地目录安装

1. 克隆或下载本仓库到 AstrBot 插件目录
2. 在 AstrBot 中启用插件 `BKtools`
3. 进入插件配置，按需填写接口与路径字段

### 方式二：WebUI 上传 zip

1. 先安装依赖：在 AstrBot Web 控制台 `控制台 -> Pip` 安装 `aiohttp`
2. 在仓库目录执行：
   - 自动打包：`python package_plugin.py`
   - 手动目录：`python package_plugin.py folder`
3. 上传生成的 zip 到插件管理页面并启用

---

## 命令总览

| 命令 | 示例用法 | 说明 |
| --- | --- | --- |
| `/bk帮助` | `/bk帮助` | 查看插件帮助 |
| `/bk视频` | `/bk视频 <作品链接>` | 解析短视频（别名：`/bktv`） |
| `/bk网易云` | `/bk网易云 稻香` | 网易云关键词搜索（别名：`/bk搜歌`） |
| `/bk点歌` | `/bk点歌 1` | 从最近一次搜索结果中点歌 |
| `/bk音乐` | `/bk音乐 <分享链接>` | 解析音乐平台分享链接 |
| `/bk状态` | `/bk状态` | 查看当前群有效配置、任务和缓存状态（管理员） |
| `/bk诊断` | `/bk诊断` | 检查接口、依赖、Alist 和 URL 安全防护（管理员） |
| `/bk清理缓存` | `/bk清理缓存` | 清理本插件的临时缓存文件 |
| `/bk停止解析` | `/bk停止解析` | 仅停止当前群或私聊会话中的短视频/主页解析 |

补充说明：

- 搜歌后可直接发送数字（如 `1`）快速点歌
- 非网易云平台请发送官方分享链接进行解析
- 可开启自动触发，无需每次手动输入命令
- 开启合并转发后，发送失败会按配置自动重试；全部失败时仅发送接口返回的完整 JSON，不会退化为逐作品发送

---

## 配置说明

插件配置来自 `_conf_schema.json`。设置页按照实际使用频率重新排序：

1. `group_control`：群聊启用范围、私聊开关与单群功能/行为覆盖。
2. `security`：管理命令权限、全局授权用户、单群并发数和缓存回收。
3. `trigger`：自动解析短视频、主页和音乐链接，以及链接去重。
4. `message`：解析提示、合并转发重试、媒体展示数量、原始链接和音乐语音消息。
5. `short_video`：短视频接口地址；自定义字段路径通过“显示高级字段映射”展开。
6. `video_threshold` / `batch_output`：大视频发送策略与大量图集、实况的 JSON 输出策略。
7. `netease` / `link_only_music`：网易云搜索解析及 QQ、汽水、酷我音乐接口。
8. `alist`：可选的 Alist 视频上传配置，关闭时自动隐藏账号与上传参数。
9. `http` / `debug`：请求超时、User-Agent 与排错日志。

设置页支持动态显示：关闭某项功能后，与它相关的参数会自动隐藏；接口字段映射默认不展开，普通用户通常只需确认接口地址即可。数值配置使用滑块与数字输入组合，JSON 附加参数提供代码编辑器。

### 群聊启用与单群配置

- 默认模式为“所有群默认启用”，不添加任何单群配置时，插件会继续在所有群正常工作。
- 如需只在部分群启用，将 `group_control.mode` 改为“仅单群配置列表中的群启用”，然后在“单群配置列表”中添加群号。
- 在“所有群默认启用”模式下，也可以添加某个群并关闭 `enabled`，实现单群禁用。
- 每个群可独立开关短视频、抖音主页、网易云和音乐解析，并覆盖自动解析、合并转发、提示语、语音、大视频直链及批量 JSON 行为。
- `override_json` 可覆盖业务配置（不允许覆盖 `group_control` 和 `security`）。示例：`{"message":{"max_images_per_work":3},"video_threshold":{"threshold_mb":50}}`。
- 停止解析、链接去重和解析锁均按群隔离，一个群的操作不会影响其他群。

### 权限与 URL 安全

- `/bk停止解析`、`/bk清理缓存`、`/bk状态` 和 `/bk诊断` 默认仅群主、群管理员及 `security.admin_user_ids` 中的用户可执行。
- 私聊执行管理命令时必须位于全局授权用户列表，或将权限模式明确设为“所有成员”。
- 自动解析只会展开受支持平台的分享链接，不会请求消息中的任意 URL。
- 外部媒体下载会阻止 localhost、内网 IP、云元数据地址、带凭据 URL、非常用端口和跳转到内网的地址。
- 每个群/私聊默认最多同时运行 2 个解析任务，可通过 `max_concurrent_tasks_per_scope` 调整。

### 开发测试

```bash
python -m unittest discover -s tests -v
```

---

## 更新日志

- 请查看 [`CHANGELOG.md`](./CHANGELOG.md)

---

## 接口与合规

- 接口能力与字段结构以 [BugPk-Api](https://api.bugpk.com/) 实际文档为准
- 请遵守平台协议与当地法律法规，勿将解析结果用于侵权或未授权传播场景

---

## 致谢

- AstrBot 插件模板：[Soulter/helloworld](https://github.com/Soulter/helloworld)
- 解析与转发思路参考：[drdon1234/astrbot_plugin_media_parser](https://github.com/drdon1234/astrbot_plugin_media_parser)
- 接口文档与示例：[BugPk-Api](https://api.bugpk.com/)

---

## 许可证

本项目采用 `AGPL-3.0`，详见仓库内 `LICENSE`。

---

## 作者

- **jiuhunwl**（<https://github.com/jiuhunwl>）

---

## Star History

[![Star History Chart](https://api.star-history.com/chart?repos=jiuhunwl/astrbot_plugin_bktools&type=date&legend=top-left)](https://www.star-history.com/?repos=jiuhunwl%2Fastrbot_plugin_bktools&type=date&legend=top-left)
