# Changelog

本项目的所有重要更新都会记录在这里。

格式参考 Keep a Changelog，并遵循语义化版本管理思路。

## [Unreleased]

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

