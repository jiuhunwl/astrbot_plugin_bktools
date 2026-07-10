# astrbot_plugin_bktools（BKtools）

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
| `/bk清理缓存` | `/bk清理缓存` | 清理本插件的临时缓存文件 |
| `/bk停止解析` | `/bk停止解析` | 停止当前正在运行的短视频/主页解析并截断后续输出 |

补充说明：

- 搜歌后可直接发送数字（如 `1`）快速点歌
- 非网易云平台请发送官方分享链接进行解析
- 可开启自动触发，无需每次手动输入命令
- 开启合并转发后，发送失败会按配置自动重试；全部失败时仅发送接口返回的完整 JSON，不会退化为逐作品发送

---

## 配置说明

插件配置来自 `_conf_schema.json`。设置页按照实际使用频率重新排序：

1. `trigger`：自动解析短视频、主页和音乐链接，以及链接去重。
2. `message`：解析提示、合并转发重试、媒体展示数量、原始链接和音乐语音消息。
3. `short_video`：短视频接口地址；自定义字段路径通过“显示高级字段映射”展开。
4. `video_threshold` / `batch_output`：大视频发送策略与大量图集、实况的 JSON 输出策略。
5. `netease` / `link_only_music`：网易云搜索解析及 QQ、汽水、酷我音乐接口。
6. `alist`：可选的 Alist 视频上传配置，关闭时自动隐藏账号与上传参数。
7. `http` / `debug`：请求超时、User-Agent 与排错日志。

设置页支持动态显示：关闭某项功能后，与它相关的参数会自动隐藏；接口字段映射默认不展开，普通用户通常只需确认接口地址即可。数值配置使用滑块与数字输入组合，JSON 附加参数提供代码编辑器。

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
