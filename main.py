"""
AstrBot 插件：BugPk 工具集（短视频解析、网易云搜歌、多平台音乐链解析）。
说明：音乐搜索仅走网易云配置；QQ/汽水/酷我等仅支持分享链接解析。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urlencode

import aiohttp

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Image, Node, Nodes, Plain, Video
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.event_message_type import EventMessageType


def _cfg(root: Any, *keys: str, default: Any = None) -> Any:
    if not isinstance(root, dict):
        return default
    cur: Any = root
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


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


def _is_pure_image_gallery_nodes(nodes: List[Any]) -> bool:
    has_v = any(isinstance(n, Video) for n in nodes)
    has_i = any(isinstance(n, Image) for n in nodes)
    return has_i and not has_v


def _chain_to_forward_nodes(
    chain: List[Any],
    sender_name: str,
    sender_id: Any,
    *,
    batch_pure_image_gallery: bool,
) -> List[Node]:
    if not chain:
        return []
    if batch_pure_image_gallery and _is_pure_image_gallery_nodes(chain):
        texts = [n for n in chain if isinstance(n, Plain)]
        images = [n for n in chain if isinstance(n, Image)]
        flat: List[Node] = []
        for t in texts:
            flat.append(Node(name=sender_name, uin=sender_id, content=[t]))
        if images:
            flat.append(Node(name=sender_name, uin=sender_id, content=images))
        return flat
    flat: List[Node] = []
    for n in chain:
        if n is None:
            continue
        flat.append(Node(name=sender_name, uin=sender_id, content=[n]))
    return flat


def _extract_urls(text: str) -> List[str]:
    return re.findall(r"https?://[^\s\]>\"']+", text or "")


def _music_platform(url: str) -> Optional[str]:
    u = (url or "").lower()
    if "music.163.com" in u or "163cn.tv" in u:
        return "netease"
    if "y.qq.com" in u or "qq.com/n/ryqq" in u:
        return "qq"
    if "qishui.douyin.com" in u:
        return "qishui"
    if "kuwo.cn" in u:
        return "kuwo"
    return None


def _looks_like_qishui_share_context(text: str, url: str) -> bool:
    """汽水分享常见于文案含“汽水”+ 短链（v.douyin.com）。"""
    t = (text or "").lower()
    u = (url or "").lower()
    if "qishui.douyin.com" in u:
        return True
    if "汽水" in t and ("douyin.com" in u or "iesdouyin.com" in u):
        return True
    return False


def _extract_netease_song_id(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    m = re.search(r"(?:id|ids)=([0-9]+)", s, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"\b([0-9]{5,})\b", s)
    return m.group(1) if m else ""


def _video_auto_match(url: str) -> bool:
    u = (url or "").lower()
    for h in (
        "douyin.com",
        "iesdouyin.com",
        "kuaishou.com",
        "xiaohongshu.com",
        "xhslink.com",
        "bilibili.com",
        "b23.tv",
        "weibo.com",
        "weibo.cn",
    ):
        if h in u:
            return True
    return False


@register(
    "astrbot_plugin_bktools",
    "jiuhunwl",
    "BugPk 工具：短视频解析、网易云搜歌、QQ/汽水/酷我链解析",
    "1.1.0",
    icon="https://static.esa.ifphp.com/img/bugpk-Api-256×256.ico"
)
class BKToolsPlugin(Star):
    def __init__(self, context: Context, config: Optional[Dict[str, Any]] = None):
        super().__init__(context)
        self.config: Dict[str, Any] = config if isinstance(config, dict) else {}
        self._netease_pick_cache: Dict[str, Dict[str, Any]] = {}

    def _debug_cfg(self) -> Tuple[bool, int]:
        d = _cfg(self.config, "debug", default={}) or {}
        return bool(d.get("enable", False)), int(d.get("max_chars", 800) or 800)

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
        h = _cfg(self.config, "http", default={}) or {}
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
        msg = _cfg(self.config, "message", default={}) or {}
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
        async with session.get(url) as resp:
            raw = await resp.read()
            return self._loads_response_json(raw)

    async def _session_post_form(
        self, session: aiohttp.ClientSession, url: str, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        async with session.post(url, data=data) as resp:
            raw = await resp.read()
            return self._loads_response_json(raw)

    async def _expand_netease_short_link(
        self, session: aiohttp.ClientSession, link: str
    ) -> str:
        """展开 163cn.tv 等短链，便于提取歌曲 id。失败时返回原链接。"""
        u = (link or "").strip()
        if not u:
            return u
        low = u.lower()
        if "163cn.tv" not in low and "y.music.163.com" not in low:
            return u
        try:
            async with session.head(u, allow_redirects=True) as resp:
                final_u = str(resp.url)
                if final_u:
                    return final_u
        except Exception:
            pass
        try:
            async with session.get(u, allow_redirects=True) as resp:
                final_u = str(resp.url)
                if final_u:
                    return final_u
        except Exception:
            pass
        return u

    async def _resolve_final_url(
        self, session: aiohttp.ClientSession, link: str
    ) -> str:
        """通用重定向展开（用于汽水等分享短链识别）。"""
        u = (link or "").strip()
        if not u:
            return u
        try:
            async with session.head(u, allow_redirects=True) as resp:
                final_u = str(resp.url)
                if final_u:
                    return final_u
        except Exception:
            pass
        try:
            async with session.get(u, allow_redirects=True) as resp:
                final_u = str(resp.url)
                if final_u:
                    return final_u
        except Exception:
            pass
        return u

    async def _fetch_short_video(self, link: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        cfg_sv = _cfg(self.config, "short_video", default={}) or {}
        endpoint = (cfg_sv.get("endpoint") or "").strip().rstrip("?&")
        if not endpoint:
            raise ValueError("未配置短视频 endpoint")
        param = cfg_sv.get("url_param_name") or "url"
        method = (cfg_sv.get("request_method") or "GET").upper()
        timeout_sec, ua = self._http_cfg()
        headers = {"User-Agent": ua}
        to = aiohttp.ClientTimeout(total=timeout_sec)
        async with aiohttp.ClientSession(timeout=to, headers=headers) as session:
            if method == "POST":
                j = await self._session_post_form(session, endpoint, {param: link})
            else:
                q = urlencode({param: link})
                sep = "&" if "?" in endpoint else "?"
                j = await self._session_get_json(session, f"{endpoint}{sep}{q}")
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
        original_link: str = "",
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
        if original_link:
            lines.append(f"原始链接：{original_link}")
        return "\n".join(lines) if lines else "（无文本信息）"

    async def _reply_short_video(self, event: AstrMessageEvent, link: str) -> None:
        await self._maybe_opening(event)
        try:
            j, data = await self._fetch_short_video(link)
        except Exception as e:
            logger.warning("短视频解析失败: %s", e)
            await event.send(event.plain_result(f"短视频解析失败：{e}"))
            return
        cfg_sv = _cfg(self.config, "short_video", default={}) or {}
        msg_cfg = _cfg(self.config, "message", default={}) or {}

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
        if isinstance(imgs, list):
            lim = int(msg_cfg.get("max_images_per_work", 9) or 9)
            for x in imgs[:lim]:
                if x:
                    img_list.append(str(x).strip())

        cover = _rel_data(data, cfg_sv.get("path_cover") or "cover", j)
        cover_s = str(cover).strip() if cover else ""
        av = _rel_data(data, cfg_sv.get("path_author_avatar") or "author.avatar", j)
        av_s = str(av).strip() if av else ""

        pack_send_video = bool(msg_cfg.get("pack_send_video", True))
        pack_include_cover = bool(msg_cfg.get("pack_include_cover", True))
        text_meta = bool(msg_cfg.get("short_video_text_metadata", True))
        append_orig = bool(msg_cfg.get("pack_append_original_link", True))

        include_video_line = True
        if video_urls and pack_send_video:
            include_video_line = False
        orig = link if append_orig else ""
        text = self._format_short_video_text(
            cfg_sv,
            data,
            j,
            include_video_line=include_video_line,
            original_link=orig,
        )

        chain: List[Any] = []
        if text_meta and text.strip():
            chain.append(Plain(text))
        elif not text_meta and not (video_urls or img_list or cover_s):
            chain.append(Plain("解析完成"))

        if video_urls:
            if pack_include_cover and cover_s:
                ci = _make_image_node(cover_s)
                if ci:
                    chain.append(ci)
            for vu in video_urls:
                if pack_send_video:
                    vn = _make_video_node(vu)
                    chain.append(vn if vn is not None else Plain(f"视频：{vu}"))
                else:
                    chain.append(Plain(f"视频：{vu}"))
        else:
            for u in img_list:
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
        batch_gallery = _is_pure_image_gallery_nodes(chain) and not av_s

        if pack:
            flat = _chain_to_forward_nodes(
                chain,
                name,
                uid,
                batch_pure_image_gallery=batch_gallery,
            )
            if flat:
                await event.send(event.chain_result([Nodes(flat)]))
        else:
            for comp in chain:
                try:
                    await event.send(event.chain_result([comp]))
                except Exception as ex:
                    logger.warning("发送节点失败: %s", ex)

    async def _netease_search(self, event: AstrMessageEvent, keyword: str) -> None:
        ne = _cfg(self.config, "netease", default={}) or {}
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
        timeout_sec, ua = self._http_cfg()
        headers = {"User-Agent": ua}
        to = aiohttp.ClientTimeout(total=timeout_sec)
        try:
            async with aiohttp.ClientSession(timeout=to, headers=headers) as session:
                if method == "POST":
                    j = await self._session_post_form(session, ep, params)
                else:
                    q = urlencode(params, quote_via=quote_plus)
                    sep = "&" if "?" in ep else "?"
                    j = await self._session_get_json(session, f"{ep}{sep}{q}")
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
        lim = int((_cfg(self.config, "message", default={}) or {}).get("search_result_limit", 8))
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

    async def _music_link_parse(self, event: AstrMessageEvent, link: str) -> None:
        plat = _music_platform(link)
        ne = _cfg(self.config, "netease", default={}) or {}
        lo = _cfg(self.config, "link_only_music", default={}) or {}
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
                # 未识别平台时尝试展开短链（如 v.douyin.com -> qishui.douyin.com）
                if not plat:
                    resolved = await self._resolve_final_url(session, req_link)
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
            await event.send(event.plain_result("无法识别音乐平台链接。"))
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
        msg_cfg = _cfg(self.config, "message", default={}) or {}
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

    @filter.command("bk帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """BKtools 命令说明"""
        await event.send(
            event.plain_result(
                "【BKtools】\n"
                "· /bk视频 <链接> — 短视频解析\n"
                "· /bk网易云 <关键词> — 仅网易云搜索（需配置搜索接口）\n"
                "· /bk点歌 <序号> — 选择最近一次网易云搜索结果\n"
                "· /bk音乐 <音乐分享链接> — QQ/汽水/酷我/网易等链接解析（无搜索）\n"
                "短视频：默认合并转发（Nodes），纯图集会合并图片节点，"
                "含视频时用 Video.fromURL，详见 message / short_video 配置项。\n"
                "自动短视频：可在配置中开启 trigger.auto_short_video"
            )
        )

    @filter.command("bk视频")
    async def cmd_video_slash(self, event: AstrMessageEvent):
        """短视频解析"""
        arg = self._cmd_arg(event, "bk视频")
        if not arg:
            await event.send(event.plain_result("用法：/bk视频 <作品链接>"))
            return
        await self._reply_short_video(event, arg)

    @filter.command("bktv")
    async def cmd_video_alias(self, event: AstrMessageEvent):
        """短视频解析（简写）"""
        arg = self._cmd_arg(event, "bktv")
        if not arg:
            await event.send(event.plain_result("用法：/bktv <作品链接>"))
            return
        await self._reply_short_video(event, arg)

    @filter.command("bk网易云")
    async def cmd_netease(self, event: AstrMessageEvent):
        """网易云音乐搜索（仅此平台支持关键词搜索）"""
        arg = self._cmd_arg(event, "bk网易云")
        if not arg:
            await event.send(event.plain_result("用法：/bk网易云 <关键词>"))
            return
        await self._netease_search(event, arg)

    @filter.command("bk搜歌")
    async def cmd_netease_alias(self, event: AstrMessageEvent):
        """同 bk网易云"""
        arg = self._cmd_arg(event, "bk搜歌")
        if not arg:
            await event.send(event.plain_result("用法：/bk搜歌 <关键词>（仅网易云）"))
            return
        await self._netease_search(event, arg)

    @filter.command("bk点歌")
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

    @filter.event_message_type(EventMessageType.ALL)
    async def on_auto(self, event: AstrMessageEvent):
        """自动触发短视频 / 音乐链"""
        tr = _cfg(self.config, "trigger", default={}) or {}
        text = event.message_str or ""
        pure = text.strip()
        if pure.isdigit():
            picked = await self._netease_pick_by_index(event, int(pure))
            if picked:
                return
        urls = _extract_urls(text)
        if not urls:
            return
        # 避免与显式指令冲突（简单判断）
        if text.strip().startswith("/bk"):
            return

        # 优先识别音乐链接，避免“汽水分享短链（douyin）”误判为短视频。
        if tr.get("auto_music_link"):
            for u in urls:
                if _music_platform(u) or _looks_like_qishui_share_context(text, u):
                    await self._music_link_parse(event, u)
                    return

        if tr.get("auto_short_video"):
            for u in urls:
                if _video_auto_match(u):
                    await self._reply_short_video(event, u)
                    return
