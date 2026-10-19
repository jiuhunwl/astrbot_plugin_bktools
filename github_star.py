"""GitHub Star 校验（活动链路第一步校验）。

流程（对齐 design-plugin-campaign.md 第三节）：
1. ``GET https://api.github.com/users/{login}`` — 200=用户存在，404=不存在；
2. ``GET https://api.github.com/repos/{owner}/{repo}/stargazers/{login}`` —
   204=已 star，404=未 star。

可选 token 提额（匿名 60/h → 认证 5000/h）。结果枚举：
OK / NOT_STARRED / USER_NOT_FOUND / ERROR。

GitHub token 不明文落日志。本模块不依赖 astrbot，可独立单测。
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Optional

import aiohttp

from survey_adapter import GITHUB_LOGIN_RE

logger = logging.getLogger("bktools.github")


class StarCheckResult(str, Enum):
    OK = "ok"
    NOT_STARRED = "not_starred"
    USER_NOT_FOUND = "user_not_found"
    ERROR = "error"


class GitHubStarVerifier:
    """GitHub star 状态校验器。

    ``token`` 可选：配置后请求带 ``Authorization: Bearer`` 提额，
    永不打印 token 本身。
    """

    API_BASE = "https://api.github.com"

    def __init__(
        self,
        token: str = "",
        *,
        timeout_sec: int = 15,
        user_agent: str = "astrbot-plugin-bktools/1.7.0 (BKtools star verify)",
    ) -> None:
        self._token = str(token or "").strip()
        self._timeout_sec = max(5, int(timeout_sec))
        self._user_agent = user_agent

    def _headers(self) -> dict:
        headers = {
            "User-Agent": self._user_agent,
            "Accept": "application/vnd.github+json",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def verify(
        self,
        login: str,
        owner: str,
        repo: str,
        *,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> StarCheckResult:
        """校验 login 是否已 star owner/repo。session 可注入供测试。"""
        login = str(login or "").strip()
        owner = str(owner or "").strip()
        repo = str(repo or "").strip()
        if not (login and owner and repo):
            return StarCheckResult.ERROR
        if not GITHUB_LOGIN_RE.match(login) or not GITHUB_LOGIN_RE.match(owner):
            return StarCheckResult.USER_NOT_FOUND if login else StarCheckResult.ERROR

        own_session = session is None
        try:
            if own_session:
                session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self._timeout_sec)
                )
            assert session is not None
            # 第一步：用户存在性（404 = 用户不存在）
            user_status = await self._get_status(session, f"/users/{login}")
            if user_status == 404:
                return StarCheckResult.USER_NOT_FOUND
            if user_status != 200:
                logger.warning("GitHub 用户查询异常: HTTP %s（login 已脱敏）", user_status)
                return StarCheckResult.ERROR
            # 第二步：star 状态（204 = 已 star，404 = 未 star）
            star_status = await self._get_status(
                session, f"/repos/{owner}/{repo}/stargazers/{login}"
            )
            if star_status == 204:
                return StarCheckResult.OK
            if star_status == 404:
                return StarCheckResult.NOT_STARRED
            logger.warning("GitHub star 查询异常: HTTP %s", star_status)
            return StarCheckResult.ERROR
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as ex:
            logger.warning("GitHub 校验网络异常: %s", type(ex).__name__)
            return StarCheckResult.ERROR
        finally:
            if own_session and session is not None and not session.closed:
                await session.close()

    async def _get_status(
        self, session: aiohttp.ClientSession, path: str
    ) -> int:
        url = self.API_BASE + path
        async with session.get(url, headers=self._headers()) as response:
            # 读取并丢弃响应体，避免连接复用警告；不记录任何响应内容
            await response.read()
            return int(response.status)
