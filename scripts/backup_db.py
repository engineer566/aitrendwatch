#!/usr/bin/env python3
"""SQLite 定期备份（MVP P2，2026-09-07）。

背景：data/ 是 bind mount 单点——sponsors.db（赞助位/统计/用户事件历史）与
news.db（事件卡历史库）承载全部持久数据，容器重建/误删/磁盘故障都会丢。
本脚本用 SQLite 在线备份 API（`Connection.backup`）对每个 .db 做一致快照
（WAL 模式下安全，无需停写），gzip 压缩后落盘并做轮转保留。

用法（宿主 cron，生产目录 /opt/aitrendwatch）：
    python3 scripts/backup_db.py --data-dir data --backup-dir backups --keep 14
  cron（宿主 Asia/Shanghai，每天 04:30）：
    30 4 * * * cd /opt/aitrendwatch && /usr/bin/python3 scripts/backup_db.py \
        --data-dir data --backup-dir backups --keep 14 >> /var/log/aitw-backup.log 2>&1

说明：
- 依赖宿主 python3（stdlib sqlite3/gzip），无需 sqlite3 CLI、无需进容器；
  容器挂载的 data/ 与宿主 data/ 是同一目录，宿主直接备份即一致快照。
- 备份文件名带时间戳（UTC），按文件名字典序即时间序轮转，删最旧 N 个之外。
- 建议把 backups/ 目录另行同步到对象存储/异机（rclone/rsync + 你的存储），
  否则与 data/ 同盘仍算单点。脚本退出码：0 成功 / 1 有库失败。
"""

import argparse
import gzip
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone


def _snapshot(src, dst_tmp):
    """SQLite 在线一致快照：源库 src → 目标 dst_tmp（WAL 安全）。"""
    src_c = sqlite3.connect(src)
    try:
        dst_c = sqlite3.connect(dst_tmp)
        try:
            src_c.backup(dst_c)
        finally:
            dst_c.close()
    finally:
        src_c.close()


def _compress(dst_tmp, dst_gz):
    with open(dst_tmp, "rb") as raw, gzip.open(dst_gz, "wb", compresslevel=6) as gz:
        shutil.copyfileobj(raw, gz, length=1024 * 1024)
    os.remove(dst_tmp)


def _rotate(backup_dir, db_name, keep):
    """按文件名时间序保留最新 keep 份，删除更旧的。"""
    prefix = f"{db_name}-"
    backups = sorted(
        f for f in os.listdir(backup_dir)
        if f.startswith(prefix) and f.endswith(".db.gz"))
    for stale in backups[:-keep]:
        try:
            os.remove(os.path.join(backup_dir, stale))
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description="SQLite 在线备份 + 轮转")
    ap.add_argument("--data-dir", default="data", help="SQLite 数据目录")
    ap.add_argument("--backup-dir", default="backups", help="备份输出目录")
    ap.add_argument("--keep", type=int, default=14,
                    help="每个库保留的最新备份份数")
    ap.add_argument("--dbs", nargs="*", default=["sponsors.db", "news.db"],
                    help="要备份的库文件名（默认两者）")
    args = ap.parse_args()

    os.makedirs(args.backup_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    failed = 0
    for db_name in args.dbs:
        src = os.path.join(args.data_dir, db_name)
        if not os.path.isfile(src):
            print(f"[backup][skip] {src} 不存在，跳过", flush=True)
            continue
        try:
            dst_gz = os.path.join(
                args.backup_dir, f"{db_name}-{stamp}.db.gz")
            dst_tmp = os.path.join(args.backup_dir, f"{db_name}-{stamp}.db.tmp")
            _snapshot(src, dst_tmp)
            _compress(dst_tmp, dst_gz)
            _rotate(args.backup_dir, db_name, max(1, args.keep))
            size_kb = os.path.getsize(dst_gz) // 1024
            print(f"[backup][ok] {db_name} → {os.path.basename(dst_gz)} "
                  f"({size_kb}KB)", flush=True)
        except Exception as e:  # noqa: BLE001 —— 单库失败不中断其他库
            failed = 1
            print(f"[backup][fail] {db_name}: {e}", flush=True)
    return failed


if __name__ == "__main__":
    sys.exit(main())
