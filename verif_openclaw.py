import json, sqlite3

conn = sqlite3.connect("/app/data/news.db")
rows = {}
for r in conn.execute("SELECT term, cycle, news_cnt, score_sum FROM term_snapshots ORDER BY cycle"):
    rows.setdefault(r[0], []).append(r)
all_cycles = sorted({r[1] for rs in rows.values() for r in rs})
cur_cycle = all_cycles[-1]
print("cur cycle:", cur_cycle)

with open("cache/words.json", encoding="utf-8") as f:
    d = json.load(f)
terms = d["words"]["data"]["terms"]

# cnt-only rise (news_cnt growth, decay-free)
cnt_rise = {}
for t in terms:
    cid = t.get("id")
    snaps = [r for r in rows.get(cid, []) if r[1] != cur_cycle]
    cur = [r for r in rows.get(cid, []) if r[1] == cur_cycle]
    if not snaps:
        cnt_rise[cid] = None  # cold start -> ln(1+m) style high
        continue
    m_cur = cur[0][2] if cur else 0
    m_prev = snaps[-1][2]
    cnt_rise[cid] = (m_cur - m_prev) / max(m_prev, 0.5) if m_prev else 0.0

# cold start words get a high positive value (they're new & active)
for cid, v in cnt_rise.items():
    if v is None:
        cnt_rise[cid] = 5.0  # placeholder "new word" boost, matches ln(1+m) cap spirit

ranked = sorted(cnt_rise.items(), key=lambda kv: (-kv[1], kv[0]))
oc_rank = next((i + 1 for i, (c, _) in enumerate(ranked) if c == "openclaw"), None)
print("openclaw cnt-only rise:", cnt_rise.get("openclaw"))
print("openclaw rank by cnt-only rise:", oc_rank, "of", len(ranked))
# what does the API-style tie-break (hot desc) give?
def _hot(cid):
    return next((t.get("hot") or 0) for t in terms if t.get("id") == cid)
ranked2 = sorted(cnt_rise.items(), key=lambda kv: (-kv[1], -_hot(kv[0]), kv[0]))
oc2 = next((i + 1 for i, (c, _) in enumerate(ranked2) if c == "openclaw"), None)
print("openclaw rank with hot tie-break:", oc2)
# distribution of cnt-only rise
from collections import Counter
c = Counter(round(v, 3) for v in cnt_rise.values())
print("cnt-rise distribution:", c.most_common(10))
conn.close()
