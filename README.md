# astrbot_plugin_bktools（BKtools）

基于 [BugPk-Api](https://api.bugpk.com/) 等可配置接口的 AstrBot 插件：**短视频解析**（支持合并转发/消息集合，思路参考 [astrbot_plugin_media_parser](https://github.com/drdon1234/astrbot_plugin_media_parser)）、**网易云音乐搜索**、**多平台音乐链接解析**。接口地址与返回 JSON 的字段路径均可在 AstrBot 控制台配置。

**仓库**：[https://github.com/jiuhunwl/astrbot_plugin_bktools](https://github.com/jiuhunwl/astrbot_plugin_bktools)

---

## 功能概览

| 能力 | 说明 |
|------|------|
| 短视频解析 | 调用可配置的解析接口（默认示例为 BugPk `short_videos`），解析标题、作者、封面、视频/图集、背景音乐等；支持 `Video`/`Image` 节点与合并转发（Nodes） |
| 网易云搜索 | **仅网易云**支持关键词搜索（默认已预置 `https://api.bugpk.com/api/163_music`，也可改为自建接口） |
| 音乐链接解析 | **QQ 音乐、汽水音乐、酷我、网易云**等：仅支持**分享链接**解析直链与元数据，不支持关键词搜索（除网易云外） |
| 开场语 | 可开关、可自定义文案 |
| 自动触发 | 可选：消息内出现短视频域名或音乐链接时自动处理 |

---

## 环境要求

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) 已部署并可加载插件  
- Python 依赖见 `requirements.txt`（需安装 **`aiohttp`**）

---

## 更新日志

### v1.1.0 (2026-04-17)
- ✨ 新增插件图标（BugPk API 官方图标）
- 🔖 版本号同步更新
- 📝 文档优化

---

## 安装

1. **安装依赖**  
   在 AstrBot Web控制台 → **控制台 → Pip**，安装：`aiohttp`（版本建议与 `requirements.txt` 一致）。

2. **安装插件**  
   - 将本仓库克隆或下载到 AstrBot 插件目录；或  
   - Web 控制台 **上传 zip**（二选一）：  
     - **自动 zip：** 仓库根目录执行 **`python package_plugin.py`**，上传生成的 **`astrbot_plugin_bktools.zip`**；  
     - **手动压缩：** 执行 **`python package_plugin.py folder`**，在生成的 **`manual_upload/astrbot_plugin_bktools/`** 里已是完整插件文件；回到 **`manual_upload`** 目录，**右键文件夹 `astrbot_plugin_bktools` → 发送到 → 压缩(zipped)文件夹**，再上传该 zip（不要进入文件夹只压内部文件）；或  
   - 若已上架插件市场，按市场说明搜索 `astrbot_plugin_bktools` 安装。

3. **启用插件**  
   在插件管理中启用 **BKtools**，并在 **插件配置** 中填写各接口地址与 JSON 路径（见下文）。

---

## 命令一览

| 命令 | 说明 |
|------|------|
| `/bk帮助` | 显示简要说明 |
| `/bk视频 <作品链接>` | 短视频解析（别名：`/bktv`） |
| `/bk网易云 <关键词>` | 网易云音乐搜索（别名：`/bk搜歌`） |
| `/bk点歌 <序号>` | 选择最近一次网易云搜索结果并返回该歌曲解析 |
| `/bk音乐 <分享链接>` | 音乐平台链接解析（QQ / 汽水 / 酷我 / 网易等） |

说明：**搜歌仅网易云**；搜索后可直接发送数字序号（如 `1`）或用 `/bk点歌 1` 获取对应歌曲。其他平台请直接发官方分享链接走 `/bk音乐` 或开启自动音乐链解析。

---

## 配置说明

插件通过根目录 **`_conf_schema.json`** 在 WebUI 中生成表单，主要分组如下：

### `short_video`（短视频）

- **endpoint**：解析接口地址（如 `https://api.bugpk.com/api/short_videos`）  
- **url_param_name**：链接参数名（一般为 `url`）  
- **path_***：业务码、消息、`data` 根路径及标题、作者、封面、视频、图集、`live_photo`、背景音乐等 **JSON 点号路径**（与 BugPk 返回结构对齐即可）

### `netease`（网易云）

- **search_endpoint**：网易云搜索接口地址（默认 `https://api.bugpk.com/api/163_music`；也可替换为你的接口 URL）  
- **search_extra_params_json**：搜索时附加参数（默认含 `limit`、`offset`；关键词由插件写入 `keywords`）  
- **search_list_path**：`data.songs`；单曲字段默认 `name`、`artists`、`album`、`id`  
- **link_parse_endpoint**：网易云链接解析接口地址（默认同上；留空会自动使用 **search_endpoint**）  
- **link_parse_extra_params_json**：链接解析附加参数（默认 `level=standard`，可按你的接口需要调整）  
- **parse_***：`data` 下 `name`、`ar_name`、`al_name`、`pic`、`url`、`lyric`、`size`、`level` 等  

默认配置可直接使用；若你使用自建接口，按实际返回字段调整 `*_path` / `parse_*` 即可。

### `link_only_music`（仅链接解析）

QQ / 汽水 / 酷我等：`qq_endpoint`、`qishui_endpoint`、`kuwo_endpoint` 及各自 `data` 下标题、作者、封面、音频、歌词等路径（可按 [BugPk 文档](https://api.bugpk.com/) 调整）。

### `message`（消息行为）

- **opening_enable / opening_text**：开场语  
- **pack_forward**：是否合并转发（消息集合）  
- **short_video_text_metadata**：是否发送短视频文字摘要  
- **pack_send_video**：是否使用 `Video.fromURL`（失败会回退为文本直链）  
- **pack_include_cover / pack_append_original_link**：封面与原始链接  
- **max_images_per_work / search_result_limit**：图集条数与搜歌展示条数  

### `trigger`（自动触发）

- **auto_short_video**：消息中含抖音/快手/B站等链接时自动解析短视频  
- **auto_music_link**：消息中含音乐平台链接时自动链解析  

### `http`

- **timeout_sec**、**user_agent**：请求超时与 UA  

---

## 接口与合规说明

- BugPk 为公益接口平台，具体能力、参数与返回字段以 **[api.bugpk.com](https://api.bugpk.com/)** 当前文档为准；站点对 QPS 等有限制，请合理使用。  
- 解析内容与版权归原平台及权利人所有，请遵守当地法律法规与平台用户协议，勿用于未授权传播或商用侵权场景。

---

## 开发与致谢

- 插件模板与 AstrBot 生态：[Soulter/helloworld](https://github.com/Soulter/helloworld)  
- 合并转发与媒体节点思路参考：[drdon1234/astrbot_plugin_media_parser](https://github.com/drdon1234/astrbot_plugin_media_parser)  
- 接口示例与文档：[BugPk-Api](https://api.bugpk.com/)



## 许可证

本仓库默认与上游模板保持一致，采用 **AGPL-3.0**（见仓库内 `LICENSE`）。若你修改并分发，请遵守 AGPL 义务。

---

## 作者

**jiuhunwl** — [astrbot_plugin_bktools](https://github.com/jiuhunwl/astrbot_plugin_bktools)
