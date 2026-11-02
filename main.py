"""
AstrBot 插件：BugPk 工具集（短视频解析、网易云搜歌、多平台音乐链解析）。
说明：音乐搜索仅走网易云配置；QQ/汽水/酷我等仅支持分享链接解析。
v1.7.0 起新增开放 API 活动发放：/bk领奖 走平台 /open-api/redeem-codes
（GitHub star + 问卷双校验），/bk开放Key 与 /bk活动 为管理命令。
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import tempfile
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from functools import wraps
from collections.abc import Iterable, Mapping
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, quote_plus, urlencode, urljoin, urlparse

import aiohttp

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import File, Image, Node, Nodes, Plain, Record, Video

# 按钮组件（ActionRow/Button）为较新版本 AstrBot 才有的可移植组件，
# QQ 官方机器人适配器会把它转成 inline keyboard。老版本缺失时 fail-soft，
# 只停用按钮功能，不影响其余消息输出。
try:
    from astrbot.api.message_components import ActionRow, Button

    _BUTTONS_READY = True
except Exception:  # pragma: no cover - 旧版 AstrBot 无按钮组件
    ActionRow = None  # type: ignore[assignment]
    Button = None  # type: ignore[assignment]
    _BUTTONS_READY = False
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.event_message_type import EventMessageType

# 开放 API 活动发放是可选功能。部分部署方式（只覆盖 main.py 的手动更新、旧版增量覆盖）
# 会缺少配套模块，曾导致 "No module named 'campaign_manager'" 让整个插件加载失败，
# 连短视频/音乐解析一起不可用。这里对可选模块做 fail-soft 导入：缺失时只停用活动发放。
try:
    from campaign_manager import (
        CampaignManager,
        CampaignStateStore,
        ClaimResult,
    )
    from github_star import GitHubStarVerifier
    import open_api_client as _oca
    from open_api_client import KEY_TYPE_ADMIN, OpenApiClient, OpenApiError
    import survey_adapter as _sa

    _CAMPAIGN_READY = True
except Exception as _campaign_import_exc:  # pragma: no cover - 仅部署缺文件时触发
    CampaignManager = None  # type: ignore[assignment]
    CampaignStateStore = None  # type: ignore[assignment]
    ClaimResult = None  # type: ignore[assignment]
    GitHubStarVerifier = None  # type: ignore[assignment]
    _oca = None  # type: ignore[assignment]
    KEY_TYPE_ADMIN = None  # type: ignore[assignment]
    OpenApiClient = None  # type: ignore[assignment]
    OpenApiError = None  # type: ignore[assignment]
    _sa = None  # type: ignore[assignment]
    _CAMPAIGN_READY = False
    logger.warning(
        "可选模块缺失，活动发放功能已停用（短视频/音乐解析不受影响）: %s: %s",
        type(_campaign_import_exc).__name__,
        _campaign_import_exc,
    )


def _patch_astrbot_dict_config_preservation() -> None:
    """修复 AstrBot 框架缺陷：dict 类型配置项（自由键值对）无法保存/被清空。

    两个独立缺陷：
    1. 老版本（如 v4.25.5）的 DEFAULT_VALUE_MAP 不含 "dict" 类型，
       _config_schema_to_default_config() 遇到 schema 中 "type": "dict" 会直接抛
       TypeError（"不受支持的配置类型 dict"），导致插件配置无法初始化，WebUI
       里保存请求头不生效。补丁向 DEFAULT_VALUE_MAP 原地注入 "dict": {}（与原
       dict 对象同一引用，老代码处立即生效），master 及后续版本本身已支持，
       补丁幂等无害。
    2. AstrBotConfig.check_config_integrity() 会把 schema 中默认值为空 dict 的
       配置项（如 short_video.headers / http.headers 等 "type": "dict" 项）当作
       “子配置”递归白名单清理：用户添加的键不在参考配置中，判定为“多余键”被
       丢弃，同时 has_new=True 触发立即写回，导致用户保存的请求头在插件热
       重载后永久丢失。补丁与官方修复 PR（AstrBotDevs/AstrBot#9516，修复
       issue #9512）等价：参考值为空 dict 时直接保留用户全部键值对，不再递归
       清理；非空参考值（object 子配置）行为保持不变。补丁幂等、可安全重复加载。
    """

    try:
        from astrbot.core.config import default as _cfg_default
        from astrbot.core.config.astrbot_config import AstrBotConfig

        # 缺陷 1：老版本 DEFAULT_VALUE_MAP 不识别 dict / template_list 类型
        # （template_list 自 v4.10.4 引入，活动配置 campaigns 依赖它）
        for _extra_type, _extra_default in (("dict", {}), ("template_list", [])):
            if _extra_type not in _cfg_default.DEFAULT_VALUE_MAP:
                _cfg_default.DEFAULT_VALUE_MAP[_extra_type] = _extra_default
                logger.info("[bktools] 已为 DEFAULT_VALUE_MAP 注入 %s 类型支持", _extra_type)

        if getattr(AstrBotConfig, "_bktools_dict_preservation_patched", False):
            return

        def _check_config_integrity(self, refer_conf, conf, path=""):
            has_new = False
            new_conf = {}
            for key, value in refer_conf.items():
                if key not in conf:
                    new_conf[key] = value
                    has_new = True
                elif conf[key] is None:
                    new_conf[key] = value
                    has_new = True
                elif isinstance(value, dict):
                    if not isinstance(conf[key], dict):
                        new_conf[key] = value
                        has_new = True
                    elif not value:
                        # 参考值为空 dict（dict 类型自由键值对配置项）：
                        # 保留用户的全部键值对，不进行递归清理
                        new_conf[key] = conf[key]
                    else:
                        child_has_new = self.check_config_integrity(
                            value,
                            conf[key],
                            path + "." + key if path else key,
                        )
                        new_conf[key] = conf[key]
                        has_new |= child_has_new
                else:
                    new_conf[key] = conf[key]
            for key in list(conf.keys()):
                if key not in refer_conf:
                    path_ = path + "." + key if path else key
                    logger.info("Config key removed: %s", path_)
                    has_new = True
            if list(conf.keys()) != list(new_conf.keys()):
                if path:
                    logger.info("Config key order fixed: %s", path)
                else:
                    logger.info("Config key order fixed")
                has_new = True
            conf.clear()
            conf.update(new_conf)
            return has_new

        AstrBotConfig.check_config_integrity = _check_config_integrity
        AstrBotConfig._bktools_dict_preservation_patched = True
        logger.info(
            "[bktools] 已应用 dict 类型配置补丁（DEFAULT_VALUE_MAP 注入 + 完整性保留，AstrBotDevs/AstrBot#9512 等价修复）"
        )
    except ImportError:
        # 框架版本差异导致无法导入时静默跳过，不影响插件主功能
        logger.info("[bktools] AstrBotConfig 不可导入，跳过 dict 类型配置补丁")
    except Exception as exc:  # pragma: no cover - 防御性兜底
        logger.info("[bktools] dict 类型配置补丁应用失败: %s", exc)


_patch_astrbot_dict_config_preservation()
try:
    from .bktools_runtime import (
        CircuitOpenError,
        RuntimeManager,
        SafeHttpClient,
        TaskState,
    )
except ImportError:
    from bktools_runtime import (
        CircuitOpenError,
        RuntimeManager,
        SafeHttpClient,
        TaskState,
    )
try:
    from .bktools_updater import (
        GITHUB_API,
        REPOSITORY,
        UpdateInfo,
        compare_versions,
        install_archive,
        metadata_version,
    )
except ImportError:
    from bktools_updater import (
        GITHUB_API,
        REPOSITORY,
        UpdateInfo,
        compare_versions,
        install_archive,
        metadata_version,
    )


PLUGIN_VERSION = "1.7.12"


def _cfg(root: Any, *keys: str, default: Any = None) -> Any:
    if not isinstance(root, dict):
        return default
    cur: Any = root
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


# 常见接口鉴权参数名：出现在 URL query 或普通文本中时会被打码，防止接口 key 泄露
_SENSITIVE_QUERY_KEYS = re.compile(
    r"([\s?&](?:key|keys|token|secret|sign|signature|sig|access_key|accesskey|"
    r"api_key|apikey|app_key|appkey|auth|authorization|cookie|password|"
    r"passwd|pwd|credential|cred)=)[^&\s]*",
    re.IGNORECASE,
)
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>，。；]+", re.IGNORECASE)


def _sanitize_user_error(err: Any, limit: int = 200) -> str:
    """把异常/文本转换为可安全展示给用户的内容。

    解析接口 endpoint 常带鉴权参数（key/token/sign 等），异常文本里若混入完整
    接口地址会直接泄露给聊天内所有成员。这里隐藏 URL、对敏感 query 参数打码、
    压缩空白并截断长度；完整错误仍保留在服务端日志中供排查。
    """
    text = str(err or "").strip()
    if not text:
        return "未知错误（详情见日志）"
    if text.startswith("非 JSON 响应"):
        # 接口返回正文可能包含敏感信息，不向用户透传原始内容
        return "接口返回非 JSON 数据（详情见日志）"
    text = _URL_PATTERN.sub("（接口地址已隐藏）", text)
    text = _SENSITIVE_QUERY_KEYS.sub(r"\1***", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit] + "…"
    return text or "未知错误（详情见日志）"


# ── 统一响应契约（video-parser-json-contract）字段格式化 ────────────────────
# 固定使用 Asia/Shanghai 计算 publish_time，避免受服务器本地时区影响；
# 极少数缺少 tzdata 的环境回退到固定 UTC+8。
try:
    from zoneinfo import ZoneInfo as _ZoneInfo

    _CN_TZ: Any = _ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - 缺少 tzdata 时回退
    _CN_TZ = timezone(timedelta(hours=8))

_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")
# 资源类型 → 展示用中文标签（未知类型保留原值）
_RESOURCE_TYPE_LABELS = {"video": "视频", "image": "图集", "live": "实况"}
# 统一契约里可能承载标签的字段路径（按优先级）
_TAG_PATHS = ("extra.tags", "tags", "extra.tag_list", "tag_list", "extra.topics")

# 各平台适配器对消息组件的内置支持能力。
# forward: Nodes 合并转发；file: File 组件发送；markdown: Markdown 富文本消息；
# text_chunk_chars: 普通文本分片长度。
# QQ 官方机器人（qq_official）只支持文本/富媒体且被动回复有长度限制，
# 不支持合并转发与任意文件上传；但支持原生 Markdown 消息（AstrBot 适配器
# use_markdown_ 标记，未获权限时适配器自动回退纯文本）与 inline keyboard 按钮。
# 微信系平台不支持合并转发与文件发送。
_ADAPTER_CAPS_DEFAULT: Dict[str, Dict[str, Any]] = {
    "aiocqhttp": {"forward": True, "file": True, "markdown": False, "text_chunk_chars": 3500},
    "qq_official": {"forward": False, "file": False, "markdown": True, "text_chunk_chars": 1000},
    "wechat": {"forward": False, "file": False, "markdown": False, "text_chunk_chars": 1500},
    "telegram": {"forward": False, "file": True, "markdown": False, "text_chunk_chars": 3500},
    "discord": {"forward": False, "file": True, "markdown": False, "text_chunk_chars": 1800},
    "lark": {"forward": False, "file": True, "markdown": False, "text_chunk_chars": 3000},
    "dingtalk": {"forward": False, "file": False, "markdown": False, "text_chunk_chars": 1500},
}

# 平台名特征 token → 内置能力表键（按序匹配，具体 token 在前）
_ADAPTER_CAPS_MATCHERS: Tuple[Tuple[str, str], ...] = (
    ("aiocqhttp", "aiocqhttp"),
    ("onebot", "aiocqhttp"),
    ("napcat", "aiocqhttp"),
    ("lagrange", "aiocqhttp"),
    ("shamrock", "aiocqhttp"),
    ("qq_official", "qq_official"),
    ("qqofficial", "qq_official"),
    ("qqbot", "qq_official"),
    ("wechat", "wechat"),
    ("weixin", "wechat"),
    ("gewechat", "wechat"),
    ("padpro", "wechat"),
)

# ── 音乐卡片字段规范化 ────────────────────────────────────────────────────
# 音乐平台内部标识 → 展示名（未知标识保留原值，避免出现 "平台：qishui" 这类内部代号）
_MUSIC_PLATFORM_LABELS = {
    "netease": "网易云音乐",
    "qq": "QQ音乐",
    "qishui": "汽水音乐",
    "kuwo": "酷我音乐",
}
# 上游常见的"未知"占位符：与真正缺失等价，不应原样展示给用户
# （汽水音乐部分原声/无版权曲目会返回 artistsname = "未知艺术家"）
_META_PLACEHOLDERS = frozenset(
    {
        "未知", "未知艺术家", "未知歌手", "未知歌曲", "未知作者", "未知名称",
        "未知专辑", "暂无", "无", "none", "null", "nil", "n/a", "na",
        "unknown", "unknown artist", "-", "--", "—", "－",
    }
)
# 音质等级英文 → 中文（未收录的值原样保留）
_MUSIC_QUALITY_LABELS = {
    "standard": "标准", "low": "低", "medium": "中", "high": "高",
    "higher": "较高", "highest": "极高", "exhigh": "极高", "lossless": "无损",
    "hires": "Hi-Res", "sq": "无损", "hq": "高",
}

# 音乐解析统一 JSON 2.0：quality_type（音频跨平台统一编号）→ 中文标签。
# label / level 均缺失时的兜底展示（mv 的 quality_type 走视频编号，不适用本表）。
_MUSIC_QUALITY_TYPE_LABELS = {
    0: "低音质", 1: "标准音质", 2: "较高音质", 3: "极高音质",
    4: "无损音质", 5: "录音室音质", 6: "空间音频",
}


def _format_duration_from_ms(value: Any) -> str:
    """毫秒时长 → mm:ss / hh:mm:ss（音乐 2.0 的 duration_ms 原生单位）。"""
    if isinstance(value, bool):
        return ""
    try:
        seconds = float(value) / 1000.0
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    return _format_duration(int(seconds))


def _summarize_music_qualities(qualities: Any, *, max_items: int = 5) -> str:
    """把 2.0 的 qualities[] 摘要为可读文本："极高音质 1.84MB / 无损音质 8.9MB"。

    只统计有可用地址的档位；locked（无权限）档显示"（无权限）"；超过
    max_items 截断并追加"等"。
    """
    if not isinstance(qualities, list):
        return ""
    parts: List[str] = []
    for item in qualities:
        if not isinstance(item, dict) or len(parts) >= max_items:
            break
        label = _clean_meta(item.get("label")) or _clean_meta(item.get("level"))
        if not label:
            qt = item.get("quality_type")
            label = _MUSIC_QUALITY_TYPE_LABELS.get(qt if isinstance(qt, int) else -1, "")
        if not label:
            continue
        size_text = _clean_meta(item.get("size_label")) or _format_audio_size(
            item.get("size")
        )
        locked = item.get("locked") is True or _clean_meta(
            item.get("locked")
        ).lower() in ("true", "1")
        if locked:
            suffix = "（无权限）"
        elif _clean_meta(item.get("url")):
            suffix = f" {size_text}" if size_text else ""
        else:
            # 无地址且未标记 locked：按无权限处理（契约下 url 空即锁定）
            suffix = "（无权限）"
        parts.append(f"{label}{suffix}")
    if not parts:
        return ""
    text = " / ".join(parts)
    total = len([q for q in qualities if isinstance(q, dict)])
    if total > max_items:
        text += f" 等共{total}档"
    return text

# 「字段标签：值」形态的卡片文本行（QQ 官方机器人 Markdown 排版用）。
# 标签为 2~10 个非空白、非冒号字符；JSON 行、无冒号行不匹配。
_MD_FIELD_LINE_RE = re.compile(r"^([^\s：:]{2,10})(?:：|:)\s*(\S.*)$")
# 「字段标签：URL」整行形态（官方机器人按钮跳转候选行，
# 如 视频直链/原声链接/音频直链/歌词链接；兼容 Markdown 化后的 **标签**： 行）
_BUTTON_LINK_LINE_RE = re.compile(
    r"^(?:\*\*)?([^\s：:*]{2,6})(?:\*\*)?(?:：|:)\s*(https?://\S+)\s*$"
)


def _markdownize_card_text(text: str) -> str:
    """把纯文本摘要卡片转换为 Markdown 排版（仅加粗字段标签）。

    只处理「标签：值」形态的行；URL 行的标签同样加粗（值保持原样，链接仍可
    复制）。JSON 原文行（以 { [ " 开头）原样保留，避免破坏结构。
    非官方机器人平台不应调用本函数——加粗星号会被当普通字符显示。
    """
    out: List[str] = []
    for line in str(text).splitlines():
        stripped = line.lstrip()
        m = _MD_FIELD_LINE_RE.match(line)
        if m and "://" not in m.group(1) and stripped[:1] not in ('{', '[', '"'):
            out.append(f"**{m.group(1)}**：{m.group(2)}")
        else:
            out.append(line)
    return "\n".join(out)


# 官方机器人播放器卡片：URL 字段行 → 按钮 emoji 文案（label 上限 10 字符）
_PLAYER_BUTTON_LABELS = {
    "视频直链": "▶ 播放视频",
    "原声链接": "🎵 播放原声",
    "音频": "▶ 播放音乐",
    "音频直链": "▶ 播放音乐",
    "歌词链接": "📄 歌词",
    "原始链接": "🔗 原始链接",
    "主页链接": "🏠 主页",
}
_PLAYER_MUSIC_FIELDS = ("曲名", "音频")
_PLAYER_QUOTE_FIELDS = ("时长", "大小", "画质", "音质")


def _qq_md_image(url: str, width: int, height: int) -> str:
    """QQ 原生 Markdown 内嵌图片语法（开放平台自动转存公网图片）。

    官方文档格式：![text #宽度px #高度px](图片URL)；URL 非 http(s) 返回空串。
    """
    u = str(url or "").strip()
    if not u.startswith(("http://", "https://")):
        return ""
    return f"![cover #{int(width)}px #{int(height)}px]({u})"


def _render_player_card_md(
    text: str,
    *,
    cover_url: str = "",
    buttons_enabled: bool = False,
) -> str:
    """把摘要卡片文本渲染为 QQ 官方机器人「播放器卡片」Markdown。

    版式（仅官方原生 Markdown 场景使用，语法见 QQ 开放平台文档）：
    - `## 🎬/🎵 标题` 头部（按 卡片是否含 曲名/音频 字段自动识别音乐）；
    - 可选内嵌封面图（视频 560x315、音乐 300x300，开放平台转存公网图片）；
    - 短字段 **加粗** 行；时长/大小/画质/音质收进块引用行；
    - buttons_enabled=True 时 URL 行从正文移除（由 keyboard 按钮代替，
      避免长链接换行破坏版式）；False 时保留为 `**标签**：<URL>` 可点击；
    - 结尾 `***` 分割线。解析失败时退回 Markdownize 结果。
    """
    fields: List[Tuple[str, str]] = []
    extra: List[str] = []
    for line in str(text).splitlines():
        s = line.strip()
        if not s:
            continue
        if s[:1] in ("{", "[", '"'):
            extra.append(s)
            continue
        m = _MD_FIELD_LINE_RE.match(s)
        if m and "://" not in m.group(1):
            fields.append((m.group(1).strip("*"), m.group(2).strip()))
        else:
            extra.append(s)
    if not fields:
        return _markdownize_card_text(text)
    label_set = {label for label, _ in fields}
    is_music = any(key in label_set for key in _PLAYER_MUSIC_FIELDS)
    title = ""
    for key in ("曲名", "标题"):
        for label, value in fields:
            if label == key and value and value != "—":
                title = " ".join(value.split())
                break
        if title:
            break
    if len(title) > 32:
        title = title[:32] + "…"
    out: List[str] = [f"## {'🎵' if is_music else '🎬'} {title or '播放卡片'}"]
    img = _qq_md_image(cover_url, 300 if is_music else 560, 300 if is_music else 315)
    if img:
        out.extend(("", img))
    quote_bits: List[str] = []
    body: List[str] = []
    for label, value in fields:
        if label in ("标题", "曲名"):
            continue  # 已作为卡片标题展示，避免重复一行
        if buttons_enabled and value.startswith(("http://", "https://")):
            continue
        if label in _PLAYER_QUOTE_FIELDS:
            quote_bits.append(f"{label} {value}")
        else:
            body.append(f"**{label}**：{value}")
    if body:
        if is_music and extra and any("歌词" in e or len(e) > 40 for e in extra):
            body.append("**歌词预览**")
        out.extend(("", *body))
    if quote_bits:
        out.extend(("", "> " + " · ".join(quote_bits)))
    if extra:
        out.extend(("", *extra))
    out.extend(("", "***"))
    return "\n".join(out)


def _clean_meta(value: Any) -> str:
    """清洗上游元数据：None / 空白 / 占位符统一视为无内容。"""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).strip()
    if not text or text.lower() in _META_PLACEHOLDERS:
        return ""
    return text


def _first_meta(data: Any, paths: Iterable[Any], json_root: Any) -> str:
    """按候选路径顺序取第一个有效值（跳过空值与"未知"占位符）。"""
    for path in paths:
        if not path:
            continue
        value = _clean_meta(_rel_data(data, str(path), json_root))
        if value:
            return value
    return ""


def _format_audio_size(value: Any) -> str:
    """音频大小：纯数字按 1024 进制换算；已是 "26.86 MB" 之类的文本原样保留。"""
    text = _clean_meta(value)
    if not text:
        return ""
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return _format_size_label(text)
    return text


def _format_bitrate(value: Any) -> str:
    """码率：bps → kbps / Mbps 可读文本。"""
    text = _clean_meta(value)
    if not text:
        return ""
    try:
        bps = float(text)
    except (TypeError, ValueError):
        return ""
    if not bps > 0:
        return ""
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.2f}".rstrip("0").rstrip(".") + "Mbps"
    return f"{bps / 1000:.0f}kbps"


def _format_music_quality(value: Any) -> str:
    """音质等级：命中映射表返回中文，否则原样返回。"""
    text = _clean_meta(value)
    if not text:
        return ""
    return _MUSIC_QUALITY_LABELS.get(text.lower(), text)


def _is_http_url(text: Any) -> bool:
    return str(text or "").strip().lower().startswith(("http://", "https://"))


# LRC 时间轴 [01:20.30]、汽水音乐逐字标签 <0,260,0> 与 [1980,1990] 行首时间轴
_LRC_TIMESTAMP_RE = re.compile(r"\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]|\[\d+,\d+(?:,\d+)*\]")
_KARAOKE_TAG_RE = re.compile(r"<\d+(?:,\d+){1,3}>")


def _clean_lyric_preview(lyric: Any, *, max_lines: int = 6, max_chars: int = 300) -> str:
    """把 LRC / 逐字歌词转为可读纯文本预览（去掉时间轴与卡拉OK标签）。"""
    text = str(lyric or "").strip()
    if not text:
        return ""
    candidates: List[str] = []
    for raw in text.splitlines() or [text]:
        line = _KARAOKE_TAG_RE.sub("", _LRC_TIMESTAMP_RE.sub("", raw)).strip()
        if line:
            candidates.append(line)
    if not candidates:
        candidates = [text]
    truncated = len(candidates) > max_lines
    preview = "\n".join(candidates[:max_lines])
    if len(preview) > max_chars:
        return preview[:max_chars] + "…"
    return preview + "…" if truncated else preview


def _extract_music_fields(
    data: Any,
    json_root: Any,
    *,
    title_p: str = "",
    author_p: str = "",
    cover_p: str = "",
    audio_p: str = "",
    lyric_p: str = "",
    album_p: str = "",
    size_p: str = "",
    level_p: str = "",
) -> Dict[str, str]:
    """按统一契约抽取音乐字段：多路径兜底 + 占位符过滤 + 可读化转换。

    配置的字段路径优先，其后跟随各平台常见字段名兜底，避免上游改字段后
    卡片直接变成 `曲名：—`。候选顺序以《音乐解析统一 JSON 格式 2.0》
    字段名（name/singer/album/label/br/duration_ms/qualities[]）为先，
    1.0 旧字段名（albumname/artistsname/ar_name/al_name 等）作为兜底保留。
    """
    data_obj = data if isinstance(data, dict) else {}
    # 2.0：type=music（音频）/mv（音乐视频）；1.0 无此字段
    mtype = str(data_obj.get("type") or "").strip().lower()
    # 2.0：试听片段标记（boolean，_clean_meta 会过滤 bool，需单独读取）
    preview = data_obj.get("preview") is True or _clean_meta(
        data_obj.get("preview")
    ).lower() in ("true", "1")
    qualities_text = _summarize_music_qualities(data_obj.get("qualities"))
    level_raw = _first_meta(
        data, (level_p, "label", "level", "video_meta.quality", "quality"), json_root
    )
    level_text = _format_music_quality(level_raw)
    if not level_text:
        # 2.0：label/level 均缺失时按 quality_type 统一编号映射中文标签
        qt = data_obj.get("quality_type")
        if isinstance(qt, int) and not isinstance(qt, bool):
            level_text = _MUSIC_QUALITY_TYPE_LABELS.get(qt, "")
    return {
        "title": _first_meta(
            data,
            (title_p, "name", "title", "songname", "song_name", "albumname"),
            json_root,
        ),
        "author": _first_meta(
            data,
            (
                author_p, "singer", "artists.0.name", "artistsname",
                "artist_name", "artist", "author", "ar_name", "singers",
            ),
            json_root,
        ),
        "cover": _first_meta(
            data, (cover_p, "cover", "pic", "cover_url", "artistsmedium_avatar_url.0"), json_root
        ),
        "audio": _first_meta(data, (audio_p, "url", "download_url", "music_url"), json_root),
        "lyric": _first_meta(data, (lyric_p, "lyric", "tlyric"), json_root),
        "album": _first_meta(
            # 1.0 汽水的 albumname 存的是曲名而非专辑名，故 al_name 优先于 albumname
            data, (album_p, "album", "al_name", "albumname"), json_root
        ),
        "size": _format_audio_size(
            _first_meta(data, (size_p, "size", "Size", "video_meta.size"), json_root)
        ),
        "format": _first_meta(
            data, ("format", "Format", "video_meta.vtype", "video_meta.codec_type", "Codec"), json_root
        ).upper(),
        "level": level_text,
        "bitrate": _format_bitrate(
            _first_meta(data, ("br", "Bitrate", "bitrate", "video_meta.bitrate"), json_root)
        ),
        "duration": _format_duration_from_ms(
            _first_meta(data, ("full_duration_ms", "duration_ms"), json_root)
        ),
        "preview": "1" if preview else "",
        "mtype": mtype,
        "qualities": qualities_text,
    }


def _format_music_card(plat: str, fields: Mapping[str, str]) -> str:
    """渲染音乐解析卡片：平台名中文化，空字段按契约省略对应行。

    音频/歌词直链始终完整展示（截断后的链接无法复制使用）。
    2.0 新增：type=mv 类型行、时长（毫秒换算）与试听片段提示、
    qualities[] 多音质摘要行。
    """
    lines = [f"平台：{_MUSIC_PLATFORM_LABELS.get(plat, plat)}"]
    lines.append(f"曲名：{fields['title']}" if fields.get("title") else "曲名：—")
    if fields.get("mtype") == "mv":
        lines.append("类型：MV")
    if fields.get("author"):
        lines.append(f"艺人：{fields['author']}")
    if fields.get("album"):
        lines.append(f"专辑：{fields['album']}")
    if fields.get("duration"):
        duration_line = f"时长：{fields['duration']}"
        if fields.get("preview"):
            duration_line += "（试听片段）"
        lines.append(duration_line)
    if fields.get("size"):
        lines.append(f"大小：{fields['size']}")
    quality_bits = [fields[k] for k in ("format", "level", "bitrate") if fields.get(k)]
    if quality_bits:
        lines.append("音质：" + " · ".join(quality_bits))
    if fields.get("qualities"):
        lines.append(f"音质档：{fields['qualities']}")
    lines.append(f"音频：{str(fields.get('audio') or '').strip()}")
    lyric = str(fields.get("lyric") or "")
    if lyric:
        if _is_http_url(lyric):
            lines.append(f"歌词链接：{lyric.strip()}")
        else:
            preview = _clean_lyric_preview(lyric)
            if preview:
                lines.append("歌词预览：\n" + preview)
    return "\n".join(lines)


def _format_size_label(size: Any) -> str:
    """把字节数格式化为 1024 进制标签（≤2 位小数、不留尾随 0）。

    契约示例：39245943 -> "37.43MB"、1048576 -> "1MB"、0 -> ""。
    """
    if isinstance(size, bool):
        return ""
    try:
        value = float(size)
    except (TypeError, ValueError):
        return ""
    if not (value > 0) or value != value or value in (float("inf"), float("-inf")):
        return ""
    index = 0
    while value >= 1024 and index < len(_SIZE_UNITS) - 1:
        value /= 1024.0
        index += 1
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{text}{_SIZE_UNITS[index]}"


def _format_duration(seconds: Any) -> str:
    """把秒数格式化为 mm:ss / hh:mm:ss；非法、非正数返回空串。"""
    if isinstance(seconds, bool):
        return ""
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        return ""
    if not (total > 0) or total != total or total in (float("inf"), float("-inf")):
        return ""
    total_int = int(total)
    hours, remainder = divmod(total_int, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _normalize_unix_seconds(value: Any) -> int:
    """把作品时间戳归一化为 Unix 秒。

    契约要求：≥ 1000000000000 的数值按毫秒处理（除以 1000 并截断小数），
    仅接受可表示、有限的合法时间戳，其余返回 0。
    """
    if isinstance(value, bool):
        return 0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if not (number > 0) or number != number or number in (float("inf"), float("-inf")):
        return 0
    if number >= 1_000_000_000_000:
        number /= 1000.0
    seconds = int(number)
    if seconds < 1 or seconds > 253402300799:  # 9999-12-31 23:59:59 UTC 上界
        return 0
    return seconds


def _format_publish_time(create_time: Any, publish_time: Any = None) -> str:
    """优先采用接口已格式化的 publish_time，否则由 create_time 推导。

    推导结果固定为 Asia/Shanghai 的 YYYY-MM-DD HH:mm:ss；未知返回空串。
    """
    text = str(publish_time or "").strip()
    if text and ("-" in text or ":" in text):
        return text
    seconds = _normalize_unix_seconds(create_time or text)
    if not seconds:
        return ""
    try:
        moment = datetime.fromtimestamp(seconds, tz=_CN_TZ)
    except (OverflowError, OSError, ValueError):
        return ""
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _normalize_resource_type(value: Any) -> str:
    return str(value or "").strip().lower()


def _to_int(value: Any, default: int) -> int:
    """容错地把配置值转为 int：None/空/非数字回退默认值，避免解析崩溃。"""
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# 明确属于凭据的键名 token（按非字母数字切分后做精确匹配，避免误伤
# monkey/author/design 这类普通单词）。
_SENSITIVE_KEY_TOKENS = frozenset(
    {
        "key", "keys", "token", "tokens", "secret", "sign", "signature", "sig",
        "accesskey", "apikey", "appkey", "auth", "authorization", "cookie",
        "cookies", "password", "passwd", "pwd", "credential", "credentials", "cred",
        # 开放 Key（opk_ / opka_）：配置回显与批量输出都要打码
        "openkey", "opk", "opka", "openapi",
    }
)

# 配置回显被脱敏后的占位形态（如 opka_**** / sk-****abcd / ********）：
# 这类值存回配置后根本不能用，必须明确提示，而不是当成有效 Key 静默失败。
_SENSITIVE_REDACTED_KEY_RE = re.compile(r"[*•×]{4,}")

# 活动领取结果 → 聊天文案（GRANTED 单独走成功模板，不在表里）；
# 可选模块缺失时 ClaimResult 为 None，此时活动功能整体停用，表保持为空即可
_CLAIM_USER_TEXT = {
    ClaimResult.CAMPAIGN_NOT_FOUND: "没有找到这个活动 ID，用 /bk活动 列表 查看当前可参与的活动。",
    ClaimResult.CAMPAIGN_DISABLED: "这个活动已经关闭了。",
    ClaimResult.CAMPAIGN_NOT_STARTED: "这个活动还没开始，请稍后再来。",
    ClaimResult.CAMPAIGN_EXPIRED: "这个活动已经结束了。",
    ClaimResult.ALREADY_CLAIMED: "你在这个活动已经领过一次了，同一聊天账号每个活动限领一次。",
    ClaimResult.COOLDOWN: "刚刚领过奖啦，稍微等一会儿再来。",
    ClaimResult.IN_PROGRESS: "上一次领取还在处理中，稍等几秒再试一次。",
    ClaimResult.STAR_NOT_CONFIGURED: "这个活动需要 GitHub 用户名，用法：/bk领奖 <活动ID> <GitHub用户名>",
    ClaimResult.STAR_NOT_STARRED: "还没 star 这个仓库哦，先去 star 再来领奖。",
    ClaimResult.STAR_USER_NOT_FOUND: "没找到这个 GitHub 用户名，请检查后重试（不含空格与大写）。",
    ClaimResult.SURVEY_NOT_CONFIGURED: "这个活动的问卷没配置好，先找管理员处理一下。",
    ClaimResult.SURVEY_NOT_MATCH: "联系方式和问卷记录对不上，请确认填表用的号码/邮箱一致。",
    ClaimResult.SURVEY_UNAVAILABLE: "问卷查询服务暂时繁忙，请稍后再试。",
    ClaimResult.KEY_TYPE_FORBIDDEN: "当前开放 Key 是普通用户 Key，没有发奖权限，请让管理员换成管理员作用域 Key（opka_）。",
    ClaimResult.SCOPE_DENIED: "当前开放 Key 没有发奖权限（scope 不足），请联系管理员。",
    ClaimResult.RATE_LIMITED: "领奖太频繁了，请稍后再试。",
    ClaimResult.IP_FORBIDDEN: "当前网络出口没有加白名单，请联系管理员把出口 IP 加进开放 Key 白名单。",
    ClaimResult.GENERATE_FAILED: "服务暂时繁忙，请稍后再试。",
    ClaimResult.CONFIG_ERROR: "活动配置有误，请联系管理员。",
} if ClaimResult is not None else {}


def _is_sensitive_key(key: str) -> bool:
    tokens = [t for t in re.split(r"[^a-z0-9]+", str(key).lower()) if t]
    if not tokens:
        return False
    if any(t in _SENSITIVE_KEY_TOKENS for t in tokens):
        return True
    # 兼容 apiKey / accessToken 这类无分隔的驼峰或拼接写法
    return "".join(tokens) in _SENSITIVE_KEY_TOKENS


def _redact_sensitive(value: Any, depth: int = 0) -> Any:
    """发送完整响应到聊天前，递归遮蔽明显属于凭据的键值。

    批量输出会把接口完整响应发到群聊，响应中若夹带 token/cookie 等字段
    会构成凭据泄露；这里仅遮蔽键名命中敏感词的项，其余结构原样保留。
    错误提示路径由 _sanitize_user_error 负责，二者互补。
    """
    if depth > 12:
        return value
    if isinstance(value, dict):
        return {
            key: ("***" if isinstance(key, str) and _is_sensitive_key(key)
                  else _redact_sensitive(item, depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_sensitive(item, depth + 1) for item in value)
    return value


# 从 "(upstream: xxx)" 中还原上游接口给出的业务错误文本
_UPSTREAM_DETAIL_RE = re.compile(r"upstream:\s*(.+?)\)\s*$", re.DOTALL)
# 这些是接口的通用成功文案，不代表有效错误信息，命中时改用本地友好提示
_GENERIC_SUCCESS_MSGS = frozenset(
    {"解析成功", "成功", "ok", "success", "succeed", "done", "200"}
)


def _extract_upstream_detail(err: Any) -> str:
    """取出 aiohttp 5xx 异常里已透传的上游 msg 片段并脱敏。

    诊断辅助代码本身不得抛出异常，否则会用一个无关的错误覆盖原始失败；
    因此这里对 message 与 str(err) 分别做保护。
    """
    raw = ""
    try:
        raw = getattr(err, "message", None) or ""
    except Exception:
        raw = ""
    if not raw:
        try:
            raw = str(err or "")
        except Exception:
            raw = ""
    match = _UPSTREAM_DETAIL_RE.search(str(raw))
    if not match:
        return ""
    return _sanitize_user_error(match.group(1), limit=120)


def _describe_parse_error(err: Any, label: str = "解析") -> str:
    """把解析异常转成面向用户的友好提示。

    - 接口/网络故障（5xx、超时、连接失败、熔断）→ 友好提示，不暴露原始响应码；
    - 上游给出了可读 msg → 一并带上，便于用户判断；
    - 业务失败（接口正常返回但 code 非成功）→ 直接透传接口 msg。

    label 用于业务失败兜底文案，例如 label="抖音主页解析" → "抖音主页解析失败：…"。
    """
    if isinstance(err, CircuitOpenError):
        return _sanitize_user_error(err) or "解析服务暂时不可用，请稍后重试"
    if isinstance(err, aiohttp.ClientResponseError):
        status = int(getattr(err, "status", 0) or 0)
        upstream = _extract_upstream_detail(err)
        tail = f"（{upstream}）" if upstream else ""
        if status == 429:
            return f"解析接口请求过于频繁{tail}，请稍后重试"
        if status >= 500:
            return f"解析接口暂时不可用{tail}，请稍后重试"
        if status >= 400:
            return f"解析请求被接口拒绝{tail}，请检查接口配置"
        return f"解析接口返回异常{tail}，请稍后重试"
    if isinstance(err, (asyncio.TimeoutError, aiohttp.ServerTimeoutError)):
        return "解析接口响应超时，请稍后重试"
    if isinstance(err, aiohttp.ClientConnectionError):
        return "无法连接解析接口，请检查网络或接口地址是否正确"
    if isinstance(err, ValueError):
        text = _sanitize_user_error(err, limit=160)
        if text:
            return f"{label}失败：{text}"
    return f"{label}失败，请稍后重试（详情见服务端日志）"


def _empty_result_hint(json_root: Any, cfg_sv: Dict[str, Any]) -> str:
    """接口返回成功但没有可用媒体时，优先透传接口 msg。"""
    msg = str(get_path(json_root, cfg_sv.get("path_msg") or "msg") or "").strip()
    if msg and msg.lower() not in _GENERIC_SUCCESS_MSGS and msg not in _GENERIC_SUCCESS_MSGS:
        return f"未解析到有效内容：{_sanitize_user_error(msg, limit=160)}"
    return "未解析到有效内容（作品可能已删除、设为私密，或该链接暂不支持解析）"


def _plain_config_value(value: Any) -> Any:
    """把 AstrBotConfig/dict 子类转换为普通容器，避免 deepcopy 协议冲突。"""
    if isinstance(value, Mapping):
        return {
            str(key): _plain_config_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_plain_config_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_plain_config_value(item) for item in value)
    if isinstance(value, set):
        return {_plain_config_value(item) for item in value}
    return value


def _deep_merge_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并为普通配置容器，不触发框架对象的 deepcopy 魔术方法。"""
    result: Dict[str, Any] = _plain_config_value(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge_dict(result[key], value)
        else:
            result[key] = _plain_config_value(value)
    return result


def _apply_tri_state(target: Dict[str, Any], path: Tuple[str, str], state: Any) -> None:
    """把 inherit/enable/disable 三态设置写入目标配置。"""
    normalized = str(state or "inherit").strip().lower()
    if normalized not in {"enable", "disable"}:
        return
    section, key = path
    section_cfg = target.setdefault(section, {})
    if isinstance(section_cfg, dict):
        section_cfg[key] = normalized == "enable"


def _event_scoped(feature: Optional[str] = None, *, silent: bool = False):
    """为命令和自动事件应用当前群的启用策略与有效配置。"""

    def decorator(func):
        @wraps(func)
        async def wrapped(self, event: AstrMessageEvent, *args, **kwargs):
            with self._event_runtime(event) as policy:
                if policy is None:
                    if not silent:
                        await self._send_disabled_notice(event)
                    return None
                if feature and not self._feature_enabled(feature):
                    if not silent:
                        await self._send_feature_disabled_notice(event, feature)
                    return None
                return await func(self, event, *args, **kwargs)

        return wrapped

    return decorator


def _tracked_parse(func):
    """登记解析任务并应用当前会话并发限制，供停止命令真正取消。"""

    @wraps(func)
    async def wrapped(self, *args, **kwargs):
        task = None
        try:
            async with self._track_parse_task():
                task = asyncio.current_task()
                self._runtime_manager.set_state(TaskState.PARSING)
                return await func(self, *args, **kwargs)
        except asyncio.CancelledError:
            self._runtime_manager.finish_task(task, TaskState.CANCELLED)
            logger.info("解析任务已取消: scope=%s, handler=%s", self._runtime_scope(), func.__name__)
            return None
        except Exception as ex:
            self._runtime_manager.finish_task(task, TaskState.FAILED)
            self._runtime_manager.set_state(TaskState.FAILED, str(ex))
            raise
        finally:
            self._runtime_manager.cleanup_scope(self._runtime_scope())

    return wrapped


def _management_only(func):
    """限制清理、停止和诊断等管理命令的调用权限。"""

    @wraps(func)
    async def wrapped(self, event: AstrMessageEvent, *args, **kwargs):
        if not await self._is_management_allowed(event):
            sender = self._event_sender_id(event)
            scope_id = self._event_group_id(event)
            logger.info(
                "管理命令被拒绝：%s，用户=%s，会话=%s",
                getattr(func, "__name__", "?"), sender, scope_id or "私聊",
            )
            lines = ["权限不足：该命令仅限授权管理员使用。", f"你的用户 ID：{sender}"]
            if scope_id:
                lines.append(f"当前群 ID：{scope_id}")
            lines.append(
                "把上面的用户 ID 加入「插件配置 → security → 全局授权用户 ID」即可。"
            )
            lines.append(
                "注意：QQ 官方机器人拿到的是 openid（不是 QQ 号），所以按 QQ 号填写不会命中。"
            )
            await event.send(event.plain_result("\n".join(lines)))
            return None
        return await func(self, event, *args, **kwargs)

    return wrapped


def _parse_success_codes(s: str) -> set:
    out: set = set()
    for part in (s or "").replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            out.add(part)
    return out


def get_path(obj: Any, path: str) -> Any:
    if obj is None or not path:
        return None
    cur = obj
    for raw in path.split("."):
        if cur is None:
            return None
        key = raw
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list):
            try:
                idx = int(key)
                cur = cur[idx] if 0 <= idx < len(cur) else None
            except ValueError:
                return None
        else:
            return None
    return cur


def _rel_data(data_root: Any, rel: str, json_root: Dict[str, Any]) -> Any:
    if not rel:
        return None
    if rel.startswith("data.") or rel.startswith("result."):
        return get_path(json_root, rel)
    return get_path(data_root, rel)


def _code_ok(j: Dict[str, Any], path_code: str, ok: set) -> bool:
    c = get_path(j, path_code)
    if c is None and path_code in ("code", "status"):
        c = get_path(j, "code")
        if c is None:
            c = get_path(j, "status")
    if c is None:
        return False
    try:
        c_int = int(c)
        return c_int in ok or str(c) in ok
    except (ValueError, TypeError):
        return c in ok or str(c) in ok


def _first_video_url(data: Any, cfg_sv: Dict[str, Any], json_root: Dict[str, Any]) -> Optional[str]:
    u = _rel_data(data, cfg_sv.get("path_video_url") or "url", json_root)
    if u:
        return str(u)
    bl = _rel_data(
        data, cfg_sv.get("path_video_backup_list") or "video_backup", json_root
    )
    if isinstance(bl, list):
        for x in bl:
            if x:
                return str(x)
    return None


def _live_photo_items(data: Any, cfg_sv: Dict[str, Any], j: Dict[str, Any]) -> List[Dict[str, Any]]:
    """返回实况相册条目列表（原样保留 image/video 配对结构）。"""
    lp = _rel_data(data, cfg_sv.get("path_live_photo_list") or "live_photo", j)
    if not isinstance(lp, list):
        return []
    return [item for item in lp if isinstance(item, dict)]


def _live_photo_videos(data: Any, cfg_sv: Dict[str, Any], j: Dict[str, Any]) -> List[str]:
    sub = (cfg_sv.get("path_live_photo_video") or "video").strip() or "video"
    out: List[str] = []
    for item in _live_photo_items(data, cfg_sv, j):
        u = get_path(item, sub)
        if u:
            s = str(u).strip()
            if s:
                out.append(s)
    return out


def _live_photo_images(data: Any, cfg_sv: Dict[str, Any], j: Dict[str, Any]) -> List[str]:
    """返回实况相册的封面图（image）列表，用于统计与展示。"""
    sub = (cfg_sv.get("path_live_photo_image") or "image").strip() or "image"
    out: List[str] = []
    for item in _live_photo_items(data, cfg_sv, j):
        u = get_path(item, sub)
        if u:
            s = str(u).strip()
            if s:
                out.append(s)
    return out


def _collect_tags(data: Any, cfg_sv: Dict[str, Any], j: Dict[str, Any]) -> List[str]:
    """从接口响应中收集作品标签（去重保序、限制长度）。"""
    configured = str(cfg_sv.get("path_tags") or "").strip()
    paths = [configured] if configured else []
    paths.extend(p for p in _TAG_PATHS if p not in paths)
    for path in paths:
        raw = _rel_data(data, path, j)
        if isinstance(raw, dict):
            raw = list(raw.keys())
        if not isinstance(raw, list):
            continue
        out: List[str] = []
        for item in raw:
            if isinstance(item, dict):
                item = item.get("name") or item.get("title") or item.get("tag_name")
            text = str(item or "").strip().lstrip("#")
            if text and text not in out:
                out.append(text)
        if out:
            return out[:12]
    return []


def _make_image_node(url: str) -> Optional[Image]:
    if not url:
        return None
    u = str(url).strip()
    try:
        maker = getattr(Image, "fromURL", None)
        if callable(maker):
            return maker(u)  # type: ignore[return-value]
    except Exception:
        pass
    try:
        return Image(file=u)
    except Exception:
        return None


def _make_video_node(url: str) -> Optional[Video]:
    if not url:
        return None
    u = str(url).strip()
    try:
        maker = getattr(Video, "fromURL", None)
        if callable(maker):
            return maker(u)
    except Exception:
        pass
    try:
        return Video(file=u)
    except Exception:
        return None


def _chain_to_forward_nodes(
    chain: List[Any],
    sender_name: str,
    sender_id: Any,
) -> List[Node]:
    if not chain:
        return []
    flat: List[Node] = []
    for n in chain:
        if n is None:
            continue
        # 主页作品列表本身已经构造了 Node；再次包一层会形成 Node -> Node
        # 的非法嵌套，部分平台会因此拒绝合并转发。
        if isinstance(n, Node):
            flat.append(n)
        else:
            flat.append(Node(name=sender_name, uin=sender_id, content=[n]))
    return flat


class AlistUploader:
    """Alist 文件上传封装"""

    def __init__(self, config: Dict[str, Any]):
        alist_cfg = config.get("alist", {}) or {}
        self._enable = alist_cfg.get("enable", False)
        self._url = (alist_cfg.get("url") or "").strip().rstrip("/")
        self._username = (alist_cfg.get("username") or "").strip()
        self._password = (alist_cfg.get("password") or "").strip()
        self._upload_path = (alist_cfg.get("upload_path") or "/").strip()
        self._chunk_size = (alist_cfg.get("chunk_size_mb", 10) or 10) * 1024 * 1024
        self._timeout = (alist_cfg.get("request_timeout", 60) or 60)
        self._token: Optional[str] = None

    @property
    def is_enabled(self) -> bool:
        return self._enable and bool(self._url and self._username and self._password)

    async def _login(self) -> bool:
        """登录获取 token"""
        if not self._url:
            return False
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self._timeout)) as session:
                async with session.post(
                    f"{self._url}/api/auth/login",
                    json={"username": self._username, "password": self._password}
                ) as resp:
                    if resp.status != 200:
                        return False
                    data = await resp.json()
                    if data.get("code") == 200:
                        self._token = data.get("data", {}).get("token")
                        return bool(self._token)
        except Exception as e:
            logger.warning("Alist 登录失败: %s", str(e))
        return False

    async def _get_headers(self) -> Optional[Dict[str, str]]:
        """获取认证头"""
        if not self._token:
            if not await self._login():
                return None
        return {"Authorization": self._token}

    async def upload_file(self, file_path: str, file_name: Optional[str] = None, share_url: Optional[str] = None) -> Optional[str]:
        """上传单个文件，返回 Alist 链接"""
        if not self.is_enabled:
            return None
        headers = await self._get_headers()
        if not headers:
            return None
        if share_url:
            md5_name = hashlib.md5(share_url.encode('utf-8')).hexdigest()
            ext = os.path.splitext(file_name or os.path.basename(file_path))[1] or '.mp4'
            file_name = md5_name + ext
        else:
            file_name = file_name or os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        upload_path = f"{self._upload_path.rstrip('/')}/{file_name}"
        encoded_path = quote(upload_path, safe="/")
        try:
            with open(file_path, "rb") as f:
                file_data = f.read()
            put_headers = {
                **headers,
                "Content-Length": str(file_size),
                "File-Path": encoded_path,
                "Content-Type": "application/octet-stream",
            }
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self._timeout)) as session:
                async with session.put(
                    f"{self._url}/api/fs/put",
                    headers=put_headers,
                    data=file_data
                ) as resp:
                    text = await resp.text()
                    if resp.status == 200:
                        result = await resp.json()
                        if result.get("code") == 200:
                            return f"{self._url.rstrip('/')}/d{upload_path}"
                        logger.warning("Alist 上传失败: code=%d, msg=%s", result.get("code"), result.get("message"))
                    else:
                        logger.warning("Alist 上传失败: HTTP %d, body=%s", resp.status, text[:200])
        except Exception as e:
            logger.warning("Alist 上传异常: %s", str(e))
        return None


def _ip_is_public(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


async def _validate_public_http_url(url: str) -> str:
    """拒绝非 HTTP(S)、凭据 URL、非常用端口及解析到内网的地址。"""
    value = str(url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("仅允许 http/https URL")
    if not parsed.hostname:
        raise ValueError("URL 缺少主机名")
    if parsed.username or parsed.password:
        raise ValueError("URL 不允许包含用户名或密码")
    try:
        port = parsed.port
    except ValueError as ex:
        raise ValueError("URL 端口无效") from ex
    if port not in (None, 80, 443):
        raise ValueError(f"不允许访问端口 {port}")

    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("不允许访问本机地址")
    if _ip_is_public(host):
        return value
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("不允许访问非公网 IP")

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.run_in_executor(
            None, lambda: socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
        )
    except socket.gaierror as ex:
        raise ValueError("域名解析失败") from ex
    addresses = {str(info[4][0]).split("%", 1)[0] for info in infos if info[4]}
    if not addresses or any(not _ip_is_public(address) for address in addresses):
        raise ValueError("域名解析到非公网地址")
    return value


def _public_url_trace_config() -> aiohttp.TraceConfig:
    trace = aiohttp.TraceConfig()

    async def _check_redirect(session, trace_config_ctx, params):
        location = params.response.headers.get("Location")
        if location:
            await _validate_public_http_url(urljoin(str(params.url), location))

    trace.on_request_redirect.append(_check_redirect)
    return trace


async def _download_video(url: str, timeout_sec: int = 300, max_size_mb: int = 500) -> Optional[str]:
    """下载视频文件到临时目录，返回文件路径"""
    try:
        safe_url = await _validate_public_http_url(url)
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout_sec),
            trace_configs=[_public_url_trace_config()],
        ) as session:
            async with session.get(
                safe_url,
                headers={"User-Agent": "Mozilla/5.0"},
                max_redirects=5,
            ) as resp:
                if resp.status != 200:
                    logger.warning("下载视频失败: HTTP %d", resp.status)
                    return None
                content_length = resp.headers.get('Content-Length')
                if content_length and int(content_length) > max_size_mb * 1024 * 1024:
                    logger.warning("视频文件太大: %.2f MB > %d MB限制", int(content_length) / 1024 / 1024, max_size_mb)
                    return None
                fd, temp_path = tempfile.mkstemp(suffix=".mp4", prefix="bktools_video_")
                downloaded = 0
                try:
                    with os.fdopen(fd, 'wb') as f:
                        async for chunk in resp.content.iter_chunked(8192):
                            downloaded += len(chunk)
                            if downloaded > max_size_mb * 1024 * 1024:
                                logger.warning("视频超出大小限制: %.2f MB > %d MB", downloaded / 1024 / 1024, max_size_mb)
                                os.unlink(temp_path)
                                return None
                            f.write(chunk)
                    logger.info("视频下载成功: %s (%.2f MB)", temp_path, downloaded / 1024 / 1024)
                    return temp_path
                except asyncio.CancelledError:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
                    raise
                except Exception as e:
                    logger.warning("写入视频临时文件失败: %s", str(e))
                    try:
                        os.unlink(temp_path)
                    except Exception:
                        pass
                    return None
    except Exception as e:
        logger.warning("下载视频失败: %s", str(e))
        return None


async def _get_url_content_length(url: str, timeout_sec: int = 15) -> Optional[int]:
    """通过 HEAD 请求获取远程文件大小（字节），失败返回 None"""
    try:
        safe_url = await _validate_public_http_url(url)
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout_sec),
            headers={"User-Agent": "Mozilla/5.0"},
            trace_configs=[_public_url_trace_config()],
        ) as session:
            async with session.head(
                safe_url, allow_redirects=True, max_redirects=5
            ) as resp:
                if resp.status in (200, 206):
                    cl = resp.headers.get("Content-Length")
                    if cl and cl.isdigit():
                        return int(cl)
    except Exception as e:
        logger.debug("获取文件大小失败: %s, url=%s", e, url)
    return None


# 客户端引用/转发长文本时会在 URL 中间截断并追加省略号（QQ 引用约在第 79 字符处截断），
# 例如 "https://v.douyin.com/WM5..."。尾部连续省略号是"链接不完整"的强信号。
_TRUNCATED_URL_TAIL_RE = re.compile(r"(?:\.{2,}|…+)$")


def _extract_url_candidates(text: str) -> List[Tuple[str, bool]]:
    """从文本提取 (链接, 是否疑似被截断)。

    处理中文/英文结尾标点，避免复制分享文案时把标点一起带进链接；
    尾部原本是连续省略号的链接会被标记为"疑似截断"（残链，解析必然失败）。
    """
    tail_punc = ".,!?;:)]}，。！？；：）】》」』、…"
    out: List[Tuple[str, bool]] = []
    for match in re.finditer(r"https?://[^\s\]>\"']+", text or ""):
        raw = match.group(0)
        url = raw.rstrip(tail_punc)
        if not url:
            continue
        out.append((url, bool(_TRUNCATED_URL_TAIL_RE.search(raw))))
    return out


def _extract_urls(text: str) -> List[str]:
    return [url for url, _ in _extract_url_candidates(text)]


def _quoted_message_texts(event: AstrMessageEvent) -> List[str]:
    """取出引用/回复消息段里被引用消息的完整文本。

    引用消息时 `event.message_str` 只含用户新输入的内容，可能为空；
    部分客户端还会把引用内容截断成 "https://v.douyin.com/WM5..." 这类残链。
    被引用消息的完整原文保存在 Reply 组件上（适配器通过 get_msg 拉取全量文本）。

    通过特征属性（sender_id / sender_nickname）识别引用组件，
    避免直接 import Reply 造成对 AstrBot 版本的硬依赖。
    """
    texts: List[str] = []
    try:
        chain = getattr(getattr(event, "message_obj", None), "message", None) or []
    except Exception:
        chain = []
    for comp in chain:
        if not (hasattr(comp, "sender_id") or hasattr(comp, "sender_nickname")):
            continue
        for attr in ("message_str", "text"):
            value = getattr(comp, attr, None)
            if value:
                texts.append(str(value))
                break
        for sub in getattr(comp, "chain", None) or []:
            value = getattr(sub, "text", None)
            if isinstance(value, str) and value:
                texts.append(value)
    seen: set = set()
    ordered: List[str] = []
    for item in texts:
        item = str(item).strip()
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _select_parse_urls(text: str, quoted_texts: Iterable[str]) -> Tuple[List[str], bool]:
    """选出用于解析的链接列表，并返回是否拿到"完整可用"的链接。

    优先级：本消息完整链接 > 引用消息完整链接 > 疑似截断链接（仅在没有完整链接时保留，
    供上层给出"链接不完整"提示）。返回值第二项为 False 时，列表里的链接都不可信。
    """
    good: List[str] = []
    bad: List[str] = []
    seen: set = set()

    def _add(candidates: List[Tuple[str, bool]]) -> None:
        for url, truncated in candidates:
            if url in seen:
                continue
            seen.add(url)
            (bad if truncated else good).append(url)

    _add(_extract_url_candidates(text))
    for quoted in quoted_texts:
        _add(_extract_url_candidates(quoted))
    if good:
        return good, True
    return bad, False


async def _download_audio(url: str, timeout_sec: int = 60, max_size_mb: int = 10) -> Optional[str]:
    """下载音频文件到临时目录，返回文件路径"""
    try:
        safe_url = await _validate_public_http_url(url)
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout_sec),
            trace_configs=[_public_url_trace_config()],
        ) as session:
            async with session.get(safe_url, max_redirects=5) as resp:
                if resp.status != 200:
                    logger.warning("下载音频失败: HTTP %d", resp.status)
                    return None

                content_length = resp.headers.get('Content-Length')
                if content_length and int(content_length) > max_size_mb * 1024 * 1024:
                    logger.warning("音频文件太大: %.2f MB > %d MB限制", int(content_length) / 1024 / 1024, max_size_mb)
                    return None

                fd, temp_path = tempfile.mkstemp(suffix=".mp3", prefix="bktools_audio_")
                try:
                    downloaded = 0
                    with os.fdopen(fd, 'wb') as f:
                        async for chunk in resp.content.iter_chunked(8192):
                            downloaded += len(chunk)
                            if downloaded > max_size_mb * 1024 * 1024:
                                logger.warning("音频文件超出大小限制: %d MB > %d MB", downloaded / 1024 / 1024, max_size_mb)
                                f.close()
                                try:
                                    os.unlink(temp_path)
                                except Exception:
                                    pass
                                return None
                            f.write(chunk)
                    logger.info("音频下载成功: %s (%.2f MB)", temp_path, downloaded / 1024 / 1024)
                    return temp_path
                except asyncio.CancelledError:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
                    raise
                except Exception as e:
                    logger.warning("写入临时文件失败: %s, 路径: %s", str(e), temp_path)
                    try:
                        os.unlink(temp_path)
                    except Exception:
                        pass
                    return None
    except Exception as e:
        logger.warning("下载音频失败: %s", str(e))
        return None


def _cleanup_temp_files() -> Tuple[int, int]:
    cache_dir = tempfile.gettempdir()
    prefixes = ("bktools_audio_", "bktools_video_", "bktools_voice_")
    count = 0
    size = 0
    for name in os.listdir(cache_dir):
        if not name.startswith(prefixes):
            continue
        path = os.path.join(cache_dir, name)
        try:
            size += os.path.getsize(path)
            os.unlink(path)
            count += 1
        except OSError:
            pass
    return count, size


def _convert_to_wav(input_path: str, max_duration_sec: int = 60) -> Optional[str]:
    """将音频文件转换为 wav 格式，并限制时长"""
    try:
        from pydub import AudioSegment
        
        # 加载音频文件
        audio = AudioSegment.from_file(input_path)
        
        # 限制时长
        if len(audio) > max_duration_sec * 1000:
            audio = audio[:max_duration_sec * 1000]
            logger.info("音频已截断至 %d 秒", max_duration_sec)
        
        # 转换为 wav 格式
        fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="bktools_voice_")
        os.close(fd)
        
        audio.export(wav_path, format="wav")
        logger.info("音频转换成功: %s", wav_path)
        
        # 删除原始文件
        if os.path.exists(input_path):
            os.unlink(input_path)
        
        return wav_path
    except ImportError:
        logger.warning("pydub 未安装，无法转换音频格式")
        return None
    except Exception as e:
        logger.error("音频转换失败: %s", e)
        return None


def _url_host(url: str) -> str:
    try:
        return (urlparse(str(url or "")).hostname or "").rstrip(".").lower()
    except ValueError:
        return ""


def _host_matches(host: str, domain: str) -> bool:
    normalized = str(domain or "").rstrip(".").lower()
    return bool(host and normalized and (host == normalized or host.endswith(f".{normalized}")))


def _is_qishui_url(url: str) -> bool:
    host = _url_host(url)
    return any(
        _host_matches(host, domain)
        for domain in ("qishui.douyin.com", "music.douyin.com", "qishui.com")
    )


# 视频自动解析域名集合（O(1) 查询）
_VIDEO_DOMAINS = frozenset((
    "douyin.com", "iesdouyin.com", "kuaishou.com", "xiaohongshu.com",
    "xhslink.com", "bilibili.com", "b23.tv", "weibo.com", "weibo.cn",
    "jimengai.com", "klingai.com", "jianying.com", "qianwen.com",
    "doubao.com", "weixin.qq.com",
))


def _is_doubao_video_url(url: str) -> bool:
    """检测是否为豆包视频链接"""
    parsed = urlparse(str(url or ""))
    return _host_matches((parsed.hostname or "").lower(), "doubao.com") and "/video-sharing" in parsed.path.lower()


def _is_doubao_image_url(url: str) -> bool:
    """检测是否为豆包对话图片链接"""
    parsed = urlparse(str(url or ""))
    return _host_matches((parsed.hostname or "").lower(), "doubao.com") and "/thread/" in parsed.path.lower()


def _is_wechat_video_url(url: str) -> bool:
    """检测是否为微信视频号链接"""
    parsed = urlparse(str(url or ""))
    return _host_matches((parsed.hostname or "").lower(), "weixin.qq.com") and "/sph/" in parsed.path.lower()


def _video_auto_match(url: str) -> bool:
    if _is_qishui_url(url):
        return False
    if _is_doubao_video_url(url) or _is_doubao_image_url(url):
        return True
    if _is_wechat_video_url(url):
        return True
    host = _url_host(url)
    return any(_host_matches(host, domain) for domain in _VIDEO_DOMAINS)


def _music_platform(url: str) -> Optional[str]:
    host = _url_host(url)
    if _host_matches(host, "music.163.com") or _host_matches(host, "163cn.tv"):
        return "netease"
    if _host_matches(host, "y.qq.com") or (
        _host_matches(host, "qq.com") and "/n/ryqq" in urlparse(str(url or "")).path.lower()
    ):
        return "qq"
    if _is_qishui_url(url):
        return "qishui"
    if _host_matches(host, "kuwo.cn"):
        return "kuwo"
    return None


def _supported_share_url(url: str) -> bool:
    return bool(
        _video_auto_match(url)
        or _music_platform(url)
        or _is_douyin_profile_url(url)
    )


def _extract_netease_song_id(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    m = re.search(r"(?:id|ids)=([0-9]+)", s, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"\b([0-9]{5,})\b", s)
    return m.group(1) if m else ""


def _is_douyin_profile_url(url: str) -> bool:
    """检测是否为抖音用户主页链接"""
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower()
    path_lower = parsed.path.lower()
    if _host_matches(host, "v.douyin.com"):
        # v.douyin.com/xxxx 是不透明短链，既可能指向作品也可能指向主页。
        # 在重定向展开前不能把它默认认作主页，否则普通短视频会误入主页解析。
        return "/user/" in path_lower or "/profile/" in path_lower
    return _host_matches(host, "douyin.com") and "/user/" in path_lower


def _profile_items_from_response(
    payload: Dict[str, Any], configured_path: str = ""
) -> List[Dict[str, Any]]:
    """兼容常见主页接口列表结构，并优先尊重显式字段配置。"""
    candidates: List[str] = []
    configured = str(configured_path or "").strip()
    if configured:
        candidates.append(configured)
    candidates.extend(
        (
            "data",
            "data.items",
            "data.works",
            "data.aweme_list",
            "data.videos",
            "data.list",
            "items",
            "works",
            "aweme_list",
            "videos",
            "list",
        )
    )
    seen = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        value = get_path(payload, path)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


# 活动门禁状态 → /bk活动 列表里的中文标签（键与 GateValue 取值一一对应）
_GATE_STATUS_TEXT: Dict[str, str] = {
    "granted": "🟢 进行中，可领奖",
    "already_claimed": "⚪ 已领取过",
    "campaign_not_started": "⏳ 未开始",
    "campaign_expired": "⏹ 已结束",
    "campaign_disabled": "⏹ 已关闭",
    "cooldown": "⏳ 冷却中",
}


@register(
    "astrbot_plugin_bktools",
    "jiuhunwl",
    "BugPk 工具：支持群级配置的短视频解析、网易云搜歌与多平台音乐解析",
    PLUGIN_VERSION
)
class BKToolsPlugin(Star):
    def __init__(self, context: Context, config: Optional[Dict[str, Any]] = None):
        super().__init__(context)
        self.config: Dict[str, Any] = (
            _plain_config_value(config) if isinstance(config, Mapping) else {}
        )
        self._netease_pick_cache: Dict[str, Dict[str, Any]] = {}
        self._dedup_cache: Dict[str, float] = {}
        self._alist_uploaders: Dict[str, AlistUploader] = {}
        self._runtime_config_var: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
            "bktools_runtime_config", default=None
        )
        self._runtime_scope_var: ContextVar[str] = ContextVar(
            "bktools_runtime_scope", default="global"
        )
        self._runtime_features_var: ContextVar[Optional[Dict[str, bool]]] = ContextVar(
            "bktools_runtime_features", default=None
        )
        self._active_parse_tasks: Dict[str, set[asyncio.Task]] = {}
        self._scope_semaphores: Dict[Tuple[str, int], asyncio.Semaphore] = {}
        self._last_cache_prune: float = 0.0
        self._started_at: float = time.time()
        self._runtime_manager = RuntimeManager()
        self._http_client = SafeHttpClient(
            self._runtime_manager, _public_url_trace_config
        )
        self._update_lock = asyncio.Lock()
        # 停止代次按群/私聊会话隔离，避免 A 群的停止命令中断 B 群任务。
        self._parse_cancel_generation: Dict[str, int] = {}
        self._parsing_lock: Dict[str, bool] = {}  # 解析锁，防止同一链接重复解析
        # 活动发放（v1.7.0）：编排器惰性构建，配置变更后按指纹重建
        self._campaign_mgr: Optional[CampaignManager] = None
        self._campaign_fingerprint_saved: Optional[Tuple[Any, ...]] = None
        self._campaign_lock = asyncio.Lock()
        # 当次领取内有效的聊天联系方式（只用于问卷请求体，不落日志/状态文件）
        self._claim_contact: Dict[str, Dict[str, str]] = {}
        logger.info(
            "BKtools v%s 已加载，运行文件：%s",
            PLUGIN_VERSION,
            os.path.abspath(__file__),
        )

    def _runtime_config(self) -> Dict[str, Any]:
        return self._runtime_config_var.get() or self.config

    def _runtime_scope(self) -> str:
        return self._runtime_scope_var.get()

    @staticmethod
    def _event_platform(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_platform_name() or "unknown").strip().lower()
        except Exception:
            return "unknown"

    @staticmethod
    def _event_group_id(event: AstrMessageEvent) -> str:
        try:
            getter = getattr(event, "get_group_id", None)
            if callable(getter):
                value = getter()
                if value:
                    return str(value).strip()
        except Exception:
            pass
        for attr in ("group_id", "room_id", "channel_id"):
            value = getattr(event, attr, None)
            if value:
                return str(value).strip()
        message_obj = getattr(event, "message_obj", None)
        value = getattr(message_obj, "group_id", None) if message_obj else None
        return str(value).strip() if value else ""

    @staticmethod
    def _event_sender_id(event: AstrMessageEvent) -> str:
        try:
            getter = getattr(event, "get_sender_id", None)
            if callable(getter):
                value = getter()
                if value:
                    return str(value).strip()
        except Exception:
            pass
        for attr in ("user_id", "sender_id"):
            value = getattr(event, attr, None)
            if value:
                return str(value).strip()
        return "unknown"

    @staticmethod
    def _event_nickname(event: AstrMessageEvent) -> str:
        """尽力取发送者昵称（活动问卷 contact_source=chat_member 用）；取不到返回空。"""
        for attr in ("sender_nickname", "nickname", "group_nickname", "sender_nick"):
            value = getattr(event, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        try:
            message_obj = getattr(event, "message_obj", None)
            if message_obj is not None:
                for attr in ("sender_nickname", "nickname", "group_nickname"):
                    value = getattr(message_obj, attr, None)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        except Exception:
            pass
        return ""

    def _scope_for_event(self, event: AstrMessageEvent) -> str:
        platform = self._event_platform(event)
        group_id = self._event_group_id(event)
        if group_id:
            return f"{platform}:group:{group_id}"
        return f"{platform}:private:{self._event_sender_id(event)}"

    def _find_group_override(
        self, group_id: str, platform: str, overrides: Any
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(overrides, list):
            return None
        wildcard: Optional[Dict[str, Any]] = None
        for item in overrides:
            if not isinstance(item, dict):
                continue
            if str(item.get("group_id") or "").strip() != group_id:
                continue
            configured_platform = str(item.get("platform") or "").strip().lower()
            if configured_platform == platform:
                return item
            if not configured_platform and wildcard is None:
                wildcard = item
        return wildcard

    def _resolve_event_policy(self, event: AstrMessageEvent) -> Dict[str, Any]:
        group_cfg = _cfg(self.config, "group_control", default={}) or {}
        platform = self._event_platform(event)
        group_id = self._event_group_id(event)
        scope = self._scope_for_event(event)
        features = {
            "short_video": True,
            "douyin_profile": True,
            "netease": True,
            "music": True,
        }

        if not group_id:
            return {
                "enabled": bool(group_cfg.get("private_chat_enabled", True)),
                "config": _plain_config_value(self.config),
                "features": features,
                "scope": scope,
            }

        override = self._find_group_override(
            group_id, platform, group_cfg.get("group_overrides", [])
        )
        mode = str(group_cfg.get("mode") or "all").strip().lower()
        if mode == "selected":
            enabled = bool(override and override.get("enabled", True))
        else:
            enabled = bool(override.get("enabled", True)) if override else True

        effective = _plain_config_value(self.config)
        if override:
            feature_cfg = override.get("features", {}) or {}
            if isinstance(feature_cfg, dict):
                for key in features:
                    if key in feature_cfg:
                        features[key] = bool(feature_cfg.get(key))

            raw_override = override.get("override_json", "{}")
            parsed_override: Dict[str, Any] = {}
            if isinstance(raw_override, dict):
                parsed_override = raw_override
            elif isinstance(raw_override, str) and raw_override.strip():
                try:
                    loaded = json.loads(raw_override)
                    if isinstance(loaded, dict):
                        parsed_override = loaded
                    else:
                        logger.warning("群 %s 的高级覆盖必须是 JSON 对象", group_id)
                except json.JSONDecodeError as ex:
                    logger.warning("群 %s 的高级覆盖 JSON 无效: %s", group_id, ex)
            # 群覆盖不能反向修改群控制规则本身。
            parsed_override = dict(parsed_override)
            for protected_section in ("group_control", "security", "runtime_limits", "updates"):
                parsed_override.pop(protected_section, None)
            if parsed_override:
                effective = _deep_merge_dict(effective, parsed_override)

            behavior = override.get("behavior", {}) or {}
            if isinstance(behavior, dict):
                mappings = {
                    "auto_short_video": ("trigger", "auto_short_video"),
                    "auto_douyin_profile": ("trigger", "auto_douyin_profile"),
                    "auto_music_link": ("trigger", "auto_music_link"),
                    "pack_forward": ("message", "pack_forward"),
                    "opening_enable": ("message", "opening_enable"),
                    "send_voice_message": ("message", "send_voice_message"),
                    "force_link_enabled": ("video_threshold", "force_link_enabled"),
                    "direct_json_enabled": ("batch_output", "direct_json_enabled"),
                }
                for key, path in mappings.items():
                    _apply_tri_state(effective, path, behavior.get(key))

        return {
            "enabled": enabled,
            "config": effective,
            "features": features,
            "scope": scope,
        }

    @contextmanager
    def _event_runtime(self, event: AstrMessageEvent):
        policy = self._resolve_event_policy(event)
        if not policy.get("enabled"):
            yield None
            return
        config_token = self._runtime_config_var.set(policy["config"])
        scope_token = self._runtime_scope_var.set(str(policy["scope"]))
        feature_token = self._runtime_features_var.set(policy["features"])
        try:
            yield policy
        finally:
            self._runtime_features_var.reset(feature_token)
            self._runtime_scope_var.reset(scope_token)
            self._runtime_config_var.reset(config_token)

    def _feature_enabled(self, feature: str) -> bool:
        features = self._runtime_features_var.get() or {}
        return bool(features.get(feature, True))

    def _security_cfg(self) -> Dict[str, Any]:
        return _cfg(self._runtime_config(), "security", default={}) or {}

    @staticmethod
    def _normalize_admin_ids(raw: Any) -> set:
        """归一化授权用户 ID：兼容 list 与逗号/空格/分号分隔的字符串。"""
        items: List[str] = []
        if isinstance(raw, str):
            items = re.split(r"[\s,，;；]+", raw)
        elif isinstance(raw, (list, tuple, set)):
            for entry in raw:
                if isinstance(entry, str):
                    items.extend(re.split(r"[\s,，;；]+", entry))
                elif entry is not None:
                    items.append(str(entry))
        return {str(x).strip() for x in items if str(x).strip()}

    async def _is_management_allowed(self, event: AstrMessageEvent) -> bool:
        cfg = self._security_cfg()
        mode = str(cfg.get("management_permission") or "admin_only").lower()
        if mode == "everyone":
            return True
        sender_id = self._event_sender_id(event)
        configured_admins = self._normalize_admin_ids(cfg.get("admin_user_ids"))
        if sender_id in configured_admins:
            return True
        # AstrBot 全局管理员：命中 admins_id 时框架会把 event.role 置为 admin。
        # QQ 官方机器人等平台取不到真实 QQ 号（只有 openid），这条兜底可免去手工填 ID。
        if mode != "allowlist_only":
            try:
                if event.is_admin():
                    return True
            except Exception:
                pass
        if mode == "allowlist_only":
            return False
        if not self._event_group_id(event) or not bool(cfg.get("allow_group_admins", True)):
            return False
        try:
            group = await event.get_group()
        except Exception as ex:
            logger.warning("获取群管理员信息失败: %s", ex)
            return False
        if not group:
            return False
        owner = str(getattr(group, "group_owner", "") or "")
        admins = {str(x) for x in (getattr(group, "group_admins", None) or [])}
        return sender_id == owner or sender_id in admins

    def _scope_concurrency_limit(self) -> int:
        cfg = self._security_cfg()
        try:
            return max(1, min(int(cfg.get("max_concurrent_tasks_per_scope", 2)), 10))
        except (TypeError, ValueError):
            return 2

    @asynccontextmanager
    async def _track_parse_task(self):
        scope = self._runtime_scope()
        task = asyncio.current_task()
        if task is None:
            yield
            return
        tasks = self._active_parse_tasks.setdefault(scope, set())
        tasks.add(task)
        self._runtime_manager.register_task(scope)
        limit = self._scope_concurrency_limit()
        semaphore = self._scope_semaphores.setdefault(
            (scope, limit), asyncio.Semaphore(limit)
        )
        try:
            async with semaphore:
                yield
        finally:
            record = self._runtime_manager.tasks.get(task)
            if record and record.state not in {TaskState.CANCELLED, TaskState.FAILED}:
                self._runtime_manager.finish_task(task, TaskState.COMPLETED)
            tasks.discard(task)
            if not tasks:
                self._active_parse_tasks.pop(scope, None)
            self._runtime_manager.remove_task(task)

    def _cancel_scope_tasks(self) -> int:
        current = asyncio.current_task()
        tasks = list(self._active_parse_tasks.get(self._runtime_scope(), set()))
        count = 0
        for task in tasks:
            if task is current or task.done():
                continue
            task.cancel()
            count += 1
        return count

    def _prune_runtime_caches(self, *, force: bool = False) -> None:
        now = time.time()
        cfg = self._security_cfg()
        try:
            interval = max(10, int(cfg.get("cache_prune_interval_sec", 60)))
        except (TypeError, ValueError):
            interval = 60
        if not force and now - self._last_cache_prune < interval:
            return
        self._last_cache_prune = now
        try:
            ttl = max(30, int(cfg.get("runtime_cache_ttl_sec", 600)))
        except (TypeError, ValueError):
            ttl = 600
        self._dedup_cache = {
            key: ts for key, ts in self._dedup_cache.items() if now - ts <= ttl
        }
        self._netease_pick_cache = {
            key: value
            for key, value in self._netease_pick_cache.items()
            if now - float(value.get("ts", 0)) <= ttl
        }
        for key in [key for key, locked in self._parsing_lock.items() if not locked]:
            self._parsing_lock.pop(key, None)
        limits = self._runtime_limits_cfg()
        try:
            max_entries = max(32, min(int(limits.get("runtime_cache_max_entries", 1000)), 10000))
        except (TypeError, ValueError):
            max_entries = 1000
        if len(self._dedup_cache) > max_entries:
            newest = sorted(self._dedup_cache.items(), key=lambda item: item[1], reverse=True)
            self._dedup_cache = dict(newest[:max_entries])
        if len(self._netease_pick_cache) > max_entries:
            newest = sorted(
                self._netease_pick_cache.items(),
                key=lambda item: float(item[1].get("ts", 0)),
                reverse=True,
            )
            self._netease_pick_cache = dict(newest[:max_entries])
        self._runtime_manager.prune(ttl, max_entries)
        if len(self._alist_uploaders) > 16:
            self._alist_uploaders.clear()

    async def _send_disabled_notice(self, event: AstrMessageEvent) -> None:
        group_cfg = _cfg(self.config, "group_control", default={}) or {}
        text = str(
            group_cfg.get("disabled_message")
            or "当前群未启用 BKtools，请联系管理员调整插件群聊设置。"
        ).strip()
        if text:
            await event.send(event.plain_result(text))

    async def _send_feature_disabled_notice(
        self, event: AstrMessageEvent, feature: str
    ) -> None:
        names = {
            "short_video": "短视频解析",
            "douyin_profile": "抖音主页解析",
            "netease": "网易云搜歌",
            "music": "音乐链接解析",
        }
        display_name = names.get(feature, feature)
        await event.send(
            event.plain_result(f"当前群已关闭{display_name}功能。")
        )

    def _get_alist(self) -> Optional[AlistUploader]:
        config = self._runtime_config()
        alist_cfg = _cfg(config, "alist", default={}) or {}
        if not bool(alist_cfg.get("enable", False)):
            return None
        cache_key = json.dumps(alist_cfg, ensure_ascii=False, sort_keys=True, default=str)
        uploader = self._alist_uploaders.get(cache_key)
        if uploader is None:
            uploader = AlistUploader(config)
            self._alist_uploaders[cache_key] = uploader
        return uploader if uploader.is_enabled else None

    def _check_dedup(self, url: str) -> bool:
        """检查链接是否在去重窗口期内返回 True 表示需要跳过（已解析过）"""
        self._prune_runtime_caches()
        tr = _cfg(self._runtime_config(), "trigger", default={}) or {}
        if not tr.get("dedup_enable", True):
            return False
        window = tr.get("dedup_window_sec", 30)
        now = time.time()
        cache_key = f"{self._runtime_scope()}:{url}"
        last_time = self._dedup_cache.get(cache_key)
        if last_time and (now - last_time) < window:
            return True
        self._dedup_cache[cache_key] = now
        return False

    def _debug_cfg(self) -> Tuple[bool, int]:
        d = _cfg(self._runtime_config(), "debug", default={}) or {}
        return bool(d.get("enable", False)), int(d.get("max_chars", 800) or 800)

    def _new_parse_token(self) -> Tuple[str, int]:
        scope = self._runtime_scope()
        return scope, self._parse_cancel_generation.get(scope, 0)

    def _is_parse_cancelled(self, token: Tuple[str, int]) -> bool:
        scope, generation = token
        return generation != self._parse_cancel_generation.get(scope, 0)

    def _cancel_active_parses(self) -> None:
        scope = self._runtime_scope()
        self._parse_cancel_generation[scope] = (
            self._parse_cancel_generation.get(scope, 0) + 1
        )

    def _runtime_limits_cfg(self) -> Dict[str, Any]:
        return _cfg(self._runtime_config(), "runtime_limits", default={}) or {}

    def _http_reliability_cfg(self) -> Dict[str, Any]:
        return _cfg(self._runtime_config(), "http_reliability", default={}) or {}

    def _open_api_http_kwargs(self) -> Dict[str, Any]:
        """把 http / http_reliability 配置映射成 OpenApiClient 的入参名。

        两个分组的键名与客户端入参名并不一致（退避单位是毫秒、熔断阈值叫法不同），
        直接 **splat 会因未知关键字抛 TypeError 并导致整个活动模块静默不可用，
        所以这里显式逐个映射、只输出客户端真正接受的键。
        """
        http_cfg = _cfg(self._runtime_config(), "http", default={}) or {}
        reliability = self._http_reliability_cfg()

        timeout_sec = int(http_cfg.get("timeout_sec", 45) or 45)
        user_agent = str(http_cfg.get("user_agent") or "").strip()[:512]

        retry_count = int(reliability.get("retry_count", 2) or 0)
        retry_backoff_ms = int(reliability.get("retry_backoff_ms", 500) or 0)
        circuit_threshold = int(reliability.get("circuit_failure_threshold", 5) or 1)
        circuit_recovery_sec = int(reliability.get("circuit_recovery_sec", 60) or 60)

        kwargs: Dict[str, Any] = {
            "timeout_sec": max(1, min(timeout_sec, 600)),
            "retry_count": max(0, min(retry_count, 10)),
            "retry_backoff_sec": max(0.0, retry_backoff_ms / 1000.0),
            "circuit_threshold": max(1, min(circuit_threshold, 100)),
            "circuit_recovery_sec": max(1, min(circuit_recovery_sec, 3600)),
        }
        # 用户未配置 UA 时交给客户端默认值，避免空串覆盖
        if user_agent:
            kwargs["user_agent"] = user_agent
        return kwargs

    def _send_key(self, label: str, payload: Dict[str, Any]) -> str:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return f"{self._runtime_scope()}:{label}:{digest}"

    def _send_reliability_cfg(self) -> Dict[str, Any]:
        return _cfg(self._runtime_config(), "send_reliability", default={}) or {}

    @staticmethod
    def _classify_send_error(ex: BaseException) -> str:
        """把 event.send 抛出的异常分类为发送策略依据。

        返回:
          - "timeout":   平台响应超时（如 aiocqhttp 的 "WebSocket API call
            timeout"）。请求可能已送达并在处理中，也可能已丢失 —— 状态不确定，
            重发相同内容可能造成重复消息。
          - "retryable": 连接层失败（适配器未连接/断连、HTTP 网络错误等），
            消息确定未送达，重试安全且有恢复可能。
          - "rejected":  平台明确拒绝或本地错误（ActionFailed、构造错误等），
            消息未送达但重试通常无意义。
        """
        if isinstance(ex, asyncio.TimeoutError):
            return "timeout"
        message = str(ex or "").lower()
        if "timeout" in message or "timed out" in message:
            return "timeout"
        name = type(ex).__name__.lower()
        if (
            "networkerror" in name
            or "apinotavailable" in name
            or "connection" in name
            or "connection" in message
            or "websocket" in message
        ):
            return "retryable"
        return "rejected"

    async def _dispatch_send(
        self,
        event: AstrMessageEvent,
        result: Any,
        label: str,
        *,
        retry_same_payload: bool,
    ) -> str:
        """调用 event.send 并返回发送结果分类。

        - "ok":      平台已确认发送成功
        - "timeout": 平台响应超时，状态不确定；不重发相同内容（防重复）
        - "failed":  发送失败（确定性失败按配置重试后仍失败，或不可重试）
        """
        policy = self._send_reliability_cfg()
        try:
            retry_max = max(0, min(int(policy.get("retry_max", 2) or 0), 5))
        except (TypeError, ValueError):
            retry_max = 2
        try:
            backoff = max(
                0.0, min(float(policy.get("retry_backoff_ms", 800) or 0) / 1000, 10.0)
            )
        except (TypeError, ValueError):
            backoff = 0.8
        attempt = 0
        while True:
            attempt += 1
            try:
                await event.send(result)
                logger.info("%s发送成功（第 %d 次尝试）", label, attempt)
                return "ok"
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                kind = self._classify_send_error(ex)
                if kind == "timeout":
                    logger.warning(
                        "%s发送超时（第 %d 次尝试），平台在适配器时限内未返回结果，"
                        "消息是否已送达状态不确定；为避免重复，不重发相同内容。"
                        "原始错误: %s",
                        label, attempt, ex,
                    )
                    return "timeout"
                if kind == "retryable" and retry_same_payload and attempt <= retry_max:
                    logger.warning(
                        "%s发送失败（连接类错误，第 %d/%d 次尝试，%s后重试）: %s",
                        label, attempt, retry_max + 1,
                        f"{backoff * 1000:.0f}ms" if backoff > 0 else "立即", ex,
                    )
                    if backoff > 0:
                        await asyncio.sleep(backoff)
                    continue
                logger.warning(
                    "%s发送失败（第 %d 次尝试后放弃，消息未送达平台）: %s",
                    label, attempt, ex,
                )
                return "failed"

    async def _send_json_result(
        self,
        event: AstrMessageEvent,
        payload: Dict[str, Any],
        parse_token: Tuple[str, int],
        reason: str,
        header_text: Optional[str] = None,
        link_url: Optional[str] = None,
    ) -> bool:
        """把接口响应作为消息发送。

        - 未提供 header_text：短 JSON 走单条纯文本，长 JSON 走一条合并转发分片。
        - 提供 header_text（批量超阈值）：固定走「可读摘要卡片 + 完整响应」的
          打包消息，保证既能看到关键信息，又保留完整接口响应。
        - 微信协议端不支持 Nodes，统一降级为逐条普通消息。
        """
        if self._is_parse_cancelled(parse_token):
            logger.info("%s前收到停止解析指令，取消发送 JSON", reason)
            return False
        header = (header_text or "").strip()
        # 完整响应可能夹带凭据字段，发到聊天前先遮蔽
        json_text = json.dumps(
            _redact_sensitive(payload),
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        encoded = json_text.encode("utf-8")
        limits = self._runtime_limits_cfg()
        text_limit = max(256, int(limits.get("json_text_max_chars", 3500) or 3500))
        configured_json_limit = limits.get(
            "json_output_max_bytes",
            limits.get("json_file_max_bytes", 2 * 1024 * 1024),
        )
        json_limit = max(text_limit, int(configured_json_limit or 0))
        try:
            caps = self._adapter_caps(event)
            if not caps["forward"]:
                # 平台不支持 Nodes 合并转发（微信系 / QQ 官方机器人等），
                # 统一改为分条普通文本发送，分片长度按适配器能力取值。
                # 官方机器人启用 Markdown 时：摘要卡片按 Markdown 排版发送
                # （加粗字段标签），JSON 原文分片强制纯文本避免被渲染破坏。
                if len(encoded) > json_limit:
                    raise ValueError("解析 JSON 超过配置的输出大小限制")
                md_on = self._official_markdown_enabled(event)
                header_md = _markdownize_card_text(header) if header and md_on else ""
                pieces: List[str] = [header_md] if header_md else (
                    [header] if header else []
                )
                if len(json_text) <= text_limit:
                    pieces.append(json_text)
                else:
                    chunk_size = max(256, min(text_limit, caps["text_chunk_chars"]))
                    pieces.extend(
                        json_text[index : index + chunk_size]
                        for index in range(0, len(json_text), chunk_size)
                    )
                button_rows: List[Any] = []
                if header_md:
                    if self._official_buttons_enabled(event):
                        button_rows = self._official_button_rows(header_md, link_url or "")
                sent_any = False
                for index, piece in enumerate(pieces):
                    if self._is_parse_cancelled(parse_token):
                        logger.info("%s分片发送过程中收到停止指令", reason)
                        break
                    is_header = bool(header_md) and index == 0
                    if is_header:
                        outcome = await self._send_official_card(
                            event, piece, button_rows, f"{reason}摘要",
                            parse_token=parse_token,
                        )
                    else:
                        outcome = await self._dispatch_send(
                            event,
                            self._text_result(event, piece, markdown=False),
                            f"{reason}（普通消息分片）",
                            retry_same_payload=True,
                        )
                    if outcome == "ok":
                        sent_any = True
                if not sent_any:
                    logger.warning("%s，平台分片均未发送成功（%s）", reason, caps["name"])
                return sent_any
            if len(json_text) <= text_limit and not header:
                result = event.plain_result(json_text)
            else:
                if len(encoded) > json_limit:
                    raise ValueError("解析 JSON 超过配置的输出大小限制")
                max_nodes = max(
                    1,
                    min(_to_int(limits.get("json_forward_max_nodes"), 100), 200),
                )
                if header:
                    max_nodes = max(1, max_nodes - 1)  # 预留摘要卡片节点
                # 以一条合并转发承载完整内容，避免本地 /tmp 文件在
                # AstrBot 与 OneBot/NapCat 分离部署时无法跨容器读取。
                chunk_size = max(text_limit, (len(json_text) + max_nodes - 1) // max_nodes)
                chunks = [
                    json_text[index : index + chunk_size]
                    for index in range(0, len(json_text), chunk_size)
                ]
                name, uid = self._bot_identity(event)
                nodes: List[Node] = []
                if header:
                    nodes.append(Node(name=name, uin=uid, content=[Plain(header)]))
                nodes.extend(
                    Node(name=name, uin=uid, content=[Plain(chunk)])
                    for chunk in chunks
                )
                result = event.chain_result([Nodes(nodes)])
        except Exception as ex:
            logger.error("%s，构造解析 JSON 消息失败: %s", reason, ex)
            try:
                await event.send(event.plain_result("解析结果发送失败，请稍后重试或联系管理员查看诊断信息。"))
            except Exception:
                logger.error("发送 JSON 失败提示也失败: %s", reason)
            return False

        self._runtime_manager.set_state(TaskState.SENDING)
        outcome = await self._dispatch_send(
            event, result, f"{reason}（JSON）", retry_same_payload=True
        )
        if outcome == "ok":
            return True
        # 超时/失败后不再重发相同 JSON（防重复），由上层决定是否释放去重标记。
        logger.warning("%s，JSON 发送未确认成功（%s），不再重发", reason, outcome)
        return False

    async def _send_batch_as_file(
        self,
        event: AstrMessageEvent,
        payload: Dict[str, Any],
        header: str,
        parse_token: Tuple[str, int],
        reason: str,
    ) -> bool:
        """把完整响应保存为 JSON 文件发送（默认批量输出方式）。

        NapCat 对过大的合并转发经常报 retcode 1200（send_forward_msg 失败），
        文件发送走 upload_file 接口，不经过转发消息通道，稳定性好得多。
        先发可读摘要卡片（普通文本），再发 JSON 文件。

        返回 False 表示文件方式未成功，调用方应降级为打包消息。
        """
        if self._is_parse_cancelled(parse_token):
            logger.info("%s前收到停止解析指令，取消发送 JSON 文件", reason)
            return False
        json_text = json.dumps(
            _redact_sensitive(payload),
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        encoded = json_text.encode("utf-8")
        limits = self._runtime_limits_cfg()
        json_limit = max(
            65536,
            int(
                limits.get(
                    "json_output_max_bytes",
                    limits.get("json_file_max_bytes", 2 * 1024 * 1024),
                )
                or 0,
            ),
        )
        if len(encoded) > json_limit:
            logger.warning(
                "%s，JSON 大小 %d 字节超过输出限制 %d 字节，改走打包消息",
                reason, len(encoded), json_limit,
            )
            return False

        if header:
            self._runtime_manager.set_state(TaskState.SENDING)
            outcome = await self._dispatch_send(
                event, event.plain_result(header), f"{reason}（摘要卡片）",
                retry_same_payload=True,
            )
            if outcome != "ok":
                logger.warning("%s，摘要卡片发送未确认成功（%s），改走打包消息", reason, outcome)
                return False
            if self._is_parse_cancelled(parse_token):
                logger.info("%s摘要发送后收到停止解析指令，取消发送 JSON 文件", reason)
                return False

        stamp = time.strftime("%Y%m%d_%H%M%S")
        fname = f"BKtools_解析响应_{stamp}_{os.getpid() % 10000}.json"
        fdir = os.path.join(tempfile.gettempdir(), "bktools_batch")
        try:
            os.makedirs(fdir, exist_ok=True)
            fpath = os.path.join(fdir, fname)
            with open(fpath, "wb") as fh:
                fh.write(encoded)
        except OSError as ex:
            logger.error("%s，写出 JSON 文件失败: %s", reason, ex)
            return False

        self._runtime_manager.set_state(TaskState.SENDING)
        outcome = await self._dispatch_send(
            event,
            event.chain_result([File(name=fname, file=fpath)]),
            f"{reason}（JSON 文件）",
            retry_same_payload=True,
        )
        if outcome == "ok":
            logger.info("%s，JSON 文件发送成功: %s", reason, fpath)
            return True
        logger.warning("%s，JSON 文件发送未确认成功（%s），改走打包消息", reason, outcome)
        # 发送失败时清掉刚写出的临时文件，避免堆积
        try:
            os.remove(fpath)
        except OSError:
            pass
        return False

    async def _send_batch_full_response(
        self,
        event: AstrMessageEvent,
        payload: Dict[str, Any],
        data: Any,
        cfg_sv: Dict[str, Any],
        link: str,
        parse_token: Tuple[str, int],
        reason: str,
    ) -> bool:
        """批量内容超阈值：把接口完整响应作为文件或打包消息发送。

        默认（batch_full_response_mode=file）：可读摘要卡片 + JSON 文件。
        mode=forward：可读摘要卡片 + 完整响应分片的合并转发消息。
        """
        msg_cfg = _cfg(self._runtime_config(), "message", default={}) or {}
        header = ""
        if bool(msg_cfg.get("short_video_text_metadata", True)):
            header = self._format_short_video_text(
                cfg_sv, data, payload, include_video_line=False
            )
            if header == "（无文本信息）":
                header = ""
        if header and bool(msg_cfg.get("pack_append_original_link", True)):
            stripped = (link or "").strip()
            if stripped and stripped not in header:
                header = f"{header.rstrip()}\n原始链接：{stripped}"
        mode = str(msg_cfg.get("batch_full_response_mode", "file") or "file").strip().lower()
        caps = self._adapter_caps(event)
        if mode == "file" and caps["file"]:
            if await self._send_batch_as_file(event, payload, header, parse_token, reason):
                return True
            logger.info("%s，JSON 文件方式未成功，降级为打包消息", reason)
        elif mode == "file":
            logger.info(
                "平台 %s 不支持文件发送（batch_full_response_mode=file），按适配器能力改走打包/分片消息",
                caps["name"],
            )
        return await self._send_json_result(
            event, payload, parse_token, reason,
            header_text=header or None, link_url=link,
        )

    async def _send_forward_as_plain(
        self,
        event: AstrMessageEvent,
        forward_nodes: List[Node],
        parse_token: Tuple[str, int],
        label: str,
        link_url: Optional[str] = None,
    ) -> bool:
        """不支持合并转发的平台降级：把合并转发节点逐条作为普通消息发送。

        微信系 / QQ 官方机器人等平台不支持 Nodes，每个 Node 的 content 为
        组件列表（Plain/Image/Video 等），逐个组件独立发送；任一发送失败不
        阻断后续节点（尽力而为），期间可响应停止指令。
        官方机器人：首个文本卡片渲染为播放器卡片（内嵌封面图 + 加粗字段 +
        块引用元信息），开启 message.official_buttons 时长链接转为按钮行，
        卡片发送失败自动兜底纯文本重发。
        """
        sent_any = False
        first_card = True
        cover_url = ""
        try:
            caps_name = self._adapter_caps(event)["name"]
        except Exception:
            caps_name = "unknown"
        # 预扫描首个公网图片组件作为卡片内嵌封面（短视频封面/音乐封面/图集首图）
        for node in forward_nodes:
            content = getattr(node, "content", None)
            for comp in content if isinstance(content, list) else ():
                if isinstance(comp, Image):
                    u = str(getattr(comp, "url", "") or "").strip()
                    if u.startswith(("http://", "https://")):
                        cover_url = u
                        break
            if cover_url:
                break
        for node in forward_nodes:
            if self._is_parse_cancelled(parse_token):
                logger.info("%s降级发送过程中收到停止指令", label)
                return sent_any
            content = getattr(node, "content", None)
            components = content if isinstance(content, list) else [node]
            for comp in components:
                if self._is_parse_cancelled(parse_token):
                    logger.info("%s降级发送过程中收到停止指令", label)
                    return sent_any
                if isinstance(comp, Plain):
                    # 文本卡片：官方机器人启用 Markdown 时渲染播放器卡片；
                    # 其余平台保持与旧版一致的纯文本行为
                    text = str(getattr(comp, "text", "") or "")
                    md_on = self._official_markdown_enabled(event)
                    if not md_on:
                        send_result = self._text_result(event, text)
                        first_card = False
                    elif first_card:
                        first_card = False
                        btn_on = self._official_buttons_enabled(event)
                        rows = (
                            self._official_button_rows(text, link_url or "")
                            if btn_on
                            else []
                        )
                        card_md = _render_player_card_md(
                            text, cover_url=cover_url, buttons_enabled=bool(rows)
                        )
                        logger.info(
                            "BKtools v%s 官方机器人播放器卡片已渲染（平台=%s, 封面=%s, 按钮=%s）",
                            PLUGIN_VERSION, caps_name, bool(cover_url), bool(rows),
                        )
                        if rows:
                            outcome = await self._send_official_card(
                                event, card_md, rows, f"{label}卡片",
                                parse_token=parse_token,
                            )
                        else:
                            outcome = await self._dispatch_send(
                                event,
                                self._text_result(event, card_md, markdown=True),
                                f"{label}卡片",
                                retry_same_payload=True,
                            )
                        if outcome == "ok":
                            sent_any = True
                        else:
                            # 播放器卡片发送失败（如图片转存失败），退回纯文本兜底
                            fb = await self._dispatch_send(
                                event,
                                event.plain_result(text),
                                f"{label}卡片（纯文本兜底）",
                                retry_same_payload=True,
                            )
                            if fb == "ok":
                                sent_any = True
                        continue
                    else:
                        card_md = _markdownize_card_text(text)
                        send_result = self._text_result(event, card_md)
                else:
                    if (
                        cover_url
                        and isinstance(comp, Image)
                        and str(getattr(comp, "url", "") or "").strip() == cover_url
                    ):
                        # 封面已内嵌进卡片，不再单独发送重复图片
                        continue
                    send_result = event.chain_result([comp])
                outcome = await self._dispatch_send(
                    event,
                    send_result,
                    f"{label}节点",
                    retry_same_payload=True,
                )
                if outcome == "ok":
                    sent_any = True
        return sent_any

    async def _send_packed_or_json(
        self,
        event: AstrMessageEvent,
        forward_nodes: List[Node],
        raw_json: Dict[str, Any],
        parse_token: Tuple[str, int],
        label: str,
        link_url: Optional[str] = None,
    ) -> bool:
        """合并转发至多发送一次；超时/失败时降级发送解析 JSON 兜底，保证结果不丢失。"""
        send_key = self._send_key(label, raw_json)
        if not self._runtime_manager.claim_send(send_key):
            logger.info("跳过重复发送: %s", label)
            return False
        if not forward_nodes:
            logger.warning("%s没有可用的合并转发节点，直接发送解析 JSON", label)
            return await self._send_json_result(
                event, raw_json, parse_token, f"{label}无法构造合并转发",
                link_url=link_url,
            )

        caps = self._adapter_caps(event)
        if not caps["forward"]:
            # 平台不支持 Nodes 合并转发（微信系 / QQ 官方机器人等，会渲染异常
            # 或直接被拒绝），降级为逐条普通消息/组件发送。
            logger.info(
                "%s平台 %s 不支持合并转发，降级为逐条发送", label, caps["name"]
            )
            return await self._send_forward_as_plain(
                event, forward_nodes, parse_token, label, link_url=link_url
            )

        if self._is_parse_cancelled(parse_token):
            logger.info("%s发送前收到停止解析指令", label)
            return False
        try:
            packed_result = event.chain_result([Nodes(forward_nodes)])
        except Exception as ex:
            logger.warning("%s在调用发送接口前构造失败，改为发送解析 JSON: %s", label, ex)
            return await self._send_json_result(
                event, raw_json, parse_token, f"{label}构造失败"
            )

        self._runtime_manager.set_state(TaskState.SENDING)
        outcome = await self._dispatch_send(
            event, packed_result, label, retry_same_payload=True
        )
        if outcome == "ok":
            return True

        # —— 打包消息未确认送达 ——
        # 说明：AstrBot 的通用发送接口没有跨平台幂等消息 ID。超时（timeout）
        # 意味着平台在适配器时限（默认 180 秒）内没有返回结果，而实测中 NapCat
        # 往往仍在后台下载/上传大视频，会在数分钟后才真正把打包消息送达——
        # 即“延迟送达”而非“丢失”。因此：
        #   1) 绝不重发相同的打包消息（避免 v1.6.2 修复的“三份重复打包消息”回归）；
        #   2) 默认不追加 JSON 兜底（timeout_fallback=none），否则会与迟到的
        #      打包消息构成重复；仅在用户显式开启 timeout_fallback=json 时才
        #      降级发送，接受与迟到消息重复、换取立即拿到结果。
        policy = self._send_reliability_cfg()
        try:
            grace = max(0, min(int(policy.get("timeout_grace_sec", 5) or 0), 120))
        except (TypeError, ValueError):
            grace = 5
        fallback_mode = str(policy.get("timeout_fallback", "none") or "none").strip().lower()
        release_on_failure = bool(policy.get("release_claim_on_failure", True))

        if outcome == "timeout" and fallback_mode == "none":
            if release_on_failure:
                self._runtime_manager.release_send(send_key)
            logger.warning(
                "%s发送超时且未配置 JSON 兜底：平台可能在后台继续处理（大视频下载/"
                "上传较慢，预计数分钟后送达），并非一定丢失；为避免与迟到送达的"
                "打包消息重复，不再发送兜底，已释放去重标记。若长时间未收到，请"
                "重发相同链接再次触发解析。原始错误: %s",
                label,
                outcome,
            )
            return False

        if outcome == "timeout" and grace > 0:
            logger.info("%s等待 %d 秒缓冲，给平台追送结果的机会…", label, grace)
            await asyncio.sleep(grace)
            if self._is_parse_cancelled(parse_token):
                logger.info("%s缓冲等待期间收到停止解析指令，取消兜底发送", label)
                return False

        logger.warning(
            "%s打包消息%s，降级发送解析 JSON 兜底",
            label,
            "超时未确认" if outcome == "timeout" else "发送失败",
        )
        ok = await self._send_json_result(
            event, raw_json, parse_token,
            f"{label}打包消息{'超时' if outcome == 'timeout' else '发送失败'}后降级",
            link_url=link_url,
        )
        if not ok and release_on_failure:
            # 兜底也没发出去：释放去重标记，允许用户稍后手动重发相同链接
            self._runtime_manager.release_send(send_key)
            logger.info("%s兜底发送也未成功，已释放去重标记，用户可稍后重试", label)
        return ok

    def _event_scope_key(self, event: AstrMessageEvent) -> str:
        """为“搜歌后选序号”生成会话级 key，尽量按会话隔离。"""
        for attr in ("session_id", "conversation_id", "chat_id", "room_id"):
            v = getattr(event, attr, None)
            if v:
                return f"{event.get_platform_name()}:{v}"
        gid = (
            getattr(event, "group_id", None)
            or getattr(event, "room_id", None)
            or getattr(event, "channel_id", None)
            or "global"
        )
        sender = (
            getattr(event, "user_id", None)
            or getattr(event, "sender_id", None)
            or "user"
        )
        return f"{event.get_platform_name()}:{gid}:{sender}"

    def _cache_pick_candidates(
        self, event: AstrMessageEvent, keyword: str, candidates: List[Dict[str, Any]]
    ) -> None:
        self._prune_runtime_caches()
        self._netease_pick_cache[self._event_scope_key(event)] = {
            "keyword": keyword,
            "items": candidates,
            "ts": time.time(),
        }

    def _read_pick_candidates(self, event: AstrMessageEvent, ttl_sec: int = 300) -> List[Dict[str, Any]]:
        cached = self._netease_pick_cache.get(self._event_scope_key(event))
        if not isinstance(cached, dict):
            return []
        ts = float(cached.get("ts") or 0)
        if time.time() - ts > ttl_sec:
            self._netease_pick_cache.pop(self._event_scope_key(event), None)
            return []
        items = cached.get("items")
        return items if isinstance(items, list) else []

    async def _netease_pick_by_index(self, event: AstrMessageEvent, index: int) -> bool:
        items = self._read_pick_candidates(event)
        if not items:
            return False
        if index < 1 or index > len(items):
            await event.send(
                event.plain_result(f"序号超出范围，请输入 1 ~ {len(items)}。")
            )
            return True
        picked = items[index - 1]
        sid = str(picked.get("id") or "").strip()
        if not sid:
            await event.send(event.plain_result("该条结果缺少歌曲ID，请换一个序号试试。"))
            return True
        await event.send(
            event.plain_result(
                f"已选择：{picked.get('name') or '未知歌曲'}（id={sid}），正在解析…"
            )
        )
        # 复用现有网易云链解析流程：构造标准 song URL，按 netease.link_parse 配置返回音频信息
        await self._music_link_parse(event, f"https://music.163.com/song?id={sid}")
        return True

    def _http_cfg(self) -> Tuple[int, str]:
        h = _cfg(self._runtime_config(), "http", default={}) or {}
        return int(h.get("timeout_sec", 45)), str(
            h.get(
                "user_agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            )
        )

    @staticmethod
    def _is_wechat_platform(event: AstrMessageEvent) -> bool:
        """判断是否微信系平台（gewechat/wechatpadpro/webchat 等）。

        微信协议端对 Nodes 合并转发（com.tencent.multimsg「聊天记录」卡片）支持
        有限：节点昵称不生效、内容拼接为「昵称: 内容」、易被客户端限制。此类平台
        应降级为普通消息逐条发送。
        """
        try:
            name = (event.get_platform_name() or "").lower()
        except Exception:
            return False
        if not name:
            return False
        if name in ("wechatpadpro", "gewechat", "webchat", "wechat", "weixin", "wx"):
            return True
        return any(token in name for token in ("wechat", "weixin", "gewechat", "padpro"))

    @staticmethod
    def _builtin_adapter_caps(name: str) -> Optional[Dict[str, Any]]:
        """按平台名（小写）匹配内置适配器能力表；返回 None 表示未知平台。"""
        if not name:
            return None
        if name in _ADAPTER_CAPS_DEFAULT:
            return _ADAPTER_CAPS_DEFAULT[name]
        for token, key in _ADAPTER_CAPS_MATCHERS:
            if token in name:
                return _ADAPTER_CAPS_DEFAULT[key]
        return None

    def _adapter_caps(self, event: AstrMessageEvent) -> Dict[str, Any]:
        """        查询当前事件所属平台适配器的消息发送能力。

        返回字段：
        - forward: 是否支持 Nodes 合并转发（不支持则降级为普通文本分片）
        - file: 是否支持 File 组件发送文件
        - markdown: 是否支持 Markdown 富文本消息（QQ 官方机器人等）
        - text_chunk_chars: 普通文本分片的建议长度
        - name: 平台名（小写，用于日志）

        匹配顺序：用户自定义（message.custom_adapter_caps，精确匹配平台名，大小写
        不敏感）> 内置适配表（精确名 > 特征 token）> 未知平台默认沿用合并转发旧行为。
        """
        try:
            name = str(event.get_platform_name() or "").strip().lower()
        except Exception:
            name = ""
        builtin = self._builtin_adapter_caps(name)
        if builtin is not None:
            caps = dict(builtin)
        else:
            # 未知平台：保持与旧版一致的合并转发行为，出问题可由用户自定义覆盖
            caps = {"forward": True, "file": True, "markdown": False, "text_chunk_chars": 3500}
        caps["name"] = name or "unknown"

        raw = ""
        try:
            raw = str(
                (_cfg(self._runtime_config(), "message", default={}) or {}).get(
                    "custom_adapter_caps"
                )
                or ""
            ).strip()
        except Exception:
            raw = ""
        if raw:
            try:
                custom = json.loads(raw)
            except Exception as ex:
                logger.warning("message.custom_adapter_caps 不是合法 JSON，已忽略: %s", ex)
                custom = None
            if isinstance(custom, dict):
                over = custom.get(name)
                if isinstance(over, dict):
                    for key in ("forward", "file", "markdown"):
                        if key in over:
                            caps[key] = bool(over[key])
                    tc = over.get("text_chunk_chars")
                    if tc is not None:
                        caps["text_chunk_chars"] = _to_int(
                            tc, caps["text_chunk_chars"]
                        )
                    logger.info(
                        "平台 %s 使用自定义适配器能力: forward=%s, file=%s, markdown=%s, text_chunk_chars=%s",
                        caps["name"], caps["forward"], caps["file"],
                        caps.get("markdown", False), caps["text_chunk_chars"],
                    )
        caps["text_chunk_chars"] = max(
            200, min(_to_int(caps.get("text_chunk_chars"), 3500), 20000)
        )
        return caps

    def _markdown_api_available(self, event: AstrMessageEvent) -> bool:
        """当前 AstrBot 版本的 MessageChain 是否支持 use_markdown 标记。

        早于原生 Markdown 支持的 AstrBot 没有该方法，此时强行发送 Markdown
        语法只会把 `##`、`**`、`![]()` 原样显示出来，观感比纯文本更差，因此
        探测不到就直接关闭 Markdown 排版。
        """
        try:
            return hasattr(event.plain_result(""), "use_markdown")
        except Exception:
            return False

    def _official_markdown_enabled(self, event: AstrMessageEvent) -> bool:
        """官方机器人（qq_official 系）是否启用 Markdown 排版输出。

        需同时满足：适配器能力表 markdown=True（内置 qq_official 或用户自定义
        开启）且 message.official_markdown 配置未关闭（默认开启）。AstrBot 的
        qqofficial 适配器在机器人未获得原生 Markdown 权限时会把 msg_type=2
        自动回退为纯文本，因此开启该选项是安全的。
        """
        try:
            caps = self._adapter_caps(event)
            if not caps.get("markdown"):
                return False
            if not self._markdown_api_available(event):
                return False
            msg_cfg = _cfg(self._runtime_config(), "message", default={}) or {}
            return bool(msg_cfg.get("official_markdown", True))
        except Exception:
            return False

    def _official_buttons_enabled(self, event: AstrMessageEvent) -> bool:
        """是否尝试发送 keyboard 按钮行。

        默认开启；按钮组件缺失（AstrBot 版本过旧）或最近一次因未授权 keyboard
        失败时自动跳过，避免每张卡片都先失败一次再降级。
        """
        if not _BUTTONS_READY:
            return False
        try:
            block_until = float(getattr(self, "_button_block_until", 0) or 0)
        except (TypeError, ValueError):
            block_until = 0.0
        if time.time() < block_until:
            return False
        msg_cfg = _cfg(self._runtime_config(), "message", default={}) or {}
        return bool(msg_cfg.get("official_buttons", True))

    def _text_result(
        self,
        event: AstrMessageEvent,
        text: str,
        *,
        markdown: Optional[bool] = None,
    ):
        """构造纯文本结果并按平台能力补 use_markdown_ 标记。

        markdown 三态：
        - None：跟随能力表与配置（官方机器人默认启用 Markdown）；
        - True：强制 Markdown（仅官方机器人实际生效，其余平台忽略该标记）；
        - False：强制纯文本（JSON 原文分片等场景，避免被 Markdown 渲染破坏）。
        """
        res = event.plain_result(text)
        try:
            if markdown is False:
                res.use_markdown(False)
            elif markdown is True:
                res.use_markdown(True)
            elif markdown is None and self._official_markdown_enabled(event):
                res.use_markdown(True)
        except Exception:
            # 老版本 AstrBot 无 use_markdown 方法时静默忽略，保持纯文本行为
            pass
        return res

    def _official_button_rows(
        self, card_text: str = "", link_url: str = ""
    ) -> List[Any]:
        """从摘要卡片与原始链接构造 URL 按钮行（官方机器人 inline keyboard）。

        - 「原始链接」排最前；卡片里的 视频直链/原声链接/音频直链 等行
          作为附加跳转按钮（每个按钮 = 一个 URL action）；
        - QQ keyboard 单行最多 5 个按钮，每个按钮的 URL（action data）
          不得超过 1024 字节，超长或非法链接直接跳过；
        - AstrBot 版本过旧无按钮组件时返回空列表（自动停用按钮）。
        """
        if not _BUTTONS_READY or ActionRow is None or Button is None:
            return []
        pairs: List[Tuple[str, str]] = []
        seen: set = set()
        stripped_link = (link_url or "").strip()
        if stripped_link.startswith(("http://", "https://")):
            pairs.append(("原始链接", stripped_link))
            seen.add(stripped_link)
        for line in str(card_text or "").splitlines():
            m = _BUTTON_LINK_LINE_RE.match(line)
            if not m:
                continue
            label, url = m.group(1), m.group(2).strip()
            if url in seen:
                continue
            seen.add(url)
            pairs.append((label[:10], url))
        buttons: List[Any] = []
        stamp = int(time.time() * 1000) % 1000000000
        for offset, (label, url) in enumerate(pairs[:5]):
            if len(url.encode("utf-8")) > 1024:
                continue
            label = _PLAYER_BUTTON_LABELS.get(label, f"🔗 {label}")[:10]
            try:
                buttons.append(
                    Button(
                        id=f"bktools_link_{stamp}_{offset}",
                        label=label,
                        action=Button.UrlAction(url=url),
                        style=Button.Style.PRIMARY,
                    )
                )
            except Exception as ex:
                logger.debug("构造按钮 %s 失败: %s", label, ex)
        if not buttons:
            return []
        try:
            return [ActionRow(buttons=buttons)]
        except Exception as ex:
            logger.debug("构造按钮行失败: %s", ex)
            return []

    async def _send_official_card(
        self,
        event: AstrMessageEvent,
        card_md: str,
        button_rows: List[Any],
        label: str,
        parse_token: Optional[Tuple[str, int]] = None,
    ) -> str:
        """官方机器人发送 Markdown 卡片（可带按钮行），失败自动降级无按钮。

        parse_token 为 None 时（如音乐解析路径）跳过停止解析检查。
        返回 _dispatch_send 的 outcome（ok/timeout/failed/rejected）。
        """
        if button_rows:
            res = event.chain_result([Plain(card_md), *button_rows])
            try:
                res.use_markdown(True)
            except Exception:
                pass
            outcome = await self._dispatch_send(
                event, res, f"{label}（卡片+按钮）", retry_same_payload=True
            )
            if outcome == "ok" or (
                parse_token is not None and self._is_parse_cancelled(parse_token)
            ):
                return outcome
            logger.info("%s，按钮消息发送失败（多为未申请 keyboard 权限），降级为无按钮卡片", label)
            # 10 分钟内不再尝试按钮，避免每张卡片都先失败一次
            self._button_block_until = time.time() + 600
        return await self._dispatch_send(
            event,
            self._text_result(event, card_md, markdown=True),
            f"{label}（卡片）",
            retry_same_payload=True,
        )

    def _bot_identity(self, event: AstrMessageEvent) -> Tuple[str, Any]:
        name = "BKtools"
        platform = event.get_platform_name()
        sid = event.get_self_id()
        if platform not in ("wechatpadpro", "webchat", "gewechat"):
            try:
                sid = int(sid)
            except (ValueError, TypeError):
                sid = 10000
        return name, sid

    async def _maybe_opening(self, event: AstrMessageEvent) -> None:
        msg = _cfg(self._runtime_config(), "message", default={}) or {}
        if not msg.get("opening_enable"):
            return
        text = (msg.get("opening_text") or "").strip()
        if not text:
            return
        # 统一前缀，避免用户在配置里重复维护
        prefix = "BkTools："
        if not text.lower().startswith(prefix.lower()):
            text = prefix + text
        await event.send(event.plain_result(text))

    @staticmethod
    def _loads_response_json(raw: bytes, preview_encoding: str = "utf-8") -> Dict[str, Any]:
        """解析接口 JSON；去除 UTF-8 BOM（部分 PHP 会在正文前输出 ﻿）。"""
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode(preview_encoding, errors="replace")
            text = text.lstrip("\ufeff\u200b").strip()
        else:
            text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"非 JSON 响应: {text[:200]}…") from e

    @staticmethod
    def _parse_headers_config(raw: Any) -> Dict[str, str]:
        """把 headers 配置解析为请求头字典。

        兼容两种形态：dict（旧版/直接配置）或 JSON 文本（WebUI 文本编辑器，
        如 {"Authorization": "Bearer xxx"}）；非法内容返回空字典并记日志。
        """
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items() if v is not None}
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return {}
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError) as ex:
                logger.warning("自定义请求头 JSON 解析失败，已忽略: %s（%s）", text[:80], ex)
                return {}
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items() if v is not None}
            logger.warning("自定义请求头配置格式错误（应为 JSON 对象），已忽略: %s", text[:80])
        return {}

    def _merged_request_headers(self, *sections: str) -> Dict[str, str]:
        """合并自定义请求头：全局 http.headers + 各接口节 headers（后者覆盖前者）。

        各节内配置为 JSON 文本或字典，形如 {"Authorization": "Bearer xxx", ...}。
        """
        merged: Dict[str, str] = {}
        global_headers = _cfg(self._runtime_config(), "http", "headers", default={}) or {}
        merged.update(self._parse_headers_config(global_headers))
        for section in sections:
            sec_headers = _cfg(self._runtime_config(), section, "headers", default={}) or {}
            merged.update(self._parse_headers_config(sec_headers))
        return merged

    async def _session_get_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        return await self._request_json("GET", url, headers=headers)

    async def _session_post_form(
        self,
        session: aiohttp.ClientSession,
        url: str,
        data: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        return await self._request_json("POST", url, data=data, headers=headers)

    async def _request_json(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        timeout_sec, ua = self._http_cfg()
        reliability = self._http_reliability_cfg()
        limits = self._runtime_limits_cfg()
        merged_headers = self._merged_request_headers()
        if headers:
            merged_headers.update({str(k): str(v) for k, v in headers.items()})
        raw = await self._http_client.request_bytes(
            method,
            url,
            headers=merged_headers or None,
            timeout_sec=timeout_sec,
            user_agent=ua,
            max_response_bytes=max(
                64 * 1024,
                int(limits.get("max_api_response_bytes", 5 * 1024 * 1024) or 0),
            ),
            retries=max(0, min(int(reliability.get("retry_count", 2) or 0), 5)),
            backoff_sec=max(
                0.0, min(float(reliability.get("retry_backoff_ms", 500) or 0) / 1000, 10.0)
            ),
            circuit_threshold=max(
                1, min(int(reliability.get("circuit_failure_threshold", 5) or 5), 20)
            ),
            circuit_recovery_sec=max(
                5, min(int(reliability.get("circuit_recovery_sec", 60) or 60), 3600)
            ),
            **kwargs,
        )
        return self._loads_response_json(raw)

    async def _resolve_url_redirect(self, url: str, timeout_sec: int = 10) -> str:
        """仅展开受支持平台的公网 HTTP(S) 短链接。"""
        u = (url or "").strip()
        if not u or not _supported_share_url(u):
            return u
        _, ua = self._http_cfg()
        try:
            safe_url = await _validate_public_http_url(u)
            session = await self._http_client.get_session(timeout_sec, ua)
            async with session.head(
                safe_url, allow_redirects=True, max_redirects=5
            ) as resp:
                final_u = str(resp.url)
                if final_u:
                    return final_u
        except Exception as ex:
            logger.debug("安全展开短链接 HEAD 失败: %s", ex)
        try:
            safe_url = await _validate_public_http_url(u)
            session = await self._http_client.get_session(timeout_sec, ua)
            async with session.get(
                safe_url, allow_redirects=True, max_redirects=5
            ) as resp:
                final_u = str(resp.url)
                if final_u:
                    return final_u
        except Exception as ex:
            logger.debug("安全展开短链接 GET 失败: %s", ex)
        return u

    async def _fetch_short_video(self, link: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        cfg_sv = _cfg(self._runtime_config(), "short_video", default={}) or {}
        endpoint = (cfg_sv.get("endpoint") or "").strip().rstrip("?&")
        if not endpoint:
            raise ValueError("未配置短视频 endpoint")
        param = cfg_sv.get("url_param_name") or "url"
        method = (cfg_sv.get("request_method") or "GET").upper()
        sv_headers = self._merged_request_headers("short_video")
        if method == "POST":
            j = await self._request_json("POST", endpoint, headers=sv_headers, data={param: link})
        else:
            q = urlencode({param: link})
            sep = "&" if "?" in endpoint else "?"
            j = await self._request_json("GET", f"{endpoint}{sep}{q}", headers=sv_headers)
        path_root = (cfg_sv.get("path_data_root") or "data").strip() or "data"
        data = get_path(j, path_root)
        if not isinstance(data, dict):
            data = {}
        ok = _parse_success_codes(str(cfg_sv.get("success_codes", "200")))
        if not _code_ok(j, cfg_sv.get("path_code") or "code", ok):
            # 业务失败：透传接口 msg；msg 缺失时给可读兜底，不暴露原始响应码
            msg = str(get_path(j, cfg_sv.get("path_msg") or "msg") or "").strip()
            raise ValueError(msg or "接口未返回有效内容")
        return j, data

    def _format_short_video_text(
        self,
        cfg_sv: Dict[str, Any],
        data: Any,
        j: Dict[str, Any],
        *,
        include_video_line: bool = True,
        show_extended: bool = True,
    ) -> str:
        lines: List[str] = []
        title = _rel_data(data, cfg_sv.get("path_title") or "title", j) or ""
        desc = _rel_data(data, cfg_sv.get("path_desc") or "desc", j) or ""
        typ = _normalize_resource_type(_rel_data(data, cfg_sv.get("path_type") or "type", j))
        an = _rel_data(data, cfg_sv.get("path_author_name") or "author.name", j) or ""
        vurl = _first_video_url(data, cfg_sv, j)

        imgs = _rel_data(data, cfg_sv.get("path_images_list") or "images", j)
        img_count = len(imgs) if isinstance(imgs, list) else 0
        live_count = len(_live_photo_items(data, cfg_sv, j))
        live_img_count = len(_live_photo_images(data, cfg_sv, j))
        # 接口未给出 type 时按实际载荷推断，保证卡片信息完整
        if not typ:
            if live_count:
                typ = "live"
            elif img_count:
                typ = "image"
            elif vurl:
                typ = "video"

        # 视频类元信息（统一契约：图集/实况的 duration/size/quality 为空或 null）
        is_video_like = typ == "video" or bool(vurl)
        duration_s = ""
        size_label = ""
        quality_s = ""
        if show_extended and is_video_like:
            duration_s = _format_duration(
                _rel_data(data, cfg_sv.get("path_duration") or "duration", j)
            )
            size_label = _format_size_label(
                _rel_data(data, cfg_sv.get("path_size") or "size", j)
            )
            if not size_label:
                # size 缺失/为 0 时，退回接口给出的 size_label（契约允许为空串）
                provided = str(
                    _rel_data(data, cfg_sv.get("path_size_label") or "size_label", j) or ""
                ).strip()
                if provided:
                    size_label = provided
            quality_s = str(
                _rel_data(data, cfg_sv.get("path_quality") or "quality", j) or ""
            ).strip()

        tags: List[str] = _collect_tags(data, cfg_sv, j) if show_extended else []

        if title:
            lines.append(f"标题：{title}")
        if typ:
            lines.append(f"类型：{_RESOURCE_TYPE_LABELS.get(typ, typ)}")
        if an:
            lines.append(f"作者：{an}")
        if duration_s:
            lines.append(f"时长：{duration_s}")
        if size_label:
            lines.append(f"大小：{size_label}")
        if quality_s:
            lines.append(f"画质：{quality_s}")
        if img_count:
            lines.append(f"图集：共 {img_count} 张")
        if live_count:
            lines.append(f"实况：共 {live_count} 个")
            if live_img_count:
                lines.append(f"实况封面：共 {live_img_count} 张")
        if tags:
            lines.append("标签：" + " ".join(f"#{t}" for t in tags))
        if desc and str(desc) != str(title):
            lines.append(f"简介：{desc}")
        if vurl and include_video_line:
            lines.append(f"视频直链：{vurl}")
        mt = _rel_data(data, cfg_sv.get("path_music_title") or "music.title", j) or ""
        ma = _rel_data(data, cfg_sv.get("path_music_author") or "music.author", j) or ""
        mu = _rel_data(data, cfg_sv.get("path_music_url") or "music.url", j) or ""
        if mt or ma or mu:
            parts = [x for x in (str(mt) if mt else "", str(ma) if ma else "") if x]
            lines.append("原声：" + " / ".join(parts))
            if mu:
                lines.append(f"原声链接：{mu}")
        if show_extended and is_video_like:
            published = _format_publish_time(
                _rel_data(data, cfg_sv.get("path_create_time") or "create_time", j),
                _rel_data(data, cfg_sv.get("path_publish_time") or "publish_time", j),
            )
            if published:
                lines.append(f"发布时间：{published}")
        return "\n".join(lines) if lines else "（无文本信息）"

    @_tracked_parse
    async def _reply_short_video(self, event: AstrMessageEvent, link: str) -> None:
        parse_token = self._new_parse_token()
        await self._maybe_opening(event)
        if self._is_parse_cancelled(parse_token):
            logger.info("短视频解析在请求接口前被停止: %s", link)
            return

        try:
            j, data = await self._fetch_short_video(link)
        except Exception as e:
            if self._is_parse_cancelled(parse_token):
                logger.info("短视频解析已停止，不再发送失败消息: %s", link)
                return
            logger.warning("短视频解析失败: %s，链接: %s", e, link)
            await event.send(event.plain_result(_describe_parse_error(e)))
            return

        if self._is_parse_cancelled(parse_token):
            logger.info("短视频解析在接口返回后被停止: %s", link)
            return

        self._runtime_manager.set_state(TaskState.BUILDING)

        cfg_sv = _cfg(self._runtime_config(), "short_video", default={}) or {}
        msg_cfg = _cfg(self._runtime_config(), "message", default={}) or {}

        vurl = _first_video_url(data, cfg_sv, j)
        live_vs = _live_photo_videos(data, cfg_sv, j)
        video_urls: List[str] = []
        if vurl:
            video_urls.append(str(vurl).strip())
        for lv in live_vs:
            if lv and lv not in video_urls:
                video_urls.append(lv)

        imgs = _rel_data(data, cfg_sv.get("path_images_list") or "images", j)
        img_list: List[str] = []
        img_total_count = 0
        if isinstance(imgs, list):
            img_total_count = len(imgs)
            lim = max(1, _to_int(msg_cfg.get("max_images_per_work"), 9))
            for x in imgs[:lim]:
                if x:
                    img_list.append(str(x).strip())

        # 批量输出控制：检查图集/实况数量是否超过阈值。
        # 配置解析一律走 _to_int 容错，避免 WebUI 留空(None)导致 int() 崩溃而
        # 让整个解析流程直接失败。
        batch_cfg = _cfg(self._runtime_config(), "batch_output", default={}) or {}
        direct_json_enabled = bool(batch_cfg.get("direct_json_enabled", False))
        img_threshold = _to_int(batch_cfg.get("image_count_threshold"), 20)
        live_threshold = _to_int(batch_cfg.get("live_photo_count_threshold"), 10)

        if direct_json_enabled:
            live_count = len(live_vs)
            if img_total_count > img_threshold or live_count > live_threshold:
                logger.info(
                    "批量输出: 图集数量(%d)超过阈值(%d)或实况数量(%d)超过阈值(%d)，"
                    "发送完整响应的打包消息",
                    img_total_count, img_threshold, live_count, live_threshold,
                )
                await self._send_batch_full_response(
                    event,
                    j,
                    data,
                    cfg_sv,
                    link,
                    parse_token,
                    f"批量内容超过阈值（图集 {img_total_count} 张 / 实况 {live_count} 个）",
                )
                return

        cover = _rel_data(data, cfg_sv.get("path_cover") or "cover", j)
        cover_s = str(cover).strip() if cover else ""
        av = _rel_data(data, cfg_sv.get("path_author_avatar") or "author.avatar", j)
        av_s = str(av).strip() if av else ""

        # 接口成功但没有任何可用媒体：按契约不透传原始响应，改为可读提示
        # （优先使用接口 msg，例如“作品不存在/已删除”）。
        if not video_urls and not img_list and not cover_s:
            logger.warning(
                "短视频解析成功但无可用媒体，链接: %s，接口 msg: %s",
                link,
                get_path(j, cfg_sv.get("path_msg") or "msg"),
            )
            await event.send(event.plain_result(_empty_result_hint(j, cfg_sv)))
            return

        pack_send_video = bool(msg_cfg.get("pack_send_video", True))
        pack_include_cover = bool(msg_cfg.get("pack_include_cover", True))
        text_meta = bool(msg_cfg.get("short_video_text_metadata", True))
        show_extended = bool(msg_cfg.get("show_extended_metadata", True))

        # 视频文件大小阈值控制
        vth = _cfg(self._runtime_config(), "video_threshold", default={}) or {}
        force_link = bool(vth.get("force_link_enabled", False))
        threshold_on = bool(vth.get("threshold_enabled", True))
        threshold_mb = _to_int(vth.get("threshold_mb"), 100)

        # 视频资源会在后续作为 Video 节点或独立直链节点加入消息，元信息中
        # 不再重复展示同一个地址。
        include_video_line = not bool(video_urls)
        text = self._format_short_video_text(
            cfg_sv,
            data,
            j,
            include_video_line=include_video_line,
            show_extended=show_extended,
        )
        if bool(msg_cfg.get("pack_append_original_link", True)) and link:
            original_line = f"原始链接：{link.strip()}"
            if link.strip() and link.strip() not in text:
                text = f"{text.rstrip()}\n{original_line}"

        chain: List[Any] = []
        if text_meta and text.strip():
            chain.append(Plain(text))
        elif not text_meta and not (video_urls or img_list or cover_s):
            chain.append(Plain("解析完成"))

        if video_urls:
            if self._is_parse_cancelled(parse_token):
                logger.info("短视频解析在构造视频节点前被停止: %s", link)
                return

            if pack_include_cover and cover_s:
                ci = _make_image_node(cover_s)
                if ci:
                    chain.append(ci)
            alist = self._get_alist()
            upload_only = alist.is_enabled and _cfg(self._runtime_config(), "alist", "upload_only", default=False) if alist else False
            for vu in video_urls:
                if self._is_parse_cancelled(parse_token):
                    logger.info("短视频解析在处理视频资源时被停止: %s", link)
                    return

                vu_str = str(vu).strip()
                if pack_send_video:
                    # 阈值检查：决定是否应发送链接而非直接传输视频文件
                    send_as_link = False
                    if force_link:
                        # 全局强制链接发送开关开启，直接发送链接
                        send_as_link = True
                        logger.info("视频阈值: 全局强制链接发送开关已启用，使用链接发送: %s", vu_str)
                    elif threshold_on:
                        # 阈值开关开启，检测文件大小
                        try:
                            file_size = await _get_url_content_length(vu_str)
                            if self._is_parse_cancelled(parse_token):
                                logger.info("短视频解析在检测文件大小后被停止: %s", link)
                                return
                            if file_size is not None:
                                size_mb = file_size / (1024 * 1024)
                                if size_mb > threshold_mb:
                                    send_as_link = True
                                    logger.info(
                                        "视频阈值: 文件大小 %.2f MB 超过阈值 %d MB，使用链接发送: %s",
                                        size_mb, threshold_mb, vu_str,
                                    )
                                else:
                                    logger.info(
                                        "视频阈值: 文件大小 %.2f MB 未超过阈值 %d MB，正常发送: %s",
                                        size_mb, threshold_mb, vu_str,
                                    )
                            else:
                                logger.warning("视频阈值: 无法获取文件大小，按正常流程发送: %s", vu_str)
                        except Exception as e:
                            logger.warning("视频阈值: 检测文件大小异常: %s，按正常流程发送: %s", e, vu_str)

                    if send_as_link:
                        chain.append(Plain(f"视频：{vu_str}"))
                    elif alist:
                        temp_video: Optional[str] = None
                        try:
                            temp_video = await _download_video(vu_str)
                            if self._is_parse_cancelled(parse_token):
                                logger.info("短视频解析在下载视频后被停止: %s", link)
                                return
                            if temp_video:
                                alist_url = await alist.upload_file(temp_video, share_url=vu_str)
                                if self._is_parse_cancelled(parse_token):
                                    logger.info("短视频解析在上传视频后被停止: %s", link)
                                    return
                                if alist_url:
                                    chain.append(Plain(alist_url))
                                    if not upload_only:
                                        vn = _make_video_node(vu_str)
                                        if vn:
                                            chain.append(vn)
                                else:
                                    chain.append(Plain(f"视频：{vu_str}"))
                            else:
                                chain.append(Plain(f"视频：{vu_str}"))
                        except Exception as e:
                            logger.warning("Alist 上传失败: %s", str(e))
                            chain.append(Plain(f"视频：{str(vu).strip()}"))
                        finally:
                            if temp_video:
                                try:
                                    os.unlink(temp_video)
                                except Exception:
                                    pass
                    else:
                        vn = _make_video_node(vu_str)
                        chain.append(vn if vn is not None else Plain(f"视频：{vu_str}"))
                else:
                    chain.append(Plain(f"视频：{vu_str}"))
        else:
            for u in img_list:
                if self._is_parse_cancelled(parse_token):
                    logger.info("短视频解析在处理图片资源时被停止: %s", link)
                    return

                im = _make_image_node(u)
                if im:
                    chain.append(im)
            if not img_list and pack_include_cover and cover_s:
                ci = _make_image_node(cover_s)
                if ci:
                    chain.append(ci)

        if av_s and av_s != cover_s:
            chain.append(Plain("作者头像"))
            ai = _make_image_node(av_s)
            if ai:
                chain.append(ai)

        pack = bool(msg_cfg.get("pack_forward", True))
        name, uid = self._bot_identity(event)

        if self._is_parse_cancelled(parse_token):
            logger.info("短视频解析在发送结果前被停止: %s", link)
            return

        if pack:
            flat = _chain_to_forward_nodes(chain, name, uid)
            await self._send_packed_or_json(
                event, flat, j, parse_token, "短视频解析结果", link_url=link
            )
            return

        for comp in chain:
            if self._is_parse_cancelled(parse_token):
                logger.info("短视频非打包发送过程中收到停止指令: %s", link)
                return
            await self._dispatch_send(
                event,
                event.chain_result([comp]),
                "短视频解析结果节点",
                retry_same_payload=True,
            )

    async def _fetch_douyin_profile(self, profile_url: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        cfg_sv = _cfg(self._runtime_config(), "short_video", default={}) or {}
        endpoint = (cfg_sv.get("endpoint") or "").strip().rstrip("?&")
        if not endpoint:
            raise ValueError("未配置短视频 endpoint")
        param = cfg_sv.get("url_param_name") or "url"
        method = (cfg_sv.get("request_method") or "GET").upper()
        sv_headers = self._merged_request_headers("short_video")
        if method == "POST":
            j = await self._request_json("POST", endpoint, headers=sv_headers, data={param: profile_url})
        else:
            q = urlencode({param: profile_url})
            sep = "&" if "?" in endpoint else "?"
            j = await self._request_json("GET", f"{endpoint}{sep}{q}", headers=sv_headers)
        ok = _parse_success_codes(str(cfg_sv.get("success_codes", "200")))
        if not _code_ok(j, cfg_sv.get("path_code") or "code", ok):
            msg = get_path(j, cfg_sv.get("path_msg") or "msg") or "接口返回失败"
            raise ValueError(str(msg))
        profile_path = str(cfg_sv.get("profile_items_path") or "").strip()
        return j, _profile_items_from_response(j, profile_path)

    def _format_profile_item_text(
        self,
        item: Dict[str, Any],
        j: Dict[str, Any],
    ) -> str:
        lines: List[str] = []
        author = str(item.get("author", ""))
        typ = str(item.get("type", ""))
        desc = str(item.get("desc", ""))
        create_time = str(item.get("create_time", ""))
        duration = item.get("duration")
        mt = str(item.get("music_title", ""))
        ma = str(item.get("music_author", ""))
        stats = item.get("statistics", {}) or {}
        digg = str(stats.get("digg_count", "0"))
        comment = str(stats.get("comment_count", "0"))
        share = str(stats.get("share_count", "0"))
        collect = str(stats.get("collect_count", "0"))
        play = str(stats.get("play_count", "0"))

        if author:
            lines.append(f"👤 {author}")
        if typ:
            type_emoji = "🎬" if typ == "video" else "🖼️"
            lines.append(f"{type_emoji} 类型：{typ}")
        if desc:
            desc_short = str(desc)[:100] + "..." if len(str(desc)) > 100 else str(desc)
            lines.append(f"📝 {desc_short}")
        if create_time:
            lines.append(f"⏰ {create_time}")
        if duration:
            lines.append(f"⏳ 时长：{float(duration):.1f}秒")
        lines.append(f"📊 赞:{digg} 评:{comment} 转:{share} 藏:{collect} ▶️:{play}")
        if mt or ma:
            lines.append(f"🎵 原声：{mt} - {ma}" if mt and ma else f"🎵 原声：{mt or ma}")
        return "\n".join(lines)

    @_tracked_parse
    async def _reply_douyin_profile(self, event: AstrMessageEvent, profile_url: str) -> None:
        parse_token = self._new_parse_token()
        await self._maybe_opening(event)
        if self._is_parse_cancelled(parse_token):
            logger.info("抖音主页解析在请求接口前被停止: %s", profile_url)
            return

        try:
            j, items = await self._fetch_douyin_profile(profile_url)
        except Exception as e:
            if self._is_parse_cancelled(parse_token):
                logger.info("抖音主页解析已停止，不再发送失败消息: %s", profile_url)
                return
            logger.warning("抖音主页解析失败: %s，链接: %s", e, profile_url)
            await event.send(event.plain_result(_describe_parse_error(e, "抖音主页解析")))
            return

        if self._is_parse_cancelled(parse_token):
            logger.info("抖音主页解析在接口返回后被停止: %s", profile_url)
            return

        if not items:
            await event.send(event.plain_result("未获取到作品数据"))
            return

        msg_cfg = _cfg(self._runtime_config(), "message", default={}) or {}

        pagination = j.get("pagination", {}) or {}
        total = pagination.get("total", len(items))
        has_more = pagination.get("has_more", False)

        name, uid = self._bot_identity(event)
        nodes: List[Node] = []

        header_text = f"🎭 抖音主页作品列表（共 {total} 个作品）"
        nodes.append(Node(name=name, uin=uid, content=[Plain(header_text)]))

        max_items = min(len(items), int(msg_cfg.get("search_result_limit", 8) or 8))

        for i in range(max_items):
            item = items[i]
            if not isinstance(item, dict):
                continue
            item_text = self._format_profile_item_text(item, j)
            nodes.append(Node(name=name, uin=uid, content=[Plain(item_text)]))

            typ = str(item.get("type", ""))
            cover = str(item.get("cover", ""))

            if typ == "video":
                vurl = str(item.get("url", ""))
                if vurl:
                    vn = _make_video_node(vurl)
                    if vn:
                        nodes.append(Node(name=name, uin=uid, content=[vn]))
            elif typ == "image":
                images_data = item.get("images")
                if isinstance(images_data, list):
                    max_imgs = int(msg_cfg.get("max_images_per_work", 9) or 9)
                    for img_url in images_data[:max_imgs]:
                        if img_url:
                            im = _make_image_node(str(img_url))
                            if im:
                                nodes.append(Node(name=name, uin=uid, content=[im]))

            if cover:
                ci = _make_image_node(str(cover))
                if ci:
                    nodes.append(Node(name=name, uin=uid, content=[ci]))

        if has_more:
            nodes.append(Node(name=name, uin=uid, content=[Plain(f"⚠️ 还有更多作品未显示，请访问原主页查看完整列表")]))

        pack = bool(msg_cfg.get("pack_forward", True))
        if pack and nodes:
            flat = _chain_to_forward_nodes(nodes, name, uid)
            await self._send_packed_or_json(
                event, flat, j, parse_token, "抖音主页作品列表", link_url=profile_url
            )
            return

        for node in nodes:
            if self._is_parse_cancelled(parse_token):
                logger.info("抖音主页非打包发送过程中收到停止指令: %s", profile_url)
                return
            await self._dispatch_send(
                event,
                event.chain_result([node]),
                "抖音主页作品列表节点",
                retry_same_payload=True,
            )

    @_tracked_parse
    async def _netease_search(self, event: AstrMessageEvent, keyword: str) -> None:
        ne = _cfg(self._runtime_config(), "netease", default={}) or {}
        ep = (ne.get("search_endpoint") or "").strip()
        if not ep:
            await event.send(
                event.plain_result(
                    "未配置网易云搜索接口。请在插件配置中填写 netease.search_endpoint（仅网易云支持关键词搜索）。"
                )
            )
            return
        await self._maybe_opening(event)
        method = (ne.get("search_method") or "GET").upper()
        kparam = ne.get("search_keyword_param") or "keywords"
        extra: Dict[str, Any] = {}
        try:
            extra = json.loads(ne.get("search_extra_params_json") or "{}")
        except json.JSONDecodeError:
            extra = {}
        if isinstance(extra, dict) and "type" not in extra and "Type" not in extra:
            extra = {"type": "search", **extra}
        params = {**extra, kparam: keyword}
        ne_headers = self._merged_request_headers("netease")
        try:
            if method == "POST":
                j = await self._request_json("POST", ep, headers=ne_headers, data=params)
            else:
                q = urlencode(params, quote_via=quote_plus)
                sep = "&" if "?" in ep else "?"
                j = await self._request_json("GET", f"{ep}{sep}{q}", headers=ne_headers)
        except Exception as e:
            logger.warning("网易云搜索失败: %s", e)
            await event.send(event.plain_result(_describe_parse_error(e, "网易云搜索")))
            return
        dbg, dbg_len = self._debug_cfg()
        if dbg:
            try:
                logger.info(
                    "[BKtools][debug] netease.search raw: %s",
                    str(j)[:dbg_len],
                )
            except Exception:
                pass
        ok = _parse_success_codes(str(ne.get("search_success_codes", "200")))
        if not _code_ok(j, ne.get("search_path_code") or "code", ok):
            m = get_path(j, ne.get("search_path_msg") or "msg") or "搜索失败"
            await event.send(event.plain_result(str(m)))
            return
        lp = (ne.get("search_list_path") or "data.songs").strip()
        items = get_path(j, lp)
        if not isinstance(items, list):
            await event.send(event.plain_result("搜索无结果或列表路径配置不对。"))
            return
        lim = int((_cfg(self._runtime_config(), "message", default={}) or {}).get("search_result_limit", 8))
        lines: List[str] = []
        candidates: List[Dict[str, Any]] = []
        for i, it in enumerate(items[:lim]):
            if not isinstance(it, dict):
                continue
            nm = get_path(it, ne.get("search_item_name") or "name")
            ar = get_path(it, ne.get("search_item_artist") or "artists")
            sid = get_path(it, ne.get("search_item_id") or "id")
            alb = get_path(it, ne.get("search_item_album") or "album")
            line = f"{i + 1}. {nm or '—'}"
            if ar:
                line += f" — {ar}"
            if alb:
                line += f" 《{alb}》"
            if sid is not None:
                line += f"  (id={sid})"
            lines.append(line)
            candidates.append(
                {
                    "id": sid,
                    "name": str(nm or "").strip(),
                    "artist": str(ar or "").strip(),
                }
            )
        if not lines:
            await event.send(event.plain_result("没有可展示的搜索结果。"))
            return
        self._cache_pick_candidates(event, keyword, candidates)
        header = f"网易云搜索「{keyword}」：\n" + "\n".join(lines)
        header += (
            "\n\n请输入序号选歌（如 `1`），或使用 `/bk点歌 1`。"
            "\n提示：其他音乐平台请直接发分享链接使用「音乐解析」，不支持关键词搜索。"
        )
        await event.send(event.plain_result(header))

    @_tracked_parse
    async def _music_link_parse(self, event: AstrMessageEvent, link: str) -> None:
        plat = _music_platform(link)
        ne = _cfg(self._runtime_config(), "netease", default={}) or {}
        lo = _cfg(self._runtime_config(), "link_only_music", default={}) or {}
        timeout_sec, ua = self._http_cfg()
        headers = {"User-Agent": ua}
        to = aiohttp.ClientTimeout(total=timeout_sec)

        endpoint = ""
        url_key = "url"
        method = "GET"
        use_kuwo_paths = False
        netease_parse_extra: Dict[str, Any] = {}
        netease_song_id = ""
        album_p = ""
        size_p = ""
        level_p = ""

        req_link = link
        try:
            async with aiohttp.ClientSession(timeout=to, headers=headers) as session:
                if not plat:
                    resolved = await self._resolve_url_redirect(req_link)
                    if resolved:
                        req_link = resolved
                        plat = _music_platform(req_link)
                if plat == "netease":
                    expanded = await self._expand_netease_short_link(session, req_link)
                    if expanded:
                        req_link = expanded
        except Exception:
            pass

        if not plat:
            logger.warning("无法识别音乐平台链接: %s", req_link)
            await event.send(event.plain_result(f"无法识别音乐平台链接：{req_link}"))
            return

        if plat == "netease":
            endpoint = (
                (ne.get("link_parse_endpoint") or "").strip()
                or (ne.get("search_endpoint") or "").strip()
            )
            url_key = ne.get("link_url_param") or "url"
            method = (ne.get("link_method") or "GET").upper()
            if not endpoint:
                await event.send(
                    event.plain_result(
                        "未配置网易云接口地址：请填写 link_parse_endpoint 或 search_endpoint。"
                    )
                )
                return
            try:
                netease_parse_extra = json.loads(
                    ne.get("link_parse_extra_params_json") or "{}"
                )
            except json.JSONDecodeError:
                netease_parse_extra = {}
            if not netease_parse_extra:
                netease_parse_extra = {"type": "json", "level": "standard"}
            netease_song_id = _extract_netease_song_id(req_link)
            path_code = ne.get("parse_path_code") or "code"
            path_msg = ne.get("parse_path_msg") or "msg"
            ok = _parse_success_codes(str(ne.get("parse_success_codes", "200")))
            droot = (ne.get("parse_data_root") or "data").strip() or "data"
            title_p = ne.get("parse_title") or "name"
            author_p = ne.get("parse_author") or "singer"
            cover_p = ne.get("parse_cover") or "cover"
            audio_p = ne.get("parse_audio_url") or "url"
            lyric_p = ne.get("parse_lyric") or "lyric"
            album_p = ne.get("parse_album") or "album"
            size_p = ne.get("parse_size") or "size"
            level_p = ne.get("parse_quality") or "label"
        else:
            if plat == "qq":
                endpoint = (lo.get("qq_endpoint") or "").strip()
                url_key = lo.get("qq_url_param") or "url"
            elif plat == "qishui":
                endpoint = (lo.get("qishui_endpoint") or "").strip()
                url_key = lo.get("qishui_url_param") or "url"
            else:
                endpoint = (lo.get("kuwo_endpoint") or "").strip()
                url_key = lo.get("kuwo_url_param") or "url"
                use_kuwo_paths = True
            if not endpoint:
                await event.send(event.plain_result(f"未配置 {plat} 解析 endpoint。"))
                return
            path_code = lo.get("generic_path_code") or "code"
            path_msg = lo.get("generic_path_msg") or "msg"
            ok = _parse_success_codes(str(lo.get("generic_success_codes", "200")))
            droot = (lo.get("generic_data_root") or "data").strip() or "data"
            lyric_p = ""
            if plat == "qq":
                title_p = lo.get("generic_title") or "name"
                author_p = lo.get("generic_author") or "author"
                cover_p = lo.get("generic_cover") or "cover"
                audio_p = lo.get("generic_audio_url") or "url"
                lyric_p = lo.get("qq_lyric_path") or "lrc_data"
            elif plat == "qishui":
                # 2.0 字段名（name/singer/album/cover）优先；1.0 旧字段
                # （albumname/artistsname/artistsmedium_avatar_url.0）
                # 由 _extract_music_fields 的候选链兜底
                title_p = lo.get("qishui_title_path") or "name"
                author_p = lo.get("qishui_author_path") or "singer"
                audio_p = lo.get("qishui_audio_path") or "url"
                cover_p = lo.get("qishui_cover_path") or "cover"
                lyric_p = lo.get("qishui_lyric_path") or "lyric"
            elif use_kuwo_paths:
                title_p = lo.get("kuwo_title_path") or "title"
                author_p = lo.get("kuwo_author_path") or "artist"
                audio_p = lo.get("kuwo_audio_path") or "music_url"
                cover_p = lo.get("kuwo_cover_path") or "pic"
                lyric_p = lo.get("kuwo_lyric_path") or "lyrics_url"

        await self._maybe_opening(event)
        req_params: Dict[str, Any] = {url_key: req_link}
        if plat == "netease":
            req_params = {**netease_parse_extra, url_key: req_link}
        # 附加自定义请求头：全局 http.headers + 对应接口节（netease 或 link_only_music）
        api_headers = self._merged_request_headers(
            "netease" if plat == "netease" else "link_only_music"
        )
        if api_headers:
            headers = {**headers, **api_headers}
        try:
            async with aiohttp.ClientSession(timeout=to, headers=headers) as session:
                if method == "POST":
                    j = await self._session_post_form(
                        session, endpoint, req_params, headers=api_headers
                    )
                else:
                    q = urlencode(req_params, quote_via=quote_plus)
                    sep = "&" if "?" in endpoint else "?"
                    j = await self._session_get_json(
                        session, f"{endpoint}{sep}{q}", headers=api_headers
                    )

                # 某些 163_music 部署在 type=json 场景下对 url 参数兼容较弱：
                # 若首轮返回成功但关键信息为空，自动回退为 ids=<song_id> 重试一次。
                if plat == "netease" and netease_song_id:
                    data0 = get_path(j, droot)
                    if not isinstance(data0, dict):
                        data0 = {}
                    title0 = _rel_data(data0, title_p, j) or ""
                    audio0 = _rel_data(data0, audio_p, j) or ""
                    need_retry_ids = not (str(title0).strip() or str(audio0).strip())
                    if need_retry_ids:
                        retry_params = dict(netease_parse_extra)
                        retry_params.pop(url_key, None)
                        retry_params["ids"] = netease_song_id
                        if method == "POST":
                            j2 = await self._session_post_form(
                                session, endpoint, retry_params, headers=api_headers
                            )
                        else:
                            q2 = urlencode(retry_params, quote_via=quote_plus)
                            sep2 = "&" if "?" in endpoint else "?"
                            j2 = await self._session_get_json(
                                session, f"{endpoint}{sep2}{q2}", headers=api_headers
                            )
                        if _code_ok(j2, path_code, ok):
                            j = j2
        except Exception as e:
            logger.warning("音乐解析失败: %s", e)
            await event.send(event.plain_result(_describe_parse_error(e, "音乐解析")))
            return

        dbg, dbg_len = self._debug_cfg()
        if dbg:
            try:
                logger.info(
                    "[BKtools][debug] netease.parse raw: %s",
                    str(j)[:dbg_len],
                )
            except Exception:
                pass

        if not _code_ok(j, path_code, ok):
            m = get_path(j, path_msg) or "解析失败"
            if dbg:
                try:
                    logger.info(
                        "[BKtools][debug] netease.parse failed_code raw: %s",
                        str(j)[:dbg_len],
                    )
                except Exception:
                    pass
            await event.send(event.plain_result(str(m)))
            return
        data = get_path(j, droot)
        if not isinstance(data, dict):
            data = {}
        # 兼容 163_music 的 type=json 返回：字段可能直接在根对象上（无 data 包裹）
        # 示例：{status:200, name:..., ar_name:..., url:...}
        if plat == "netease" and (not data):
            if isinstance(j, dict) and any(k in j for k in ("name", "url", "ar_name", "al_name", "pic")):
                data = j

        msg_cfg = _cfg(self._runtime_config(), "message", default={}) or {}

        # 字段缺失/占位符不展示，并按候选路径兜底，避免出现
        # “曲名：—”与“艺人：未知艺术家”（汽水音乐原声/无版权曲目）这类上游噪声。
        mf = _extract_music_fields(
            data,
            j,
            title_p=title_p,
            author_p=author_p,
            cover_p=cover_p,
            audio_p=audio_p,
            lyric_p=lyric_p,
            album_p=album_p,
            size_p=size_p,
            level_p=level_p,
        )
        title = mf["title"]
        author = mf["author"]
        cover = mf["cover"]
        audio = mf["audio"]

        if dbg:
            try:
                logger.info(
                    "[BKtools][debug] 音乐解析(%s) data: title=%r, author=%r, audio=%r, album=%r, size=%r, format=%r, level=%r, bitrate=%r, raw_data=%s",
                    plat,
                    title,
                    author,
                    audio,
                    mf["album"],
                    mf["size"],
                    mf["format"],
                    mf["level"],
                    mf["bitrate"],
                    str(data)[:dbg_len],
                )
            except Exception:
                pass

        # 标题/艺人为空时留一条诊断日志（不含凭据），便于定位上游字段变动
        if not title or not author:
            try:
                data_keys = (
                    sorted(data.keys())[:20] if isinstance(data, dict) else type(data).__name__
                )
            except Exception:
                data_keys = "?"
            logger.info(
                "[BKtools] 音乐解析字段缺失（平台=%s，缺失=%s，data_keys=%s）",
                plat,
                ",".join(n for n, v in (("title", title), ("author", author)) if not v) or "-",
                data_keys,
            )

        # 没有音频直链时整条结果不可用，给出友好提示而不是一张空卡片
        if not audio:
            logger.warning("音乐解析未返回音频直链（平台=%s，链接=%s）", plat, req_link)
            await event.send(
                event.plain_result(
                    "音乐解析失败：接口未返回可播放的音频直链，请稍后重试或换一个分享链接。"
                )
            )
            return

        text = _format_music_card(plat, mf)
        
        # 尝试发送语音消息
        voice_sent = False
        if msg_cfg.get("send_voice_message") and audio:
            try:
                logger.info("尝试发送语音消息: %s", audio)
                max_size_mb = _to_int(msg_cfg.get("voice_max_size_mb"), 10)
                temp_audio = await _download_audio(str(audio), timeout_sec=self._http_cfg()[0], max_size_mb=max_size_mb)
                if temp_audio:
                    wav_path = _convert_to_wav(temp_audio, max_duration_sec=_to_int(msg_cfg.get("voice_max_duration"), 60))
                    if wav_path and os.path.exists(wav_path):
                        try:
                            voice_node = Record(file=wav_path, url=wav_path)
                            await event.send(event.chain_result([voice_node]))
                            voice_sent = True
                            logger.info("语音消息发送成功")
                        finally:
                            # 清理临时文件
                            if os.path.exists(wav_path):
                                os.unlink(wav_path)
                    else:
                        logger.warning("音频转换失败")
            except Exception as e:
                logger.warning("发送语音消息失败: %s", e)
        
        # 发送文本消息和封面
        music_caps = self._adapter_caps(event)
        if not music_caps["forward"]:
            # 不支持 Nodes（QQ 官方机器人 / 微信系）：卡片文本逐条发送。
            # 官方机器人渲染播放器卡片（内嵌封面），开启 official_buttons 时
            # 音频/歌词等长链接转为按钮行，卡片发送失败兜底纯文本。
            md_on = self._official_markdown_enabled(event)
            if md_on:
                rows: List[Any] = []
                if self._official_buttons_enabled(event):
                    rows = self._official_button_rows(text, req_link or "")
                card_md = _render_player_card_md(
                    text, cover_url=str(cover or ""), buttons_enabled=bool(rows)
                )
                logger.info(
                    "BKtools v%s 官方机器人音乐播放器卡片已渲染（封面=%s, 按钮=%s）",
                    PLUGIN_VERSION, bool(cover), bool(rows),
                )
                if rows:
                    outcome = await self._send_official_card(
                        event, card_md, rows, "音乐卡片"
                    )
                else:
                    outcome = await self._dispatch_send(
                        event,
                        self._text_result(event, card_md, markdown=True),
                        "音乐卡片",
                        retry_same_payload=True,
                    )
                if outcome != "ok":
                    await event.send(event.plain_result(text))
            else:
                await event.send(self._text_result(event, text))
            if cover and not md_on:
                ci = _make_image_node(str(cover))
                if ci:
                    await event.send(event.chain_result([ci]))
        elif msg_cfg.get("pack_forward") and cover:
            name, uid = self._bot_identity(event)
            cov_node = _make_image_node(str(cover))
            nodes = [Node(name=name, uin=uid, content=[Plain(text)])]
            if cov_node:
                nodes.append(Node(name=name, uin=uid, content=[cov_node]))
            await event.send(event.chain_result([Nodes(nodes)]))
        else:
            await event.send(event.plain_result(text))
            if cover:
                ci = _make_image_node(str(cover))
                if ci:
                    await event.send(event.chain_result([ci]))

    def _cmd_arg(self, event: AstrMessageEvent, *aliases: str) -> str:
        t = (event.message_str or "").strip()
        for a in aliases:
            for p in (f"/{a}", a):
                if t.startswith(p):
                    return t[len(p) :].strip()
        return ""

    def _updates_cfg(self) -> Dict[str, Any]:
        return _cfg(self.config, "updates", default={}) or {}

    async def _github_bytes(self, url: str, max_bytes: int) -> bytes:
        safe_url = await _validate_public_http_url(url)
        timeout_sec, ua = self._http_cfg()
        reliability = self._http_reliability_cfg()
        return await self._http_client.request_bytes(
            "GET",
            safe_url,
            timeout_sec=max(10, timeout_sec),
            user_agent=ua,
            max_response_bytes=max_bytes,
            retries=max(0, min(int(reliability.get("retry_count", 2) or 0), 3)),
            backoff_sec=max(
                0.0, min(float(reliability.get("retry_backoff_ms", 500) or 0) / 1000, 5.0)
            ),
            circuit_threshold=max(
                1, min(int(reliability.get("circuit_failure_threshold", 5) or 5), 20)
            ),
            circuit_recovery_sec=max(
                5, min(int(reliability.get("circuit_recovery_sec", 60) or 60), 3600)
            ),
            allow_redirects=True,
            max_redirects=5,
        )

    async def _check_github_update(self) -> UpdateInfo:
        release_error: Optional[Exception] = None
        try:
            raw = await self._github_bytes(f"{GITHUB_API}/releases/latest", 1024 * 1024)
            release = self._loads_response_json(raw)
            latest = str(release.get("tag_name") or "").strip()
            download_url = str(release.get("zipball_url") or "").strip()
            if latest and download_url:
                compare_versions(latest, PLUGIN_VERSION)
                return UpdateInfo(
                    current_version=f"v{PLUGIN_VERSION}",
                    latest_version=latest,
                    download_url=download_url,
                    source="GitHub Release",
                    release_url=str(release.get("html_url") or ""),
                    notes=str(release.get("body") or "")[:1000],
                )
        except Exception as ex:
            release_error = ex
            logger.info("读取 GitHub Release 失败，回退到默认分支: %s", ex)

        try:
            repo_raw = await self._github_bytes(GITHUB_API, 512 * 1024)
            repo_info = self._loads_response_json(repo_raw)
            branch = str(repo_info.get("default_branch") or "main").strip()
            if not re.fullmatch(r"[0-9A-Za-z._/-]+", branch) or ".." in branch:
                raise ValueError("GitHub 默认分支名称异常")
            metadata_url = (
                f"https://raw.githubusercontent.com/{REPOSITORY}/{branch}/metadata.yaml"
            )
            metadata_raw = await self._github_bytes(metadata_url, 128 * 1024)
            latest = metadata_version(metadata_raw.decode("utf-8-sig"))
            return UpdateInfo(
                current_version=f"v{PLUGIN_VERSION}",
                latest_version=latest,
                download_url=f"https://codeload.github.com/{REPOSITORY}/zip/refs/heads/{branch}",
                source=f"GitHub {branch} 分支",
                release_url=f"https://github.com/{REPOSITORY}",
            )
        except Exception as branch_error:
            raise RuntimeError(
                f"无法检查 GitHub 更新；Release: {release_error or '无可用版本'}；仓库分支: {branch_error}"
            ) from branch_error

    @filter.command("bk版本")
    @_event_scoped()
    async def cmd_version(self, event: AstrMessageEvent):
        """显示插件版本与更新仓库。"""
        await event.send(
            event.plain_result(
                f"BKtools v{PLUGIN_VERSION}\n"
                f"运行文件：{os.path.abspath(__file__)}\n"
                f"更新仓库：https://github.com/{REPOSITORY}\n"
                "管理员可使用 /bk检查更新 和 /bk更新插件。"
            )
        )

    @filter.command("bk检查更新")
    @_event_scoped()
    @_management_only
    async def cmd_check_update(self, event: AstrMessageEvent):
        """从官方 GitHub 仓库检查新版本。"""
        try:
            info = await self._check_github_update()
            if info.available:
                text = (
                    f"发现 BKtools 新版本：{info.latest_version}\n"
                    f"当前版本：v{PLUGIN_VERSION}\n来源：{info.source}\n"
                    "使用 /bk更新插件 可下载并安装，安装后必须重启 AstrBot。"
                )
            else:
                text = f"BKtools 当前已是最新版本：v{PLUGIN_VERSION}（{info.source}）"
            await event.send(event.plain_result(text))
        except Exception as ex:
            logger.warning("检查插件更新失败: %s", ex)
            await event.send(event.plain_result(f"检查更新失败：{ex}"))

    @filter.command("bk更新插件")
    @_event_scoped()
    @_management_only
    async def cmd_update_plugin(self, event: AstrMessageEvent):
        """从官方 GitHub 仓库下载、验证、备份并安装新版本。"""
        cfg = self._updates_cfg()
        if not bool(cfg.get("self_update_enabled", True)):
            await event.send(event.plain_result("插件自更新已在后台配置中关闭。"))
            return
        if self._update_lock.locked():
            await event.send(event.plain_result("已有插件更新任务正在执行，请稍后再试。"))
            return
        async with self._update_lock:
            try:
                info = await self._check_github_update()
                if not info.available:
                    await event.send(event.plain_result(f"当前已是最新版本：v{PLUGIN_VERSION}"))
                    return
                max_mb = max(1, min(int(cfg.get("max_download_mb", 20) or 20), 100))
                archive = await self._github_bytes(info.download_url, max_mb * 1024 * 1024)
                backup, count = await asyncio.to_thread(
                    install_archive,
                    archive,
                    os.path.dirname(os.path.abspath(__file__)),
                    expected_version=info.latest_version,
                )
                await event.send(
                    event.plain_result(
                        f"BKtools 已更新到 {info.latest_version}，共替换 {count} 个文件。\n"
                        f"备份目录：{backup}\n"
                        "请立即完整重启 AstrBot 后再继续使用插件。"
                    )
                )
            except Exception as ex:
                logger.exception("插件自更新失败")
                await event.send(event.plain_result(f"插件更新失败，已尽量回滚：{ex}"))

    @filter.command("bk状态")
    @_event_scoped()
    @_management_only
    async def cmd_status(self, event: AstrMessageEvent):
        """查看当前群/会话的有效配置与运行状态。"""
        self._prune_runtime_caches(force=True)
        cfg = self._runtime_config()
        features = self._runtime_features_var.get() or {}
        trigger = _cfg(cfg, "trigger", default={}) or {}
        message = _cfg(cfg, "message", default={}) or {}
        group_control = _cfg(self.config, "group_control", default={}) or {}
        scope = self._runtime_scope()
        active = len(self._active_parse_tasks.get(scope, set()))
        states = self._runtime_manager.state_counts(scope)
        state_text = "、".join(f"{key}={value}" for key, value in sorted(states.items())) or "无"
        open_circuits = sum(
            1 for state in self._runtime_manager.circuits.values() if state.opened_until > time.time()
        )
        uptime = max(0, int(time.time() - self._started_at))
        feature_text = "、".join(
            name
            for key, name in (
                ("short_video", "短视频"),
                ("douyin_profile", "抖音主页"),
                ("netease", "网易云"),
                ("music", "音乐链接"),
            )
            if features.get(key, True)
        ) or "无"
        text = (
            "【BKtools 当前状态】\n"
            f"会话：{scope}\n"
            f"群聊模式：{group_control.get('mode', 'all')}\n"
            f"可用功能：{feature_text}\n"
            f"自动短视频：{bool(trigger.get('auto_short_video', True))}\n"
            f"自动主页：{bool(trigger.get('auto_douyin_profile', True))}\n"
            f"自动音乐：{bool(trigger.get('auto_music_link', True))}\n"
            f"合并转发：{bool(message.get('pack_forward', True))}\n"
            f"活动解析任务：{active}\n"
            f"任务状态：{state_text}\n"
            f"接口熔断：{open_circuits}\n"
            f"活动发放：{self._open_api_status_text()}\n"
            f"HTTP 会话：{'已连接' if self._http_client.session and not self._http_client.session.closed else '未建立'}\n"
            f"去重缓存：{len(self._dedup_cache)}\n"
            f"点歌缓存：{len(self._netease_pick_cache)}\n"
            f"运行时长：{uptime // 3600} 小时 {(uptime % 3600) // 60} 分钟"
        )
        await event.send(event.plain_result(text))

    @filter.command("bk诊断")
    @_event_scoped()
    @_management_only
    async def cmd_diagnostics(self, event: AstrMessageEvent):
        """检查依赖、接口配置与关键安全控制。"""
        cfg = self._runtime_config()
        checks: List[str] = []
        checks.append(f"ℹ️ 插件版本：v{PLUGIN_VERSION}")
        checks.append(f"ℹ️ 运行文件：{os.path.abspath(__file__)}")
        for label, path in (
            ("短视频接口", ("short_video", "endpoint")),
            ("网易云搜索", ("netease", "search_endpoint")),
            ("网易云解析", ("netease", "link_parse_endpoint")),
        ):
            value = str(_cfg(cfg, *path, default="") or "").strip()
            parsed = urlparse(value)
            ok = bool(value and parsed.scheme in {"http", "https"} and parsed.hostname)
            checks.append(f"{'✅' if ok else '❌'} {label}：{'已配置' if ok else '地址无效或为空'}")
        checks.append(f"{'✅' if shutil.which('ffmpeg') else '⚠️'} ffmpeg：{'可用' if shutil.which('ffmpeg') else '未找到，语音转换不可用'}")
        try:
            import pydub  # noqa: F401
            pydub_ok = True
        except ImportError:
            pydub_ok = False
        checks.append(f"{'✅' if pydub_ok else '⚠️'} pydub：{'可用' if pydub_ok else '未安装，语音转换不可用'}")
        try:
            await _validate_public_http_url("http://127.0.0.1/")
        except ValueError:
            ssrf_ok = True
        else:
            ssrf_ok = False
        checks.append(f"{'✅' if ssrf_ok else '❌'} URL 安全防护：{'正常' if ssrf_ok else '异常'}")
        now = time.time()
        opened = [
            state for state in self._runtime_manager.circuits.values()
            if state.opened_until > now
        ]
        failures = sum(state.failures for state in self._runtime_manager.circuits.values())
        last_errors = [state.last_error for state in self._runtime_manager.circuits.values() if state.last_error]
        checks.append(f"{'⚠️' if opened else '✅'} 接口健康：失败计数 {failures}，熔断 {len(opened)}")
        if last_errors:
            sanitized = re.sub(r"https?://[^\s]+", "[URL已隐藏]", last_errors[-1])
            checks.append(f"ℹ️ 最近错误：{sanitized[:160]}")
        alist_cfg = _cfg(cfg, "alist", default={}) or {}
        if alist_cfg.get("enable"):
            alist_ok = all(alist_cfg.get(key) for key in ("url", "username", "password"))
            checks.append(f"{'✅' if alist_ok else '❌'} Alist：{'配置完整' if alist_ok else '缺少地址或账号密码'}")
        open_api_cfg = self._open_api_cfg()
        if open_api_cfg.get("enabled"):
            api_key = str(open_api_cfg.get("api_key") or "").strip()
            has_key = bool(api_key) and _SENSITIVE_REDACTED_KEY_RE.search(api_key) is None
            checks.append(
                "✅ 开放 API：已启用，接口 Key 已配置"
                if has_key
                else "❌ 开放 API：已启用，但接口 Key 为空或仍是脱敏值"
            )
        else:
            checks.append("⚠️ 开放 API：未启用，/bk领奖 活动发放不可用")
        campaigns_raw = self._campaigns_raw()
        if campaigns_raw:
            checks.append(f"✅ 活动配置：共 {len(campaigns_raw)} 个活动")
        else:
            checks.append("⚠️ 活动配置：未配置任何活动（campaigns 为空）")
        # 官方机器人（qq_official 系）环境自检：版本 / Markdown / 全量消息指引
        try:
            caps = self._adapter_caps(event)
            msg_cfg_diag = _cfg(cfg, "message", default={}) or {}
            checks.append(
                f"ℹ️ 平台适配器：{caps.get('name') or '未知'}"
                f"｜合并转发{'✓' if caps.get('forward') else '✗'}"
                f"｜文件{'✓' if caps.get('file') else '✗'}"
                f"｜Markdown{'✓' if caps.get('markdown') else '✗'}"
                f"｜Markdown API{'✓' if self._markdown_api_available(event) else '✗'}"
                f"｜按钮组件{'✓' if _BUTTONS_READY else '✗'}"
            )
            checks.append(
                "ℹ️ 输出配置："
                f"official_markdown={bool(msg_cfg_diag.get('official_markdown', True))}"
                f"，official_buttons={bool(msg_cfg_diag.get('official_buttons', True))}"
                f"，pack_forward={bool(msg_cfg_diag.get('pack_forward', True))}"
            )
            if caps.get("markdown") or caps.get("name") == "qq_official":
                md_on = self._official_markdown_enabled(event)
                checks.append(
                    f"{'✅' if md_on else '⚠️'} 官方机器人：v{PLUGIN_VERSION}，Markdown 排版"
                    f"{'已启用' if md_on else '未启用（检查 message.official_markdown）'}"
                )
                checks.append(
                    "ℹ️ 非@消息：需在 QQ 开放平台开通「接收所有消息」，且群主在群设置"
                    "里把机器人消息范围改为「接收全部消息」，否则群里必须 @ 机器人才触发"
                )
                checks.append("ℹ️ 渲染自检：发送 /bk卡片测试 可直接验证 Markdown 与按钮是否生效")
        except Exception:
            pass
        await event.send(event.plain_result("【BKtools 诊断】\n" + "\n".join(checks)))

    @filter.command("bk卡片测试")
    @_event_scoped()
    @_management_only
    async def cmd_card_test(self, event: AstrMessageEvent, image_url: str = ""):
        """发送一张官方机器人 Markdown 卡片样张，验证渲染与按钮权限。

        用法：/bk卡片测试 [封面图片URL]
        - 渲染成「标题 + 内嵌封面 + 加粗字段 + 引用块 + 分割线」说明 Markdown 生效；
        - 若看到 `##`、`**`、`![]()` 原样出现，说明适配器/平台未启用 Markdown；
        - 开启 message.official_buttons 时样张会附带按钮，用于验证 keyboard 权限。
        """
        caps = self._adapter_caps(event)
        if not caps.get("markdown"):
            await event.send(
                event.plain_result(
                    f"当前平台「{caps.get('name') or '未知'}」不是 QQ 官方机器人，"
                    "Markdown 卡片不适用；其他平台使用原有卡片输出。"
                )
            )
            return
        if not self._markdown_api_available(event):
            await event.send(
                event.plain_result(
                    "当前 AstrBot 版本的 MessageChain 不支持 use_markdown 标记，"
                    "发不出 Markdown 卡片。请升级 AstrBot 后重试（插件要求 v4.10.4+）。"
                )
            )
            return
        md_on = self._official_markdown_enabled(event)
        if not md_on:
            await event.send(
                event.plain_result(
                    "Markdown 排版已被关闭：请在 WebUI 插件配置 message.official_markdown 中开启。"
                )
            )
            return
        cover = str(image_url or "").strip()
        if cover and not cover.startswith(("http://", "https://")):
            cover = ""
        sample = "\n".join(
            [
                "标题：卡片渲染测试",
                "作者：BKtools",
                f"平台：{caps.get('name')}",
                "时长：00:20",
                "大小：1.20MB",
                "画质：original",
                "原声：BKtools 创作的原声",
                "视频直链：https://example.com/bktools-card-test.mp4",
            ]
        )
        btn_on = self._official_buttons_enabled(event)
        rows = self._official_button_rows(sample, "") if btn_on else []
        card_md = _render_player_card_md(
            sample, cover_url=cover, buttons_enabled=bool(rows)
        )
        logger.info(
            "BKtools v%s 卡片测试：平台=%s，Markdown API=%s，按钮=%s，封面=%s",
            PLUGIN_VERSION, caps.get("name"), True, bool(rows), bool(cover),
        )
        block_before = float(getattr(self, "_button_block_until", 0) or 0)
        if rows:
            outcome = await self._send_official_card(
                event, card_md, rows, "卡片测试"
            )
        else:
            outcome = await self._dispatch_send(
                event,
                self._text_result(event, card_md, markdown=True),
                "卡片测试",
                retry_same_payload=True,
            )
        if outcome != "ok":
            await event.send(
                event.plain_result(
                    "卡片发送失败，已按纯文本兜底。常见原因：机器人未获得 Markdown/keyboard "
                    "权限，或 AstrBot 适配器版本过旧（详见 AstrBot 日志）。"
                )
            )
        elif rows and float(getattr(self, "_button_block_until", 0) or 0) > block_before:
            await event.send(
                event.plain_result(
                    "按钮发送失败（多为未申请 keyboard 权限），已降级为无按钮卡片。"
                    "开通权限后 10 分钟内会自动重试。"
                )
            )

    @filter.command("bk帮助")
    @_event_scoped()
    async def cmd_help(self, event: AstrMessageEvent):
        """BKtools 命令说明"""
        await event.send(
            event.plain_result(
                "【BKtools】\n"
                "· /bk视频 <链接> — 短视频解析\n"
                "· /bk主页 <用户主页链接> — 抖音主页作品列表解析\n"
                "· /bk网易云 <关键词> — 仅网易云搜索（需配置搜索接口）\n"
                "· /bk点歌 <序号> — 选择最近一次网易云搜索结果\n"
                "· /bk音乐 <音乐分享链接> — QQ/汽水/酷我/网易等链接解析（无搜索）\n"
                "· /bk领奖 — 领取已配置的活动奖励（需先完成对应任务）\n"
                "· /bk开放Key、/bk活动 — 校验开放 API Key、查看活动配置（管理员）\n"
                "· /bk状态 — 查看当前群有效配置与任务状态（管理员）\n"
                "· /bk诊断 — 检查接口、依赖和安全防护（管理员）\n"
                "· /bk卡片测试 [封面URL] — 发送官方机器人 Markdown 卡片样张（管理员）\n"
                "· /bk版本 — 查看当前版本和更新仓库\n"
                "· /bk检查更新 — 检查 GitHub 新版本（管理员）\n"
                "· /bk更新插件 — 从官方 GitHub 仓库安全更新（管理员）\n"
                "· /bk停止解析 — 停止当前群或私聊会话中的解析任务\n"
                "· /bk清理缓存 — 清理插件临时音频文件\n"
                "短视频：默认合并转发（Nodes），纯图集会合并图片节点，"
                "含视频时用 Video.fromURL，详见 message / short_video 配置项。\n"
                "语音消息：可在配置中开启 send_voice_message，自动将音乐转换为语音消息发送。\n"
                "自动短视频：可在配置中开启 trigger.auto_short_video"
            )
        )

    @filter.command("bk视频")
    @_event_scoped("short_video")
    async def cmd_video_slash(self, event: AstrMessageEvent):
        """短视频解析"""
        arg = self._cmd_arg(event, "bk视频")
        if not arg:
            await event.send(event.plain_result("用法：/bk视频 <作品链接>"))
            return
        await self._reply_short_video(event, arg)

    @filter.command("bktv")
    @_event_scoped("short_video")
    async def cmd_video_alias(self, event: AstrMessageEvent):
        """短视频解析（简写）"""
        arg = self._cmd_arg(event, "bktv")
        if not arg:
            await event.send(event.plain_result("用法：/bktv <作品链接>"))
            return
        await self._reply_short_video(event, arg)

    @filter.command("bk主页")
    @_event_scoped("douyin_profile")
    async def cmd_douyin_profile(self, event: AstrMessageEvent):
        """抖音主页作品列表解析"""
        arg = self._cmd_arg(event, "bk主页")
        if not arg:
            await event.send(event.plain_result("用法：/bk主页 <抖音用户主页链接>"))
            return
        await self._reply_douyin_profile(event, arg)

    @filter.command("bk网易云")
    @_event_scoped("netease")
    async def cmd_netease(self, event: AstrMessageEvent):
        """网易云音乐搜索（仅此平台支持关键词搜索）"""
        arg = self._cmd_arg(event, "bk网易云")
        if not arg:
            await event.send(event.plain_result("用法：/bk网易云 <关键词>"))
            return
        await self._netease_search(event, arg)

    @filter.command("bk搜歌")
    @_event_scoped("netease")
    async def cmd_netease_alias(self, event: AstrMessageEvent):
        """同 bk网易云"""
        arg = self._cmd_arg(event, "bk搜歌")
        if not arg:
            await event.send(event.plain_result("用法：/bk搜歌 <关键词>（仅网易云）"))
            return
        await self._netease_search(event, arg)

    @filter.command("bk点歌")
    @_event_scoped("netease")
    async def cmd_netease_pick(self, event: AstrMessageEvent):
        """按序号选择最近搜索结果"""
        arg = self._cmd_arg(event, "bk点歌")
        if not arg or not arg.strip().isdigit():
            await event.send(event.plain_result("用法：/bk点歌 <序号>（例如 /bk点歌 1）"))
            return
        ok = await self._netease_pick_by_index(event, int(arg.strip()))
        if not ok:
            await event.send(
                event.plain_result("没有可选的搜歌结果，请先使用 /bk网易云 <关键词>。")
            )

    @filter.command("bk音乐")
    @_event_scoped("music")
    async def cmd_music(self, event: AstrMessageEvent):
        """音乐链接解析（不支持关键词；非网易平台仅链接）"""
        arg = self._cmd_arg(event, "bk音乐")
        if not arg:
            await event.send(
                event.plain_result(
                    "用法：/bk音乐 <分享链接>\n"
                    "说明：QQ/汽水/酷我只支持链接解析；搜歌请用 /bk网易云"
                )
            )
            return
        await self._music_link_parse(event, arg)

    @filter.command("bk清理缓存")
    @_event_scoped()
    @_management_only
    async def cmd_cleanup_cache(self, event: AstrMessageEvent):
        """清理本插件的临时缓存文件"""
        try:
            count, size = _cleanup_temp_files()
            self._prune_runtime_caches(force=True)
            await event.send(event.plain_result(f"已清理 {count} 个缓存文件，释放 {size / 1024 / 1024:.2f} MB"))
        except Exception as e:
            await event.send(event.plain_result(f"清理失败: {str(e)}"))

    @filter.command("bk停止解析")
    @_event_scoped()
    @_management_only
    async def cmd_stop_parsing(self, event: AstrMessageEvent):
        """停止当前群或私聊会话的解析任务、截断输出并清理缓存"""
        # 递增当前会话取消代次；其他群和私聊会话不受影响。
        self._cancel_active_parses()
        cancelled_tasks = self._cancel_scope_tasks()

        # 清理缓存文件
        try:
            count, size = _cleanup_temp_files()
            if count > 0:
                await event.send(event.plain_result(f"已停止当前会话解析任务 {cancelled_tasks} 个，已清理 {count} 个缓存文件，释放 {size / 1024 / 1024:.2f} MB"))
            else:
                await event.send(event.plain_result(f"已停止当前会话解析任务 {cancelled_tasks} 个"))
        except Exception as e:
            await event.send(event.plain_result(f"已停止当前会话解析，但清理缓存失败: {str(e)}"))

    # ============================================================= 活动发放（v1.7.0）
    #
    # 🔴 核心边界：插件只**生成并发放** CDK，绝不代兑——开放 Key 的 owner 是
    # 持有账号，代兑会把奖励发到管理员账号。
    # 双门禁：插件 open_api.enabled（默认关）+ 平台 systemsettings open_api.enabled，
    # 任一关闭即整体停用；发放链路失败一律 fail-closed，网络/结构异常按「服务繁忙」
    # 收敛，绝不放行。联系方式只进问卷请求体，不落日志与状态文件。

    def _open_api_cfg(self) -> Dict[str, Any]:
        return _cfg(self._runtime_config(), "open_api", default={}) or {}

    def _campaigns_raw(self) -> List[Dict[str, Any]]:
        # 走运行时配置（与其他配置节一致），这样单群覆盖里的 campaigns 才会生效；
        # 直接读 self.config 会绕过覆盖，导致群级活动配置被静默忽略。
        raw = _cfg(self._runtime_config(), "campaigns", default=[])
        return list(_plain_config_value(raw)) if isinstance(raw, list) else []

    def _survey_providers_raw(self) -> List[Dict[str, Any]]:
        raw = self._open_api_cfg().get("survey_providers")
        return list(_plain_config_value(raw)) if isinstance(raw, list) else []

    def _chat_claim_key(self, event: AstrMessageEvent) -> str:
        """聊天维度的领取标识：运行时 scope + 群/会话 + 发送者。"""
        return f"{self._runtime_scope()}:{self._event_group_id(event)}:{self._event_sender_id(event)}"

    def _event_contact_map(self, event: AstrMessageEvent) -> Dict[str, str]:
        """当前事件里能取到的聊天身份（供 contact_source=chat_member 的问卷用）。"""
        nickname = self._event_nickname(event)
        sender = self._event_sender_id(event)
        return {
            "nickname": nickname,
            "nick": nickname,
            "name": nickname,
            "group_nickname": nickname,
            "qq": sender,
            "id": sender,
            "sender_id": sender,
        }

    def _chat_member_contact_provider(self, chat_key: str, contact_field_id: str) -> str:
        """chat_member_provider：按 contact_field_id 从当次领取的上下文取联系方式。"""
        contact = self._claim_contact.get(chat_key) or {}
        return str(contact.get(str(contact_field_id or "").strip()) or "").strip()

    @staticmethod
    def _state_file_path(raw: Any) -> str:
        """状态文件路径：绝对/相对当前目录直接沿用，裸文件名落到插件 data 目录。"""
        text = str(raw or "").strip()
        if not text:
            text = "campaign_state.json"
        if os.path.isabs(text) or text.startswith(("./", "../")):
            return text
        return os.path.join("data", "plugin_data", "astrbot_plugin_bktools", text)

    def _campaign_fingerprint(self) -> Tuple[Any, ...]:
        """影响编排器构建的配置指纹；变化则下次调用时重建。

        必须覆盖**构建期读到的每一项**：活动模板和问卷 provider 也在构建期
        被 load_campaigns / build_survey_verifier 消费，漏掉它们会让「只改
        活动列表」这类改动被静默忽略，得靠 /bk活动 重载 才能生效。
        """
        cfg = self._open_api_cfg()
        return (
            str(cfg.get("base_url") or "").strip(),
            str(cfg.get("api_key") or "").strip(),
            str(cfg.get("key_type") or "opka_").strip(),
            bool(cfg.get("verify_tls", True)),
            str(cfg.get("github_token") or "").strip(),
            self._state_file_path(cfg.get("state_file")),
            int(cfg.get("cooldown_sec", 0) or 0),
            int(cfg.get("max_claims_per_chat_day", 0) or 0),
            self._config_digest(self._survey_providers_raw()),
            self._config_digest(self._campaigns_raw()),
        )

    @staticmethod
    def _config_digest(items: List[Dict[str, Any]]) -> str:
        """把配置列表转成稳定可比较的字符串（键排序，不可序列化对象走 default=str）。"""
        return json.dumps(items, ensure_ascii=False, sort_keys=True, default=str)


    def _campaign_build(self) -> Optional[str]:
        """（重）构建编排器。返回 None = 成功；否则返回停用原因（fail-closed）。"""
        cfg = self._open_api_cfg()
        base_url = str(cfg.get("base_url") or "").strip()
        api_key = str(cfg.get("api_key") or "").strip()
        key_type = str(cfg.get("key_type") or "opka_").strip()
        client: Optional[OpenApiClient] = None
        try:
            client = OpenApiClient(
                base_url,
                api_key,
                key_type,
                verify_tls=bool(cfg.get("verify_tls", True)),
                runtime=self._runtime_manager,
                trace_factory=_public_url_trace_config,
                **self._open_api_http_kwargs(),
            )
        except (TypeError, ValueError) as ex:
            logger.warning("活动发放不可用（配置无效）: %s", ex)
            return "开放 API 配置无效（base_url / api_key 缺失或格式不对）"
        if not client.can_generate:
            return "当前开放 Key 为普通用户 Key（opk_），没有发奖权限，请配置管理员作用域 Key（opka_）"

        verifiers: Dict[str, _sa.SurveyVerifier] = {}
        providers = self._survey_providers_raw()
        if providers:
            for provider_cfg in providers:
                provider_type = str(provider_cfg.get("type") or "").strip().lower()
                verifier = _sa.build_survey_verifier(provider_cfg)
                if verifier is None:
                    # 显式配置的 provider 构建失败不能静默跳过——否则会退化成
                    # 「问卷不校验也照样发奖」，属 fail-open，必须整体停用。
                    logger.error("问卷 provider 构建失败，活动发放整体停用: type=%s", provider_type)
                    return f"问卷 provider 配置无效（type={provider_type or '空'}）"
                verifiers[provider_type] = verifier
        else:
            # 无 provider 配置：只要活动开启问卷校验就整体停用，绝不静默跳过校验
            if any((item.get("survey") or {}).get("enabled") for item in self._campaigns_raw()):
                return "有活动开启了问卷校验但未配置 open_api.survey_providers"

        reliability = self._http_reliability_cfg()
        timeout_sec = int(reliability.get("timeout_sec", 30) or 30)
        try:
            star_verifier = GitHubStarVerifier(
                str(cfg.get("github_token") or ""),
                timeout_sec=max(5, min(timeout_sec, 60)),
            )
            state_store = CampaignStateStore(self._state_file_path(cfg.get("state_file")))
            manager = CampaignManager(
                client=client,
                state_store=state_store,
                star_verifier=star_verifier,
                survey_verifiers=verifiers,
                chat_member_provider=self._chat_member_contact_provider,
                cooldown_sec=int(cfg.get("cooldown_sec", 0) or 0),
                max_claims_per_chat_day=int(cfg.get("max_claims_per_chat_day", 0) or 0),
            )
        except (TypeError, ValueError) as ex:
            logger.warning("活动发放不可用（构建失败）: %s", ex)
            return "活动发放初始化失败"
        manager.load_campaigns(self._campaigns_raw())
        self._campaign_mgr = manager
        self._campaign_fingerprint_saved = self._campaign_fingerprint()
        logger.info("活动发放已就绪: 活动 %d 个，问卷 provider %d 个", len(manager.campaigns()), len(verifiers))
        return None

    async def _campaign_ensure(self, *, force: bool = False) -> Tuple[Optional[CampaignManager], Optional[str]]:
        """惰性构建/复用编排器，返回 (manager, 停用原因)。

        注意方法名不能叫 `_campaign_mgr`：`__init__` 里的同名**实例属性**存的是
        编排器实例，会遮蔽类方法，导致 `await self._campaign_mgr()` 直接
        `TypeError: 'NoneType' object is not callable`（v1.7.0 遗留 bug）。

        force=True 用于管理命令的「强制重建」：直接丢弃旧实例再按当前配置重建，
        不比对指纹，这样临时改动配置后 /bk活动 能立刻看到效果。
        """
        if not _CAMPAIGN_READY:
            return None, "可选模块缺失，活动发放功能停用（请用完整插件包重新安装）"
        async with self._campaign_lock:
            if force:
                self._campaign_mgr = None
                self._campaign_fingerprint_saved = None
            fingerprint = self._campaign_fingerprint()
            if self._campaign_mgr is not None and self._campaign_fingerprint_saved == fingerprint:
                return self._campaign_mgr, None
            reason = self._campaign_build()
            if reason:
                return None, reason
            return self._campaign_mgr, None

    async def _open_api_test(self) -> Tuple[bool, str]:
        """连通性 + 读 scope 校验：GET /open-api/account。"""
        if not _CAMPAIGN_READY:
            return False, "可选模块缺失，活动发放功能停用（请用完整插件包重新安装）"
        cfg = self._open_api_cfg()
        base_url = str(cfg.get("base_url") or "").strip()
        api_key = str(cfg.get("api_key") or "").strip()
        key_type = str(cfg.get("key_type") or "opka_").strip()
        if not (base_url and api_key):
            return False, "open_api.base_url / api_key 未配置"
        client = OpenApiClient(
            base_url, api_key, key_type, verify_tls=bool(cfg.get("verify_tls", True)),
            runtime=self._runtime_manager, trace_factory=_public_url_trace_config,
            **self._open_api_http_kwargs(),
        )
        try:
            data = await client.get_account()
            if not isinstance(data, dict) or not data:
                return False, "接口响应为空"
            enabled = data.get("openApiEnabled")
            username = str(data.get("username") or data.get("nickname") or "-")
            # bool 字面量 True/False 显示为「开/关」；None 表示上游未返回该字段
            if enabled is True:
                switch = "已开启"
            elif enabled is False:
                switch = "已关闭（未开启时任何调用都会被拒）"
            else:
                switch = "未返回"
            return True, f"连通正常，账号 {username}，平台开放 API 开关：{switch}"
        except OpenApiError as ex:
            extra = ""
            if getattr(ex, "code", "") == "openapi.ip_forbidden" and getattr(ex, "client_ip", ""):
                extra = f"（出口 IP {ex.client_ip}，请把它加进开放 Key 白名单）"
            return False, f"{ex.describe()}{extra}"
        except (TypeError, ValueError) as ex:
            return False, f"配置异常: {ex}"
        finally:
            await client.close()

    # ------------------------------------------------------------- 命令

    @filter.command("bk领奖")
    @_event_scoped("campaign")
    async def cmd_claim(self, event: AstrMessageEvent):
        """活动领取 CDK"""
        await self._run_claim(event, self._cmd_arg(event, "bk领奖"))

    @filter.command("bk领奖码")
    @_event_scoped("campaign")
    async def cmd_claim_alias(self, event: AstrMessageEvent):
        """活动领取 CDK（简写）"""
        await self._run_claim(event, self._cmd_arg(event, "bk领奖码"))

    async def _run_claim(self, event: AstrMessageEvent, arg: str) -> None:
        manager, reason = await self._campaign_ensure()
        if manager is None:
            await event.send(event.plain_result(f"活动领奖暂不可用：{reason or '未配置'}"))
            return
        parts = re.split(r"\s+", str(arg or "").strip())
        campaign_id = parts[0] if parts else ""
        login = parts[1] if len(parts) > 1 else ""
        if not campaign_id:
            live = [cid for cid, item in manager.campaigns().items() if item.enabled]
            if len(live) == 1:
                campaign_id = live[0]
            else:
                await event.send(
                    event.plain_result(
                        "用法：/bk领奖 <活动ID> <GitHub用户名>\n"
                        + (f"可用活动：{'、'.join(live)}" if live else "当前没有进行中的活动，用 /bk活动 列表 查看详情。")
                    )
                )
                return
        key = self._chat_claim_key(event)
        self._claim_contact[key] = self._event_contact_map(event)
        try:
            result = await manager.claim(
                key,
                campaign_id,
                login,
                chat_ref=f"{self._runtime_scope()}:{self._event_group_id(event)}",
            )
        finally:
            self._claim_contact.pop(key, None)
        definition = manager.get(result.campaign_id) or manager.get(campaign_id)
        if result.result is ClaimResult.GRANTED:
            url = str(self._open_api_cfg().get("redeem_url") or "").strip()
            text = (
                "🎉 领取成功，下面是你的专属兑换码：\n"
                f"CDK：{result.code}\n"
                + (f"兑换入口：{url}\n" if url else "登录平台后进入「兑换码」页面自行兑换。\n")
                + "兑换码会自动绑定到你的平台账号，请自己登录后兑换，不要外传。"
            )
        else:
            text = _CLAIM_USER_TEXT.get(result.result, "服务暂时繁忙，请稍后再试。")
            if result.result is ClaimResult.STAR_NOT_STARRED and definition:
                text += f"请先去 star {definition.star.owner}/{definition.star.repo} 再来领奖。"
        if result.detail:
            logger.info(
                "活动领奖: campaign=%s chat=%s result=%s %s",
                result.campaign_id, key, result.result.value, result.detail,
            )
        await event.send(event.plain_result(text))

    async def _open_api_status_text(self) -> str:
        """开放 API 状态（Key 全程只出掩码）。"""
        if not _CAMPAIGN_READY:
            return "【开放 API 状态】\n可选模块缺失，活动发放功能停用（短视频/音乐解析不受影响）。\n请用完整插件包重新安装后重试。"
        cfg = self._open_api_cfg()
        enabled = bool(cfg.get("enabled", False))
        base_url = str(cfg.get("base_url") or "").strip()
        api_key = str(cfg.get("api_key") or "").strip()
        key_type = str(cfg.get("key_type") or "opka_").strip()
        key_text = _oca.mask_key(api_key)
        if api_key and not api_key.startswith(key_type):
            key_text += "（前缀与 key_type 不一致）"
        if api_key and base_url and not base_url.startswith("https://"):
            key_text += "（接口地址非 https，建议改用 https）"
        manager, reason = await self._campaign_ensure()
        lines = [
            "【开放 API 状态】",
            f"功能开关：{'开启' if enabled else '关闭（open_api.enabled=false，领奖不可用）'}",
            f"接口地址：{base_url or '未配置'}",
            f"开放 Key：{key_text if key_text else '未配置'}",
            f"Key 作用域：{key_type}{'（管理员作用域）' if key_type == KEY_TYPE_ADMIN else '（普通用户，无发奖权限）'}",
            f"证书校验：{'开启' if cfg.get('verify_tls', True) else '关闭'}",
            f"GitHub Token：{'已配置' if str(cfg.get('github_token') or '') else '未配置（star 校验走匿名配额 60 次/小时）'}",
            f"状态文件：{self._state_file_path(cfg.get('state_file'))}",
            f"兑换入口：{str(cfg.get('redeem_url') or '') or '未配置（领取成功时不附链接）'}",
        ]
        if manager is not None:
            lines.append(f"活动配置：{len(manager.campaigns())} 个已加载")
        else:
            lines.append(f"活动配置：未就绪（{reason or '未知原因'}）")
        return "\n".join(lines)

    @filter.command("bk开放Key")
    @_event_scoped("campaign")
    @_management_only
    async def cmd_openkey(self, event: AstrMessageEvent):
        """开放 API 状态 / 连通性测试（管理员）"""
        await self._openkey_action(event, self._cmd_arg(event, "bk开放Key", "bk开放key"))

    @filter.command("bk开放key")
    @_event_scoped("campaign")
    @_management_only
    async def cmd_openkey_alias(self, event: AstrMessageEvent):
        """开放 API 状态 / 连通性测试（管理员，小写）"""
        await self._openkey_action(event, self._cmd_arg(event, "bk开放key", "bk开放Key"))

    async def _openkey_action(self, event: AstrMessageEvent, arg: str) -> None:
        action = str(arg or "").strip()
        if action in ("测试", "test", "连通", "check"):
            await event.send(event.plain_result("正在测试开放 API 连通性…"))
            ok, message = await self._open_api_test()
            await event.send(event.plain_result(f"{'✅' if ok else '❌'} 开放 API 测试：{message}"))
            return
        await event.send(event.plain_result(await self._open_api_status_text()))

    @filter.command("bk活动")
    @_event_scoped("campaign")
    @_management_only
    async def cmd_campaign(self, event: AstrMessageEvent):
        """活动列表 / 重载配置（管理员）"""
        await self._campaign_action(event, self._cmd_arg(event, "bk活动"))

    async def _campaign_action(self, event: AstrMessageEvent, arg: str) -> None:
        action = str(arg or "").strip()
        if action in ("重载", "reload"):
            manager, reason = await self._campaign_ensure(force=True)
            if manager is None:
                await event.send(event.plain_result(f"❌ 重载失败：{reason or '未知原因'}"))
                return
            await event.send(
                event.plain_result(f"✅ 活动配置已重载，当前 {len(manager.campaigns())} 个活动。")
            )
            return
        manager, reason = await self._campaign_ensure()
        if manager is None:
            await event.send(event.plain_result(f"活动发放未就绪：{reason or '未知原因'}"))
            return
        campaigns = manager.campaigns()
        if not campaigns:
            await event.send(event.plain_result("当前没有配置活动（campaigns 为空）。"))
            return
        lines = ["【活动列表】"]
        now = time.time()
        for cid, definition in sorted(campaigns.items()):
            gate = manager.check_gate(definition, now_ts=now)
            status = _GATE_STATUS_TEXT.get(gate.value, "⚪ 已关闭")
            gates: List[str] = []
            if definition.star.enabled:
                gates.append(f"star {definition.star.owner}/{definition.star.repo}")
            if definition.survey.enabled:
                gates.append(f"问卷 {definition.survey.provider or '未指定'}")
            line = f"{cid} — {definition.name or cid}：{status}"
            if gates:
                line += "（" + "、".join(gates) + "）"
            lines.append(line)
        await event.send(event.plain_result("\n".join(lines)))

    @filter.event_message_type(EventMessageType.ALL)
    @_event_scoped(silent=True)
    async def on_auto(self, event: AstrMessageEvent):
        """自动触发短视频 / 音乐链"""
        user_id = getattr(event, 'user_id', None)
        if user_id == 0:
            return
        tr = _cfg(self._runtime_config(), "trigger", default={}) or {}
        text = event.message_str or ""
        pure = text.strip()
        if pure.isdigit() and self._feature_enabled("netease"):
            picked = await self._netease_pick_by_index(event, int(pure))
            if picked:
                return
        # 引用/回复消息：message_str 可能为空，或被客户端截断成 "https://v.douyin.com/WM5..."；
        # 被引用消息的完整原文保存在 Reply 组件上，作为候选来源一并参与选链
        urls, has_complete = _select_parse_urls(text, _quoted_message_texts(event))
        if not urls:
            return
        # 拦截命令消息，避免命令处理器和自动触发重复执行
        text_stripped = text.strip()
        if text_stripped.startswith("/bk") or text_stripped.startswith("bk"):
            return
        if not has_complete:
            # 只剩被截断的残链，请求接口必然失败，给出提示而不是白白消耗一次解析
            logger.warning("自动解析跳过截断链接（引用长文本被客户端截断）: %s", urls)
            await event.send(
                event.plain_result(
                    "链接不完整：客户端引用长文本时会截断内容，导致解析失败。\n"
                    "请直接发送完整链接，或复制整段分享文案后单独发送。"
                )
            )
            return

        resolved_urls = []
        for u in urls:
            resolved = await self._resolve_url_redirect(u)
            resolved_urls.append(resolved)

        def _matched(check_fn, orig, resolved):
            return check_fn(resolved) or (orig != resolved and check_fn(orig))

        runtime_scope = self._runtime_scope()
        for i, resolved in enumerate(resolved_urls):
            orig = urls[i]

            if self._feature_enabled("music") and tr.get("auto_music_link"):
                if _is_qishui_url(resolved) or _matched(_is_qishui_url, orig, resolved):
                    # 使用解析锁防止重复解析
                    lock_key = f"{runtime_scope}:music_link:{orig}"
                    if self._parsing_lock.get(lock_key):
                        logger.info("音乐链接解析任务已在进行中，跳过: %s", orig)
                        return
                    self._parsing_lock[lock_key] = True
                    if self._check_dedup(orig):
                        self._parsing_lock.pop(lock_key, None)
                        return
                    try:
                        await self._music_link_parse(event, orig)
                    finally:
                        self._parsing_lock.pop(lock_key, None)
                    return

                if _matched(_music_platform, orig, resolved):
                    # 使用解析锁防止重复解析
                    lock_key = f"{runtime_scope}:music_link:{orig}"
                    if self._parsing_lock.get(lock_key):
                        logger.info("音乐链接解析任务已在进行中，跳过: %s", orig)
                        return
                    self._parsing_lock[lock_key] = True
                    if self._check_dedup(orig):
                        self._parsing_lock.pop(lock_key, None)
                        return
                    try:
                        await self._music_link_parse(event, orig)
                    finally:
                        self._parsing_lock.pop(lock_key, None)
                    return

        for i, resolved in enumerate(resolved_urls):
            orig = urls[i]

            if self._feature_enabled("douyin_profile") and tr.get("auto_douyin_profile"):
                if _matched(_is_douyin_profile_url, orig, resolved):
                    # 使用解析锁防止重复解析
                    lock_key = f"{runtime_scope}:douyin_profile:{orig}"
                    if self._parsing_lock.get(lock_key):
                        logger.info("抖音主页解析任务已在进行中，跳过: %s", orig)
                        return
                    self._parsing_lock[lock_key] = True
                    if self._check_dedup(orig):
                        self._parsing_lock.pop(lock_key, None)
                        return
                    try:
                        await self._reply_douyin_profile(event, orig)
                    finally:
                        self._parsing_lock.pop(lock_key, None)
                    return

        for i, resolved in enumerate(resolved_urls):
            orig = urls[i]

            if self._feature_enabled("short_video") and tr.get("auto_short_video"):
                if _matched(_video_auto_match, orig, resolved) and not _is_douyin_profile_url(resolved):
                    # 使用解析锁防止重复解析
                    lock_key = f"{runtime_scope}:short_video:{orig}"
                    if self._parsing_lock.get(lock_key):
                        logger.info("短视频解析任务已在进行中，跳过: %s", orig)
                        return
                    self._parsing_lock[lock_key] = True
                    if self._check_dedup(orig):
                        self._parsing_lock.pop(lock_key, None)
                        return
                    try:
                        await self._reply_short_video(event, orig)
                    finally:
                        self._parsing_lock.pop(lock_key, None)
                    return

    async def terminate(self):
        """卸载插件时取消活动任务并释放运行时缓存。"""
        tasks = [
            task
            for scope_tasks in self._active_parse_tasks.values()
            for task in scope_tasks
            if not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active_parse_tasks.clear()
        self._scope_semaphores.clear()
        self._dedup_cache.clear()
        self._netease_pick_cache.clear()
        self._parsing_lock.clear()
        self._alist_uploaders.clear()
        self._runtime_manager.cleanup_all()
        await self._http_client.close()
        _cleanup_temp_files()
