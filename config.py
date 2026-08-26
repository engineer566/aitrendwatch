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
# 站点联系/DMCA 通知邮箱（服务条款、隐私声明、版权侵权通知统一入口）。
# 未配置 → 条款页用占位文案，不影响功能。
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "").strip()

# ---------- 数据存储 ----------
# SQLite 数据库目录与路径（赞助位 + 统计）。
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
DB_PATH = os.path.join(DATA_DIR, "sponsors.db")

# GeoLite2 离线国家库（监控页 IP 地域兜底；缺失则地域为 Unknown，不影响服务）。
# 反代头（CF-IPCountry / X-Country-Code）优先，无头时才查此库。
GEOIP_DB_PATH = os.environ.get(
    "GEOIP_DB_PATH",
    os.path.join(DATA_DIR, "GeoLite2-Country.mmdb"),
)

# 向后兼容：tracker.py 仍直接读 CACHE_DIR，这里同步暴露一份。
# 容器内工作目录是 /app（挂载点 /app/cache）；本地裸跑没有 /app，
# 退化到项目根的 ./cache，避免缓存写入静默失败。
_cache_default = "/app/cache" if os.path.isdir("/app") else os.path.join(os.getcwd(), "cache")
CACHE_DIR = os.environ.get("CACHE_DIR", _cache_default)

# ---------- DeepSeek LLM（维度热词打标）----------
# 未配置 key → dims.py 的 LLM 打标降级到 RSS 源默认维度，热词仍可展示。
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")

# ---------- 分析开关 ----------
def _as_bool(v, default=True):
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")

ANALYTICS_ENABLED = _as_bool(os.environ.get("ANALYTICS_ENABLED", "true"))

# ---------- SEO ----------
# 关闭后不输出 canonical/OG/JSON-LD，robots 禁止索引，sitemap 仅含首页。
SEO_ENABLED = _as_bool(os.environ.get("SEO_ENABLED", "true"))
# sitemap.xml 最多输出的 <url> 条数（首页 + 各热词详情页）。
SITEMAP_MAX_URLS = int(os.environ.get("SITEMAP_MAX_URLS", "200") or 200)
# 单热词详情页进程内缓存 TTL（秒）。get_term_detail 是 live HF + 同步 arXiv（~1-4s）。
TERM_DETAIL_CACHE_TTL = int(os.environ.get("TERM_DETAIL_CACHE_TTL", "1800") or 1800)

# ---------- 第三方广告联盟 ----------
# 与自建赞助位并存；自建优先，联盟广告作为补位/默认填充。
# Google AdSense：未备案也能申请，审核需脚本已部署。
ADSENSE_ENABLED = _as_bool(os.environ.get("ADSENSE_ENABLED", "false"))
ADSENSE_CLIENT = os.environ.get("ADSENSE_CLIENT", "").strip()  # ca-pub-xxxxxxxxxxxxxxxx

# 百度联盟：硬门槛 ICP 备案，备案通过后申请 union.baidu.com。
BAIDU_ADS_ENABLED = _as_bool(os.environ.get("BAIDU_ADS_ENABLED", "false"))
BAIDU_ADS_CPRO_ID = os.environ.get("BAIDU_ADS_CPRO_ID", "").strip()

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
