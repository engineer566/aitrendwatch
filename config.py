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
NEWS_DB_PATH = os.environ.get("NEWS_DB_PATH", os.path.join(DATA_DIR, "news.db"))
# 历史内容池最多回溯条数（issue 6：扩大内容，但上限控单次响应体积）
NEWS_HISTORY_LIMIT = int(os.environ.get("NEWS_HISTORY_LIMIT", "400") or 400)
# 历史回溯天数：仅展示近 N 天 published 的卡，过期内容不再进内容池
NEWS_HISTORY_DAYS = int(os.environ.get("NEWS_HISTORY_DAYS", "30") or 30)

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

# ---------- LLM 提供方（维度热词打标，模型故障转移链）----------
# 未配置 key → dims.py 的 LLM 打标降级到 RSS 源默认维度，热词仍可展示。
#
# LLM_CHAIN：模型故障转移链，按序尝试；每档连续 LLM_FAILOVER_THRESHOLD 次失败后
# 切下一档（单向熔断式转移，成功只清零计数、不回退首档）。首档即默认模型。
# 默认链（2026-08-30 用户指定）：
#   glm-4.7-flash（免费）→ glm-5.3-flash（2026-08 新旗舰，约 DeepSeek 1/3 价）
#     → deepseek-v4-flash（最贵，仅前两档都失败才用）
# 覆盖整条链：设 LLM_CHAIN="模型A,模型B,..."（逗号分隔；glm-* 走 GLM、deepseek-* 走 DeepSeek）。
# LLM_FAILOVER_THRESHOLD：GLM 免费档限流（1302/1305/429）会连续打掉整批翻译；
# 阈值太高（如 10）会让链在 4 个 GLM 档上烧掉 40 次失败都到不了 deepseek。
# 默认 3：短暂抖动损失一个档，持续限流则快速逃到下一个 provider，保住翻译覆盖。
LLM_CHAIN = [m.strip().lower() for m in
             os.environ.get("LLM_CHAIN",
               "glm-4.7-flash,glm-5.3-flash,deepseek-v4-flash")
             .split(",") if m.strip()]
LLM_FAILOVER_THRESHOLD = max(1, int(os.environ.get("LLM_FAILOVER_THRESHOLD", "3") or 3))
# LLM_CYCLE_ESCAPE：单次刷新周期内累计失败（不限连续）达到该值，就跳过当前
# provider 剩余档位（GLM 429 多为散落单发、从不连续，连续阈值抓不到；累计阈值
# 保证一个周期内 GLM 明显不稳时快速逃到 deepseek，翻译覆盖优先于免费额度）。
LLM_CYCLE_ESCAPE = max(1, int(os.environ.get("LLM_CYCLE_ESCAPE", "4") or 4))

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

# GLM-4.7-Flash 等：智谱 BigModel，OpenAI 兼容 chat/completions 接口。
# ⚠️ 免费档并发上限 1，高峰常返 429/1305（访问量过大）——正是故障转移链要扛的场景。
GLM_API_KEY = os.environ.get("GLM_API_KEY", "").strip()
GLM_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"


def llm_endpoint(model):
    """模型 ID → (url, api_key)：deepseek 前缀走 DeepSeek，其余（glm-* 等）走智谱 BigModel。
    供 dims.py 故障转移链取当前档的请求端点。"""
    if model.startswith("deepseek"):
        return DEEPSEEK_URL, DEEPSEEK_API_KEY
    return GLM_URL, GLM_API_KEY

# ---------- dims 生产定点预热（Asia/Shanghai 24h 制）----------
# 一天 4 次：13/19/01/07，6 小时一档。
# 选点理由：① 避开 DeepSeek 高峰段（工作日 9-12 / 14-18），多落空闲档（半价）；
# ② 6 小时一档压在 DeepSeek 硬盘缓存 TTL（几小时到几天）内，规则前缀跨次复用
#   可命中缓存（命中价是未命中的 1/30）。
DIMS_REFRESH_HOURS = tuple(
    int(h) for h in os.environ.get("DIMS_REFRESH_HOURS", "1,7,13,19").split(",")
)

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
