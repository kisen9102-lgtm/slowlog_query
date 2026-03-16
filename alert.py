#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实时慢 SQL 告警

查询 PolarDB PROCESSLIST，对正在执行的慢 SQL 按阈值触发 Lark 告警：
  - 出现 1 条 >= 180 秒的 SQL → 立即告警
  - 出现 3 条及以上 >= 60 秒的 SQL → 立即告警

用法：
  python3 alert.py [-c config.ini路径]
"""

import sys
import argparse
import configparser
import os
import requests
import pymysql
import pymysql.cursors
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))

LONG_THRESHOLD  = 180   # 秒，超过此值视为严重
MID_THRESHOLD   = 60    # 秒，超过此值视为中级
MID_COUNT_LIMIT = 3     # 中级 SQL 数量达到此值时触发告警


def load_config(path=None):
    if path is None:
        path = os.path.join(SCRIPT_DIR, "config.ini")
    cfg = configparser.ConfigParser()
    cfg.read(path, encoding="utf-8")
    return cfg


def query_processlist(cfg):
    conn = pymysql.connect(
        host=cfg.get("polardb", "db_host"),
        port=cfg.getint("polardb", "db_port"),
        user=cfg.get("polardb", "db_user"),
        password=cfg.get("polardb", "db_pass"),
        connect_timeout=10,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ID, TIME, USER, HOST, DB, INFO
                FROM information_schema.PROCESSLIST
                WHERE COMMAND = 'Query'
                  AND INFO IS NOT NULL
                  AND TIME >= %s
            """, (MID_THRESHOLD,))
            return cur.fetchall()
    finally:
        conn.close()


def send_lark(webhook, text):
    payload = {"msg_type": "text", "content": {"text": text}}
    try:
        r = requests.post(webhook, json=payload, timeout=6)
        r.raise_for_status()
        print("Lark 推送成功")
    except Exception as e:
        print(f"Lark 推送失败: {e}")


def format_row(label, r):
    return (
        f"【{label}】\n"
        f"ID: {r['ID']}\n"
        f"HOST: {r['USER']}@{r['HOST']}\n"
        f"Time: {r['TIME']}s\n"
        f"DB: {r['DB']}\n"
        f"SQL: {r['INFO']}\n"
    )


def main():
    parser = argparse.ArgumentParser(description="PolarDB 实时慢 SQL 告警")
    parser.add_argument("-c", "--config", default=None, help="config.ini 路径")
    args = parser.parse_args()

    cfg     = load_config(args.config)
    webhook = cfg.get("polardb", "lark_webhook")
    db_host = cfg.get("polardb", "db_host")
    db_port = cfg.getint("polardb", "db_port")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        rows = query_processlist(cfg)
    except Exception as e:
        print(f"[{now}] 查询 PROCESSLIST 失败: {e}", file=sys.stderr)
        sys.exit(1)

    if not rows:
        print(f"[{now}] No SQL over {MID_THRESHOLD} seconds.")
        return

    long_sqls = [r for r in rows if r["TIME"] >= LONG_THRESHOLD]
    mid_sqls  = [r for r in rows if r["TIME"] <  LONG_THRESHOLD]

    should_alert = len(long_sqls) >= 1 or len(mid_sqls) >= MID_COUNT_LIMIT
    if not should_alert:
        print(f"[{now}] No alert triggered. "
              f"(long={len(long_sqls)}, mid={len(mid_sqls)})")
        return

    body = "\n".join(
        [format_row(f"超过 {LONG_THRESHOLD} 秒 SQL", r) for r in long_sqls]
        + [format_row(f"超过 {MID_THRESHOLD} 秒 SQL",  r) for r in mid_sqls]
    )

    text = (
        f"【PolarDB 上正在运行的慢 SQL】\n"
        f"实例：{db_host}:{db_port}\n"
        f"超过 {LONG_THRESHOLD} 秒：{len(long_sqls)} 个\n"
        f"超过 {MID_THRESHOLD} 秒：{len(mid_sqls)} 个\n\n"
        f"{body}"
    )

    send_lark(webhook, text)
    print(f"[{now}] Alert sent.")


if __name__ == "__main__":
    main()
