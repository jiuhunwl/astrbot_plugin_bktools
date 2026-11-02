# astrbot_plugin_bktools（BKtools）

当前版本：`v1.7.12`，要求 AstrBot `v4.10.4+`，已验证兼容 AstrBot `v4.25.5`。

## v1.7.12 卡片版式与权限修复

- 修复卡片**标题重复**：标题只出现在 `## 🎬 标题` 头部，正文不再多出一行「标题」
- 调整卡片顺序：作者/平台/原声等字段在前，时长/大小/画质等引用块在后，更接近播放器卡片
- 修复 QQ 官方机器人管理命令**误报「权限不足」**：官方机器人只返回 openid（不是 QQ 号），
  现在优先采信 AstrBot 全局管理员身份，并在拒绝时回显真实用户 ID 供填表
- `security.admin_user_ids` 兼容逗号/空格/分号分隔的字符串写法
- `message.official_buttons` **默认开启**：卡片默认带按钮；未授权 keyboard 时自动降级为
  无按钮卡片，并冷却 10 分钟再重试

## v1.7.11 卡片自检与官方机器人配置指引

- 新增 `/bk卡片测试 [封面URL]`（管理员）：直接发一张 Markdown 卡片样张，用来单独验证
  「Markdown 是否渲染 / 封面能否内嵌 / 按钮 keyboard 权限是否可用」，与解析流程解耦
- `/bk诊断` 增加平台适配器能力明细与输出配置快照，可一眼看出当前是否被判为官方机器人、
  Markdown 能力位是否打开、`official_markdown` / `official_buttons` 实际取值
- AstrBot 老版本不支持 `use_markdown` 时自动关闭 Markdown 排版，避免把 `##`、`**`、`![]()`
  原样发出去

### 官方机器人配置清单（插件侧）

| 配置项 | 建议值 | 说明 |
| --- | --- | --- |
| `message.pack_forward` | `true` | 必须开启才会走「卡片 + 按钮」路径 |
| `message.official_markdown` | `true` | 官方机器人卡片版式总开关 |
| `message.official_buttons` | `true` | 卡片底部按钮，需开放平台 keyboard 权限；失败自动降级为无按钮卡片 |
| `message.pack_include_cover` | `true` | 封面会内嵌进卡片；关闭则卡片无图 |
| `message.custom_adapter_caps` | 留空 | 仅自定义适配器需要，内置换算已覆盖 `qq_official` |

### 官方机器人配置清单（QQ 开放平台侧）

1. 机器人管理端开启「**接收所有消息**」（群消息全量模式 `GROUP_MESSAGE_CREATE`），否则群里
   必须 @ 机器人才能触发解析；
2. 目标群由**群主**在「群设置 → 机器人 → 消息范围」改为「**接收群内全部消息**」，每个群单独设置；
3. 在开放平台申请 **keyboard 按钮**权限（否则按钮消息发送失败，插件自动降级为无按钮卡片）；
4. 原生 Markdown 已于 2026/04 对全部机器人开放，无需单独申请模板；
5. 修改上述配置后需**重启 AstrBot 进程**（上传 zip 只解压文件，Python 不会重新加载已导入模块）。

## v1.7.8 官方机器人播放器卡片

- 解析卡片重构为**播放器卡片**版式：`## 🎬/🎵 标题` 头部 + 内嵌封面图（视频 560x315、音乐 300x300，开放平台自动转存公网图片）+ 字段加粗 + 时长/大小/画质块引用 + 分割线
- 依据 QQ 官方文档：群聊/单聊原生 Markdown 已对全部机器人开放（2026/04 起），无需申请；卡片失败自动兜底纯文本
- 按钮 emoji 化：▶ 播放视频 / 🎵 播放原声 / ▶ 播放音乐 / 📄 歌词 / 🔗 原始链接 / 🏠 主页；开启按钮时长链接不再占用正文版面
- 已内嵌的封面不再单独发一条图；`official_markdown` 默认开启即可获得新版式

## v1.7.7 音乐解析 2.0 格式适配

- 适配《音乐解析统一 JSON 格式 2.0》：字段默认路径改为 2.0 字段名优先（`name`/`singer`/`album`/`cover`/`label`/`br`），1.0 旧字段全部保留兜底，未升级的后端不受影响
- 音乐卡片新增：**时长**（毫秒换算）、**音质档**多档摘要（`qualities[]`，locked 档标注无权限）、MV 类型行、**试听片段**提示（`preview: true`）
- `label`/`level` 缺失时按 `quality_type` 统一编号映射中文音质标签；歌词为空回退翻译歌词 `tlyric`
- 修复专辑兜底顺序：1.0 的 `al_name` 优先于 `albumname`（汽水 1.0 里后者存的是曲名）

## v1.7.6 官方机器人卡片+按钮

- QQ 官方机器人输出升级为**卡片 + 按钮**完整形态：Markdown 卡片（字段标签加粗）+ 跳转按钮行
- 按钮自动从卡片提取：视频直链、原声链接、音频直链、歌词链接等链接行都会变成对应按钮（「原始链接」排最前，单行最多 5 个，超长直链自动跳过）
- **正常解析**、批量输出摘要、**音乐解析**三条路径均支持；音乐解析在官方机器人上不再误发 Nodes 合并转发（此前会异常）
- 卡片+按钮发送失败（未申请 keyboard 权限）自动降级为无按钮 Markdown 卡片，结果必达
- 开关：`message.official_buttons`（默认关闭，需官方平台 keyboard 权限）、`message.official_markdown`（默认开启）

## v1.7.5 官方机器人 Markdown 输出优化

- QQ 官方机器人（`qq_official`）发送摘要卡片时按 **Markdown 排版**（字段标签加粗，如 `**标题**：xxx`），观感更好；机器人未获得原生 Markdown 权限时 AstrBot 会自动回退纯文本，开启无副作用
- JSON 原文分片**强制纯文本**发送，避免 JSON 被 Markdown 渲染破坏
- 可选 `message.official_buttons`（默认关闭）：批量输出摘要卡片附带**「原始链接」跳转按钮**（inline keyboard）。需在 QQ 开放平台申请 keyboard 权限；未授权时发送失败会自动降级为无按钮摘要重发，AstrBot 版本过旧无按钮组件时自动停用
- 新增 `message.official_markdown`（默认开启）开关；适配器能力表新增 `markdown` 位，`message.custom_adapter_caps` 可按平台自定义覆盖

## v1.7.1 引用消息链接修复

- 修复 QQ 引用消息解析时传参残缺（`{"url":["https://v.douyin.com/WM5"]}`）的问题：客户端引用长文本会在约第 79 字符处截断并追加 `...`，插件曾把去掉省略号的残链当作完整链接送接口
- 现在会读取引用消息（Reply 组件）里被引用消息的**完整原文**参与选链，完整链接优先于截断链接
- 尾部连续省略号的链接会被判定为"疑似截断"（残链）；当只剩残链时不再白白请求接口，改为提示用户直接发送完整链接
- 用户自己发送的完整链接始终优先于引用内容
- 可选模块（`campaign_manager` 等 4 个）缺失时不再导致**整个插件加载失败**，只停用活动发放并给出提示，短视频/音乐解析照常工作
- 修复 v1.7.0 的 `self._campaign_mgr` 属性遮蔽同名方法导致 `/bk领奖`、`/bk活动` 必抛 `TypeError` 的问题

## v1.7.0 开放 API 活动发放

- 新增 `open_api` 与 `campaigns` 两组配置，群里用 `/bk领奖 <活动ID>` 领取活动奖励。
- 双门禁：GitHub star + 问卷（卷王 / 自定义 HTTP provider），两个条件都通过才发奖。
- 本地防刷：同会话冷却、每日领取上限、按用户 / IP 次数限制；参与记录原子写入且只增不删。
- 新增管理命令：`/bk开放Key`（校验接口 Key 连通性与发奖权限）、`/bk活动`（活动列表 / `重载`）。
- `/bk状态` 与 `/bk诊断` 增加活动发放就绪状态与开放 API Key 校验结果。
- 发放链路一律 fail-closed：配置缺失、Key 无发奖权限、provider 未配置、网络或结构异常都按「暂不可用」收敛，绝不放行。

## v1.6.6 音乐卡片修复

- 平台名改为中文展示：`平台：qishui` → `平台：汽水音乐`。
- 上游返回 `未知艺术家` / `未知歌手` / `unknown` / `-` 等占位符时不再原样展示。
- 标题/艺人/封面/音频/专辑/歌词改为多路径兜底，修复 `曲名：—`。
- 音乐卡片补充大小与音质（格式 · 音质等级 · 码率），汽水音乐自动读取 `video_meta`。
- 音乐卡片补充大小与音质后，音频/歌词直链始终完整展示（v1.7.2 起，截断展示已移除——截断后的链接复制出去无法使用）。
- 接口未返回音频直链时给出友好提示，不再发送空卡片。

## v1.6.5 整合卡片与批量打包

- 整合卡片信息更完整，按统一响应契约展示：标题、类型、作者、时长、大小、画质、图集数、实况数与实况封面数、标签、原声、原声链接、发布时间、原始链接。
- 视频按 `size` 自动换算大小（1024 进制，如 `37.43MB`）、按 `duration` 格式化时长（`00:15`）；`create_time` 毫秒时间戳自动归一化并按 Asia/Shanghai 展示。
- 修复大批量输出阈值配置留空（`None`）时 `int()` 抛错导致解析失败的问题。
- 超过图集/实况阈值时，改为发送「可读摘要卡片 + 完整接口响应」的合并转发打包消息，不再把原始 JSON 当普通文本刷屏。
- 发送完整响应前自动遮蔽 `token`/`api_key`/`cookie` 等凭据字段，避免群聊泄露。
- 解析失败分类提示：接口/网络/超时故障给友好中文提示且不暴露响应码，业务失败透传接口 `msg`，无可解析内容时优先透传接口说明。

## v1.6.4 多平台适配与发送可靠性

- 微信系平台（gewechat/wechatpadpro 等）不再发送合并转发（避免渲染为「聊天记录」卡片）：作品打包降级为逐条普通消息，长 JSON 自动分条文本发送。
- 解析接口业务失败（如 429 满载）只发送友好提示，不再把原始 JSON（可能含接口信息）发到聊天。
- 新增自定义请求头：`http.headers` 全局 + `short_video/netease/link_only_music.headers` 接口级覆盖。
- 新增 `send_reliability` 配置：发送超时/失败策略（连接类错误重试、超时延迟送达提示、失败后释放去重标记）。
- 解析失败的错误消息统一脱敏，接口 key/URL 不再泄露到聊天。

## v1.6.3 大批量 JSON 发送修复

- 修复 AstrBot 与 OneBot/NapCat 分离部署时，适配器无法读取 AstrBot 容器 `/tmp/bktools_json_*.json` 而返回 `ENOENT` 的问题。
- 超过图集或实况阈值后，短 JSON 仍发送单条文本；长 JSON 改为一条合并转发中的连续文本分片。
- 不再使用本地临时 JSON 文件，因此不要求 AstrBot 与消息适配器共享文件系统。
- JSON 仍保持完整接口响应，不会降级为逐个作品或逐张图片发送。
- 新增 JSON 总输出大小与最大分片节点数配置。

## v1.6.2 合并消息重复发送修复

- 合并转发现在对平台发送接口采用“至多一次”策略。
- `event.send()` 抛错时，平台可能已经收到消息；插件不会再盲目重试或追加 JSON，从而避免一次解析出现三份打包消息。
- 只有消息在调用发送接口前构建失败时，才会确定性地回退为一条完整 JSON。
- 旧版 `pack_retry_count` 和 `pack_retry_delay_ms` 配置继续保留以兼容现有配置，但不再触发不安全的发送重试。

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
| `/bk领奖` | `/bk领奖 <活动ID>` | 领取已配置的活动奖励（需先通过 star / 问卷门禁） |
| `/bk状态` | `/bk状态` | 查看当前群有效配置、任务和缓存状态（管理员） |
| `/bk诊断` | `/bk诊断` | 检查接口、依赖、Alist、开放 API 和 URL 安全防护（管理员） |
| `/bk开放Key` | `/bk开放Key` | 校验开放 API Key 的连通性与发奖权限（管理员） |
| `/bk活动` | `/bk活动` / `/bk活动 重载` | 查看活动列表，或按当前配置强制重建活动配置（管理员） |
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
10. `campaigns`：活动模板列表，每个活动对应一段门禁与一个奖励类型。
11. `open_api`：开放 API 活动发放配置（默认关闭），包括平台地址、接口 Key、GitHub token、冷却与问卷 provider。

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

### 活动发放（开放 API）

活动发放默认**关闭**，且采用双重开关：插件的 `open_api.enabled` 与平台后台的开放 API 开关任一为关，整体即不可用。

- **活动模板（`campaigns`）**：每条活动填 `id`（用户领取时用的标识）与 `type_code`（平台兑换码类型代码，如 `NEWBIE`）。`star` 填 `owner`/`repo` 后要求用户已 star 该仓库；`survey` 填 `provider`/`form_id` 后要求用户提交过对应问卷。两个条件都配置时须**同时**满足才发奖。
- **门禁 fail-closed**：发放链路任何一步失败（配置缺失、Key 无发奖权限、provider 未配置、网络或结构异常）都按「暂不可用」收敛，绝不放行。GitHub token 缺失时 star 条件一律不通过。
- **Key 权限**：发奖必须使用管理员作用域 Key（`opka_`）；普通用户 Key（`opk_`）只有读写自身数据的权限，用它配置时活动会直接停用并提示。
- **本地防刷**：`cooldown_sec`（同会话冷却）、`max_claims_per_chat_day`（同会话每日上限）、`per_user_limit` / `per_ip_limit`（按用户 / IP 次数）。参与记录写入 `state_file`（默认 `data/plugin_data/astrbot_plugin_bktools/campaign_state.json`），原子写入且**只增不删**，用于本地防刷与审计。
- **问卷 provider**：在 `open_api.survey_providers` 中配置，支持 `juanwang`（卷王）与 `http_generic`（对接任意返回成功标志的 HTTP 接口）。有活动开启问卷校验但未配置 provider 时，活动整体停用，不会跳过校验。
- **联系方式**：只进入问卷请求体，不落日志、不落状态文件。
- **配置生效**：修改配置后活动配置会按指纹自动重建；用 `/bk活动 重载` 可强制立即重建。

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
