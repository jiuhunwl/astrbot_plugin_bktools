# Changelog

本项目的所有重要更新都会记录在这里。

格式参考 Keep a Changelog，并遵循语义化版本管理思路。

## [Unreleased]

## [v1.1.5] - 2026-04-18

### Added

- **自动解析功能增强**：支持短链接重定向解析，根据真实URL类型分配解析接口
- **新增 `_resolve_short_url` 函数**：自动解析短链接获取真实URL

### Fixed

- 修复 `user_id` 判断逻辑错误导致的自动解析不执行问题
- 修复合并转发消息发送失败时崩溃的问题，添加降级处理

### Changed

- 更新版本号至 v1.1.5

## [v1.1.4] - 2026-04-18

### Fixed

- 修复 `v.douyin.com` 短链接被误判为短视频的问题
- 修复机器人自身消息中的链接被重复解析的问题
- 移除解析结果中多余的"原始链接"显示

### Changed

- 更新版本号至 v1.1.4

## [v1.1.3] - 2026-04-18

### Changed

- 简化抖音主页解析配置，复用聚合接口
- 移除作品列表中的重复分享链接显示
- 修复 `_chain_to_forward_nodes` 参数缺失问题
- 更新版本号至 v1.1.3

## [v1.1.2] - 2026-04-18

### Added

- **语音消息发送功能**：支持将解析的音乐自动转换为语音消息发送（需要安装 `pydub` 和 `ffmpeg` 依赖）
- **配置选项**：`send_voice_message` 和 `voice_max_duration` 配置项
- **抖音主页解析功能**：新增 `/bk主页` 命令，支持解析抖音用户主页作品列表
- **自动触发**：新增 `auto_douyin_profile` 配置项，可自动识别抖音主页链接

### Fixed

- 修复汽水音乐（`qishui`）分享链接解析异常问题，提升链接识别与解析稳定性
- 修复汽水音乐链接被误识别为抖音短视频的问题
- 修复 `SyntaxError: 'return' outside function` 缩进错误
- 修复 `register_star() got an unexpected keyword argument 'icon'` 兼容性问题

### Changed

- 优化汽水音乐链接识别优先级，`music.douyin.com` 和 `qishui.douyin.com` 域名优先识别为音乐
- 更新版本号至 v1.1.2

## [v1.1.1] - 2026-04-17

### Added

- **语音消息发送功能**：支持将解析的音乐自动转换为语音消息发送（需要安装 `pydub` 和 `ffmpeg` 依赖）
- **配置选项**：`send_voice_message` 和 `voice_max_duration` 配置项

### Fixed

- 修复汽水音乐（`qishui`）分享链接解析异常问题，提升链接识别与解析稳定性
- 修复汽水音乐链接被误识别为抖音短视频的问题
- 修复 `SyntaxError: 'return' outside function` 缩进错误
- 修复 `register_star() got an unexpected keyword argument 'icon'` 兼容性问题

### Changed

- 优化汽水音乐链接识别优先级，`music.douyin.com` 和 `qishui.douyin.com` 域名优先识别为音乐
- 更新版本号至 v1.1.1

## [v1.1.0] - 2026-04-16

### Added

- 新增插件图标（BugPk API 官方图标）
- 版本号同步更新
- 文档优化

## [v1.0.0] - 2026-04-15

### Added

- 初始版本发布
- 支持短视频解析（抖音、快手、小红书等）
- 支持网易云搜歌
- 支持音乐链接解析（QQ音乐、汽水音乐、酷我）

