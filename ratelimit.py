"""极简进程内固定窗口限流（MVP P1，2026-09-07）。

按 (bucket, key) 计数：窗口内超过 limit 次即拒绝（429），窗口滑过后自动恢复。
用途：/api/event（埋点上报，可被刷量撑爆 SQLite）与 /admin/login（无失败次数
限制）两个公开/半公开端点做按 IP 的简单内存计数限流——MVP 够用，无需引入
Redis/持久化（重启即清零属可接受的短暂放宽）。

设计约束：
- 纯 stdlib、无外部依赖；线程安全（进程内锁）。
- 内存有界：全表超上限后新 key 放行（fail-open），防伪造 X-Forwarded-For
  制造无限新 key 把内存打爆——限流器自身不得成为新的 DoS 面。
- 计数异常 fail-open：内部出错放行，不让限流变成可用性故障。
- 多 worker（gunicorn 2）下每 worker 各自独立计数，对单 IP 刷量已足够收敛
  （单 worker 内的超额请求同样被 429）；如需全局精确限流再上共享存储。

注意：key 由调用方传入（本项目取 _client_ip，信任自建 Nginx 注入的
X-Forwarded-For 最左一跳）。若 Nginx 未覆写 XFF，客户端可伪造头绕过单 IP
限流——部署时建议 Nginx 对上游只透传可信头（proxy_set_header X-Forwarded-For
$remote_addr 或先清空再追加），见 docs/memory 运维记录。
"""

import threading
import time

# 全表最多跟踪的 key 数（含两个 bucket）。超过后新 key fail-open，保证内存有界。
_MAX_KEYS = 4096
# 过期清理的兜底窗口（秒）：惰性 prune 只清超过该值的条目；
# 表未满时旧 key 会被同 key 新请求自然覆盖，无需精确清理。
_PRUNE_AFTER = 3600

_lock = threading.Lock()
# {(bucket, key): [window_start, count]}
_hits = {}


def _now():
    """可 mock 的时间源（测试用）。"""
    return time.monotonic()


def _prune(now):
    for ck in list(_hits):
        if now - _hits[ck][0] >= _PRUNE_AFTER:
            del _hits[ck]


def allow(bucket, key, limit, window=60):
    """请求放行判定：返回 (allowed: bool, retry_after: int)。

    - limit <= 0：不限流（恒放行）；
    - key 为空：不计数（恒放行，避免内网/无头请求被误伤）；
    - 超限返回 allowed=False + retry_after（秒，供 429 Retry-After 头）。
    """
    if limit <= 0 or not key:
        return True, 0
    now = _now()
    with _lock:
        if len(_hits) >= _MAX_KEYS:
            _prune(now)
            if len(_hits) >= _MAX_KEYS:
                # 表仍满：顺手清掉当前窗口已过期的条目（旋转），让高频 key
                # 表能轮换而不是永久占满；仍满则新 key fail-open（保护内存）。
                for ck in list(_hits):
                    if now - _hits[ck][0] >= window:
                        del _hits[ck]
        ck = (bucket, key)
        ent = _hits.get(ck)
        if ent is None:
            if len(_hits) >= _MAX_KEYS:
                return True, 0  # 表满：新 key 放行（保护内存）
            _hits[ck] = [now, 1]
            return True, 0
        start, count = ent
        if now - start >= window:
            ent[0] = now
            ent[1] = 1
            return True, 0
        if count >= limit:
            return False, max(1, int(window - (now - start)) + 1)
        ent[1] = count + 1
        return True, 0


def reset():
    """清空全部计数（测试用）。"""
    with _lock:
        _hits.clear()
