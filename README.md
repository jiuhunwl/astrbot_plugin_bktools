# astrbot_plugin_bktools（BKtools）

一个 AstrBot 多功能解析插件，支持短视频解析、网易云搜歌与多平台音乐链接解析。  
插件基于可配置接口工作（默认示例为 [BugPk-Api](https://api.bugpk.com/)），接口地址和 JSON 字段路径都可在 AstrBot WebUI 自定义。

- 仓库地址：<https://github.com/jiuhunwl/astrbot_plugin_bktools>
- 参考文档风格：[astrbot_plugin_group_geetest_verify](https://github.com/VanillaNahida/astrbot_plugin_group_geetest_verify)

---

## 功能展示

| 功能 | 说明 |
| --- | --- |
| 短视频解析 | 解析标题、作者、封面、视频/图集、背景音乐等信息，支持消息集合转发 |
| 网易云搜歌 | 支持关键词搜索网易云歌曲并二次点歌 |
| 音乐链接解析 | 支持 QQ 音乐、汽水音乐、酷我、网易云等分享链接解析 |
| 自动触发 | 消息中出现短视频/音乐链接时可自动识别并解析 |
| 消息可定制 | 支持开场语、封面显示、原链接附带、发送方式等配置 |

> 注意：关键词搜索仅支持网易云；其他平台仅支持分享链接解析。

---

## 环境要求

1. 已部署可用的 [AstrBot](https://github.com/AstrBotDevs/AstrBot)
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

补充说明：

- 搜歌后可直接发送数字（如 `1`）快速点歌
- 非网易云平台请发送官方分享链接进行解析
- 可开启自动触发，无需每次手动输入命令

---

## 配置说明

插件配置来自 `_conf_schema.json`，WebUI 中主要分为以下模块：

### `short_video`（短视频）

- `endpoint`：短视频解析接口
- `url_param_name`：链接参数名（通常为 `url`）
- `path_*`：返回 JSON 字段路径（状态码、标题、作者、封面、视频、图集等）

### `netease`（网易云）

- `search_endpoint`：网易云搜索接口
- `search_extra_params_json`：搜索附加参数
- `search_list_path` 与歌曲字段路径
- `link_parse_endpoint`：网易云链接解析接口（可与搜索接口相同）
- `parse_*`：歌曲名、作者、封面、音频、歌词等字段路径

### `link_only_music`（链接解析）

- `qq_endpoint`、`qishui_endpoint`、`kuwo_endpoint` 等平台接口
- 各平台 `data` 下标题、作者、封面、音频、歌词字段路径

### `message`（消息行为）

- `opening_enable` / `opening_text`：开场语
- `pack_forward`：是否消息集合转发
- `pack_send_video`：是否使用视频节点发送
- `pack_include_cover` / `pack_append_original_link`：附带封面/原链接
- `max_images_per_work`、`search_result_limit`：条数控制

### `trigger`（自动触发）

- `auto_short_video`：自动短视频解析
- `auto_music_link`：自动音乐链接解析

### `http`

- `timeout_sec`：请求超时
- `user_agent`：请求 UA

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
