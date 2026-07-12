"""
AstrBot 插件：BugPk 工具集（短视频解析、网易云搜歌、多平台音乐链解析）。
说明：音乐搜索仅走网易云配置；QQ/汽水/酷我等仅支持分享链接解析。
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
from functools import wraps
from collections.abc import Mapping
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, quote_plus, urlencode, urljoin, urlparse

import aiohttp

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Image, Node, Nodes, Plain, Record, Video
try:
    from astrbot.api.message_components import File
except ImportError:  # AstrBot 旧版本没有 File 组件时保留文本回退
    File = None
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.event_message_type import EventMessageType

try:
    from .bktools_runtime import (
        CircuitOpenError,
        RuntimeManager,
        SafeHttpClient,
        TaskState,
        create_json_temp,
    )
except ImportError:
    from bktools_runtime import (
        CircuitOpenError,
        RuntimeManager,
        SafeHttpClient,
        TaskState,
        create_json_temp,
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


PLUGIN_VERSION = "1.6.2"


def _cfg(root: Any, *keys: str, default: Any = None) -> Any:
    if not isinstance(root, dict):
        return default
    cur: Any = root
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


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
            await event.send(event.plain_result("权限不足：该命令仅限授权管理员使用。"))
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


def _live_photo_videos(data: Any, cfg_sv: Dict[str, Any], j: Dict[str, Any]) -> List[str]:
    lp = _rel_data(data, cfg_sv.get("path_live_photo_list") or "live_photo", j)
    if not isinstance(lp, list):
        return []
    sub = (cfg_sv.get("path_live_photo_video") or "video").strip() or "video"
    out: List[str] = []
    for item in lp:
        if isinstance(item, dict):
            u = get_path(item, sub)
            if u:
                s = str(u).strip()
                if s:
                    out.append(s)
    return out


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


def _make_file_node(path: str, name: str):
    if File is None:
        return None
    for method_name in ("fromFileSystem", "from_file", "fromPath"):
        method = getattr(File, method_name, None)
        if callable(method):
            try:
                return method(path, name=name)
            except TypeError:
                try:
                    return method(path)
                except Exception:
                    pass
            except Exception:
                pass
    for kwargs in (
        {"file": path, "name": name},
        {"path": path, "name": name},
        {"file_path": path, "name": name},
    ):
        try:
            return File(**kwargs)
        except Exception:
            continue
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


def _extract_urls(text: str) -> List[str]:
    raw_urls = re.findall(r"https?://[^\s\]>\"']+", text or "")
    # 处理中文/英文结尾标点，避免复制分享文案时把标点一起带进链接
    tail_punc = ".,!?;:)]}，。！？；：）】》」』、"
    return [u.rstrip(tail_punc) for u in raw_urls if u.rstrip(tail_punc)]


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

    async def _is_management_allowed(self, event: AstrMessageEvent) -> bool:
        cfg = self._security_cfg()
        mode = str(cfg.get("management_permission") or "admin_only").lower()
        if mode == "everyone":
            return True
        sender_id = self._event_sender_id(event)
        configured_admins = {str(x).strip() for x in cfg.get("admin_user_ids", []) if str(x).strip()}
        if sender_id in configured_admins:
            return True
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

    def _send_key(self, label: str, payload: Dict[str, Any]) -> str:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return f"{self._runtime_scope()}:{label}:{digest}"

    async def _send_json_result(
        self,
        event: AstrMessageEvent,
        payload: Dict[str, Any],
        parse_token: Tuple[str, int],
        reason: str,
    ) -> bool:
        """以单条纯文本发送解析接口原始 JSON，不拆分为作品消息。"""
        if self._is_parse_cancelled(parse_token):
            logger.info("%s前收到停止解析指令，取消发送 JSON", reason)
            return False
        json_text = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        encoded = json_text.encode("utf-8")
        limits = self._runtime_limits_cfg()
        text_limit = max(256, int(limits.get("json_text_max_chars", 3500) or 3500))
        file_limit = max(
            text_limit,
            int(limits.get("json_file_max_bytes", 2 * 1024 * 1024) or 0),
        )
        try:
            if len(json_text) <= text_limit:
                result = event.plain_result(json_text)
            else:
                if len(encoded) > file_limit:
                    raise ValueError("解析 JSON 超过文件回退大小限制")
                if File is None:
                    raise RuntimeError("当前 AstrBot 版本不支持文件消息组件")
                path = create_json_temp(encoded)
                self._runtime_manager.register_temp(self._runtime_scope(), path)
                file_node = _make_file_node(path, "bktools-result.json")
                if file_node is None:
                    raise RuntimeError("当前平台无法构造文件消息")
                result = event.chain_result([file_node])
        except Exception as ex:
            logger.error("%s，构造解析 JSON 消息失败: %s", reason, ex)
            try:
                await event.send(event.plain_result("解析结果发送失败，请稍后重试或联系管理员查看诊断信息。"))
            except Exception:
                logger.error("发送 JSON 失败提示也失败: %s", reason)
            return False

        try:
            self._runtime_manager.set_state(TaskState.SENDING)
            await event.send(result)
            return True
        except Exception as ex:
            # send() 抛错时平台可能已经接收消息；再次发送会造成重复结果。
            logger.warning("%s，发送结果状态不确定，为避免重复不再重试: %s", reason, ex)
            return False

    async def _send_packed_or_json(
        self,
        event: AstrMessageEvent,
        forward_nodes: List[Node],
        raw_json: Dict[str, Any],
        parse_token: Tuple[str, int],
        label: str,
    ) -> bool:
        """合并转发采用至多一次发送；仅发送前的确定失败回退 JSON。"""
        send_key = self._send_key(label, raw_json)
        if not self._runtime_manager.claim_send(send_key):
            logger.info("跳过重复发送: %s", label)
            return False
        if not forward_nodes:
            logger.warning("%s没有可用的合并转发节点，直接发送解析 JSON", label)
            return await self._send_json_result(
                event, raw_json, parse_token, f"{label}无法构造合并转发"
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

        try:
            self._runtime_manager.set_state(TaskState.SENDING)
            await event.send(packed_result)
            return True
        except Exception as ex:
            # AstrBot 的通用发送接口没有跨平台幂等消息 ID。异常可能发生在
            # 平台已接收之后，因此任何自动重试或 JSON 回退都可能形成重复消息。
            logger.warning(
                "%s发送结果状态不确定，为避免重复打包消息，不重试也不追加 JSON: %s",
                label,
                ex,
            )
            return False

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

    async def _session_get_json(
        self, session: aiohttp.ClientSession, url: str
    ) -> Dict[str, Any]:
        return await self._request_json("GET", url)

    async def _session_post_form(
        self, session: aiohttp.ClientSession, url: str, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        return await self._request_json("POST", url, data=data)

    async def _request_json(self, method: str, url: str, **kwargs: Any) -> Dict[str, Any]:
        timeout_sec, ua = self._http_cfg()
        reliability = self._http_reliability_cfg()
        limits = self._runtime_limits_cfg()
        raw = await self._http_client.request_bytes(
            method,
            url,
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
        if method == "POST":
            j = await self._request_json("POST", endpoint, data={param: link})
        else:
            q = urlencode({param: link})
            sep = "&" if "?" in endpoint else "?"
            j = await self._request_json("GET", f"{endpoint}{sep}{q}")
        path_root = (cfg_sv.get("path_data_root") or "data").strip() or "data"
        data = get_path(j, path_root)
        if not isinstance(data, dict):
            data = {}
        ok = _parse_success_codes(str(cfg_sv.get("success_codes", "200")))
        if not _code_ok(j, cfg_sv.get("path_code") or "code", ok):
            msg = get_path(j, cfg_sv.get("path_msg") or "msg") or "接口返回失败"
            raise ValueError(str(msg))
        return j, data

    def _format_short_video_text(
        self,
        cfg_sv: Dict[str, Any],
        data: Any,
        j: Dict[str, Any],
        *,
        include_video_line: bool = True,
    ) -> str:
        lines: List[str] = []
        title = _rel_data(data, cfg_sv.get("path_title") or "title", j) or ""
        desc = _rel_data(data, cfg_sv.get("path_desc") or "desc", j) or ""
        typ = _rel_data(data, cfg_sv.get("path_type") or "type", j) or ""
        an = _rel_data(data, cfg_sv.get("path_author_name") or "author.name", j) or ""
        vurl = _first_video_url(data, cfg_sv, j)
        mt = _rel_data(data, cfg_sv.get("path_music_title") or "music.title", j) or ""
        ma = _rel_data(data, cfg_sv.get("path_music_author") or "music.author", j) or ""
        mu = _rel_data(data, cfg_sv.get("path_music_url") or "music.url", j) or ""
        if title:
            lines.append(f"标题：{title}")
        if typ:
            lines.append(f"类型：{typ}")
        if an:
            lines.append(f"作者：{an}")
        if desc and str(desc) != str(title):
            lines.append(f"简介：{desc}")
        if vurl and include_video_line:
            lines.append(f"视频直链：{vurl}")
        imgs = _rel_data(data, cfg_sv.get("path_images_list") or "images", j)
        if isinstance(imgs, list) and imgs:
            lines.append(f"图集：共 {len(imgs)} 张")
        if mt or ma or mu:
            parts = [x for x in (str(mt) if mt else "", str(ma) if ma else "") if x]
            lines.append("原声：" + " / ".join(parts))
            if mu:
                lines.append(f"原声链接：{mu}")
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
            await event.send(event.plain_result(f"短视频解析失败：{e}"))
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
            lim = int(msg_cfg.get("max_images_per_work", 9) or 9)
            for x in imgs[:lim]:
                if x:
                    img_list.append(str(x).strip())

        # 批量输出控制：检查图集/实况数量是否超过阈值
        batch_cfg = _cfg(self._runtime_config(), "batch_output", default={}) or {}
        direct_json_enabled = bool(batch_cfg.get("direct_json_enabled", False))
        img_threshold = int(batch_cfg.get("image_count_threshold", 20))
        live_threshold = int(batch_cfg.get("live_photo_count_threshold", 10))
        
        if direct_json_enabled:
            live_count = len(live_vs)
            if img_total_count > img_threshold or live_count > live_threshold:
                logger.info(
                    "批量输出: 图集数量(%d)超过阈值(%d)或实况数量(%d)超过阈值(%d)，直接输出JSON",
                    img_total_count, img_threshold, live_count, live_threshold
                )
                await self._send_json_result(
                    event, j, parse_token, "批量内容超过阈值"
                )
                return

        cover = _rel_data(data, cfg_sv.get("path_cover") or "cover", j)
        cover_s = str(cover).strip() if cover else ""
        av = _rel_data(data, cfg_sv.get("path_author_avatar") or "author.avatar", j)
        av_s = str(av).strip() if av else ""

        pack_send_video = bool(msg_cfg.get("pack_send_video", True))
        pack_include_cover = bool(msg_cfg.get("pack_include_cover", True))
        text_meta = bool(msg_cfg.get("short_video_text_metadata", True))

        # 视频文件大小阈值控制
        vth = _cfg(self._runtime_config(), "video_threshold", default={}) or {}
        force_link = bool(vth.get("force_link_enabled", False))
        threshold_on = bool(vth.get("threshold_enabled", True))
        threshold_mb = int(vth.get("threshold_mb", 100) or 100)

        # 视频资源会在后续作为 Video 节点或独立直链节点加入消息，元信息中
        # 不再重复展示同一个地址。
        include_video_line = not bool(video_urls)
        text = self._format_short_video_text(
            cfg_sv,
            data,
            j,
            include_video_line=include_video_line,
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
                event, flat, j, parse_token, "短视频解析结果"
            )
            return

        for comp in chain:
            if self._is_parse_cancelled(parse_token):
                logger.info("短视频非打包发送过程中收到停止指令: %s", link)
                return
            try:
                await event.send(event.chain_result([comp]))
            except Exception as ex:
                logger.warning("发送节点失败: %s", ex)

    async def _fetch_douyin_profile(self, profile_url: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        cfg_sv = _cfg(self._runtime_config(), "short_video", default={}) or {}
        endpoint = (cfg_sv.get("endpoint") or "").strip().rstrip("?&")
        if not endpoint:
            raise ValueError("未配置短视频 endpoint")
        param = cfg_sv.get("url_param_name") or "url"
        method = (cfg_sv.get("request_method") or "GET").upper()
        if method == "POST":
            j = await self._request_json("POST", endpoint, data={param: profile_url})
        else:
            q = urlencode({param: profile_url})
            sep = "&" if "?" in endpoint else "?"
            j = await self._request_json("GET", f"{endpoint}{sep}{q}")
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
            await event.send(event.plain_result(f"抖音主页解析失败：{e}"))
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
                event, flat, j, parse_token, "抖音主页作品列表"
            )
            return

        for node in nodes:
            if self._is_parse_cancelled(parse_token):
                logger.info("抖音主页非打包发送过程中收到停止指令: %s", profile_url)
                return
            try:
                await event.send(event.chain_result([node]))
            except Exception as ex:
                logger.warning("发送节点失败: %s", ex)

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
        try:
            if method == "POST":
                j = await self._request_json("POST", ep, data=params)
            else:
                q = urlencode(params, quote_via=quote_plus)
                sep = "&" if "?" in ep else "?"
                j = await self._request_json("GET", f"{ep}{sep}{q}")
        except Exception as e:
            logger.warning("网易云搜索失败: %s", e)
            await event.send(event.plain_result(f"网易云搜索失败：{e}"))
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
            author_p = ne.get("parse_author") or "ar_name"
            cover_p = ne.get("parse_cover") or "pic"
            audio_p = ne.get("parse_audio_url") or "url"
            lyric_p = ne.get("parse_lyric") or "lyric"
            album_p = ne.get("parse_album") or "al_name"
            size_p = ne.get("parse_size") or "size"
            level_p = ne.get("parse_quality") or "level"
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
                title_p = lo.get("qishui_title_path") or "albumname"
                author_p = lo.get("qishui_author_path") or "artistsname"
                audio_p = lo.get("qishui_audio_path") or "url"
                cover_p = lo.get("qishui_cover_path") or "artistsmedium_avatar_url.0"
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
        try:
            async with aiohttp.ClientSession(timeout=to, headers=headers) as session:
                if method == "POST":
                    j = await self._session_post_form(session, endpoint, req_params)
                else:
                    q = urlencode(req_params, quote_via=quote_plus)
                    sep = "&" if "?" in endpoint else "?"
                    j = await self._session_get_json(session, f"{endpoint}{sep}{q}")

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
                            j2 = await self._session_post_form(session, endpoint, retry_params)
                        else:
                            q2 = urlencode(retry_params, quote_via=quote_plus)
                            sep2 = "&" if "?" in endpoint else "?"
                            j2 = await self._session_get_json(session, f"{endpoint}{sep2}{q2}")
                        if _code_ok(j2, path_code, ok):
                            j = j2
        except Exception as e:
            logger.warning("音乐解析失败: %s", e)
            await event.send(event.plain_result(f"音乐解析失败：{e}"))
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

        title = _rel_data(data, title_p, j) or ""
        author = _rel_data(data, author_p, j) or ""
        cover = _rel_data(data, cover_p, j) or ""
        audio = _rel_data(data, audio_p, j) or ""
        lyric = _rel_data(data, lyric_p, j) if lyric_p else ""
        album = _rel_data(data, album_p, j) if album_p else ""
        size_v = _rel_data(data, size_p, j) if size_p else ""
        level_v = _rel_data(data, level_p, j) if level_p else ""

        if dbg:
            try:
                logger.info(
                    "[BKtools][debug] netease.parse data: title=%r, audio=%r, album=%r, size=%r, level=%r, raw_data=%s",
                    title,
                    audio,
                    album,
                    size_v,
                    level_v,
                    str(data)[:dbg_len],
                )
            except Exception:
                pass

        lines = [
            f"平台：{plat}",
            f"曲名：{title}" if title else "曲名：—",
            f"艺人：{author}" if author else "",
        ]
        if album:
            lines.append(f"专辑：{album}")
        if level_v:
            lines.append(f"音质：{level_v}")
        if size_v:
            lines.append(f"大小：{size_v}")
        lines.append(f"音频：{audio}" if audio else "音频：—")
        if lyric:
            preview = str(lyric)[:800]
            if len(str(lyric)) > 800:
                preview += "…"
            lines.append("歌词预览：\n" + preview)
        text = "\n".join(x for x in lines if x)
        msg_cfg = _cfg(self._runtime_config(), "message", default={}) or {}
        
        # 尝试发送语音消息
        voice_sent = False
        if msg_cfg.get("send_voice_message") and audio:
            try:
                logger.info("尝试发送语音消息: %s", audio)
                max_size_mb = int(msg_cfg.get("voice_max_size_mb", 10))
                temp_audio = await _download_audio(str(audio), timeout_sec=self._http_cfg()[0], max_size_mb=max_size_mb)
                if temp_audio:
                    wav_path = _convert_to_wav(temp_audio, max_duration_sec=int(msg_cfg.get("voice_max_duration", 60)))
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
        if msg_cfg.get("pack_forward") and cover:
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
        await event.send(event.plain_result("【BKtools 诊断】\n" + "\n".join(checks)))

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
                "· /bk状态 — 查看当前群有效配置与任务状态（管理员）\n"
                "· /bk诊断 — 检查接口、依赖和安全防护（管理员）\n"
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
        urls = _extract_urls(text)
        if not urls:
            return
        # 拦截命令消息，避免命令处理器和自动触发重复执行
        text_stripped = text.strip()
        if text_stripped.startswith("/bk") or text_stripped.startswith("bk"):
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
