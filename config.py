"""
集中配置层 —— 所有可调参数从环境变量读，带安全默认值。

设计原则（与 tracker.py 一致）：
- 零外部依赖（纯 stdlib）。
- 向后兼容：CACHE_DIR 仍按原方式读，tracker.py 不强制改。
- 生产必设：SECRET_KEY、ADMIN_TOKEN（未设则管理后台隐身 404）。
"""

import os

# ---------- Flask 会话签名 ----------
# 生产必须显式设置，否则每次进程重启 session 失效。
# 默认用一个进程级随机值，仅适合本地开发。
SECRET_KEY = os.environ.get("SECRET_KEY", os.urandom(32).hex())

# ---------- 管理后台令牌 ----------
# 未设置 → 所有 /admin/* 路由返回 404（隐身，不只是锁）。
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()

# ---------- 站点信息 ----------
SITE_NAME = os.environ.get("SITE_NAME", "ModelRadar")
BASE_URL = os.environ.get("BASE_URL", "").strip()  # 如 https://modelradar.ai

# ---------- 数据存储 ----------
# SQLite 数据库目录与路径（赞助位 + 统计）。
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
DB_PATH = os.path.join(DATA_DIR, "sponsors.db")

# 向后兼容：tracker.py 仍直接读 CACHE_DIR，这里同步暴露一份。
CACHE_DIR = os.environ.get("CACHE_DIR", "/app/cache")

# ---------- 分析开关 ----------
def _as_bool(v, default=True):
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")

ANALYTICS_ENABLED = _as_bool(os.environ.get("ANALYTICS_ENABLED", "true"))

# ---------- 赞助位展示 ----------
# 热词卡之间每 N 张插一张 inline 赞助卡。
INLINE_SLOT_EVERY_N = int(os.environ.get("INLINE_SLOT_EVERY_N", "8") or 8)

# ---------- 数据目录就绪 ----------
def ensure_data_dir():
    """确保 DATA_DIR 存在且可写。失败不抛——store.py 会降级。"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except OSError:
        pass
