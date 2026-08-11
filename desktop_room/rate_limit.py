"""登录限流：防止 Argon2id 被恶意请求拖垮。

规则：
- 单 IP 每分钟最多 5 次失败
- 单房间每分钟最多 30 次失败
- 连续失败指数退避（客户端可在 Retry-After 感知）
- 全局并发密码验证数量限制（信号量）
- 房主界面可读取累计失败次数（failed_login_attempts）
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import defaultdict, deque
from typing import DefaultDict


class LoginRateLimiter:
    """进程内限流器。单房间网关单实例；键为 str（IP 或 room:IP 组合）。"""

    FAIL_PER_IP_PER_MIN = 5
    FAIL_PER_ROOM_PER_MIN = 30
    BACKOFF_BASE_SECONDS = 2.0
    BACKOFF_MAX_SECONDS = 60.0
    GLOBAL_CONCURRENCY = 2

    def __init__(self) -> None:
        # 计数窗口：最近 60 秒的失败时刻
        self._ip_failures: DefaultDict[str, deque[float]] = defaultdict(deque)
        self._room_failures: DefaultDict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._semaphore = asyncio.Semaphore(self.GLOBAL_CONCURRENCY)
        # 房主界面显示：每个房间累计失败次数
        self._room_failed_counts: DefaultDict[str, int] = defaultdict(int)

    # ------------------------------------------------------------------
    # 准入检查（在调用 Argon2id 之前）
    # ------------------------------------------------------------------
    def ip_allows(self, ip: str) -> bool:
        with self._lock:
            self._prune(self._ip_failures[ip], 60.0)
            return len(self._ip_failures[ip]) < self.FAIL_PER_IP_PER_MIN

    def room_allows(self, room_public_id: str, ip: str) -> bool:
        key = f"{room_public_id}:{ip}"
        with self._lock:
            self._prune(self._room_failures[key], 60.0)
            return len(self._room_failures[key]) < self.FAIL_PER_ROOM_PER_MIN

    def backoff_seconds(self, ip: str) -> float:
        """连续失败后的指数退避秒数。"""
        with self._lock:
            n = max(0, len(self._ip_failures[ip]) - 1)
        seconds = self.BACKOFF_BASE_SECONDS * (2 ** n)
        return min(seconds, self.BACKOFF_MAX_SECONDS)

    # ------------------------------------------------------------------
    # 失败 / 成功记录
    # ------------------------------------------------------------------
    def record_failure(self, ip: str, room_public_id: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._ip_failures[ip].append(now)
            self._room_failures[f"{room_public_id}:{ip}"].append(now)
            self._room_failed_counts[room_public_id] += 1

    def record_success(self, ip: str, room_public_id: str) -> None:
        with self._lock:
            self._ip_failures.pop(ip, None)
            self._room_failures.pop(f"{room_public_id}:{ip}", None)

    def room_failed_count(self, room_public_id: str) -> int:
        with self._lock:
            return self._room_failed_counts.get(room_public_id, 0)

    def reset_room(self, room_public_id: str) -> None:
        with self._lock:
            self._room_failed_counts.pop(room_public_id, None)

    # ------------------------------------------------------------------
    # 全局并发
    # ------------------------------------------------------------------
    def concurrent_guard(self) -> asyncio.Semaphore:
        return self._semaphore

    @staticmethod
    def _prune(queue: deque[float], window_seconds: float) -> None:
        cutoff = time.monotonic() - window_seconds
        while queue and queue[0] <= cutoff:
            queue.popleft()
