#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OSS 历史文件清理

列出 OSS Bucket 中指定前缀下超过 N 天的对象并删除。
复用 OSSUploader 进行 STS 认证，无需单独配置 ossutil。

用法：
  python3 cleanup_oss.py                  # 删除 90 天前的文件
  python3 cleanup_oss.py --days 30        # 删除 30 天前的文件
  python3 cleanup_oss.py --dry-run        # 仅列出，不删除
  python3 cleanup_oss.py -c config.ini    # 指定配置文件
"""

import argparse
import configparser
import os
from datetime import datetime, timedelta

import oss2

from oss_uploader import OSSUploader

SCRIPT_DIR  = os.path.dirname(os.path.realpath(__file__))
OSS_PREFIX  = "polardb_slowlog/"
DEFAULT_DAYS = 90


def list_expired_objects(bucket, prefix, cutoff):
    expired = []
    for obj in oss2.ObjectIterator(bucket, prefix=prefix):
        last_modified = datetime.utcfromtimestamp(obj.last_modified)
        if last_modified < cutoff:
            expired.append((obj.key, last_modified))
    return expired


def main():
    parser = argparse.ArgumentParser(description="OSS 历史慢日志文件清理")
    parser.add_argument("--days",    type=int, default=DEFAULT_DAYS,
                        help=f"清理超过多少天的文件（默认 {DEFAULT_DAYS}）")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅列出待删除文件，不执行删除")
    parser.add_argument("-c", "--config", default=None,
                        help="config.ini 路径（默认脚本同目录）")
    args = parser.parse_args()

    cfg_path = args.config or os.path.join(SCRIPT_DIR, "config.ini")
    cfg = configparser.ConfigParser()
    cfg.read(cfg_path, encoding="utf-8")

    cutoff = datetime.utcnow() - timedelta(days=args.days)
    print(f"检查时间: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"清理阈值: {args.days} 天前（早于 {cutoff.strftime('%Y-%m-%d')}）")
    print(f"OSS 前缀: {OSS_PREFIX}")
    print()

    uploader = OSSUploader(cfg)
    bucket   = uploader.bucket

    expired = list_expired_objects(bucket, OSS_PREFIX, cutoff)

    if not expired:
        print("无过期文件，无需清理。")
        return

    print(f"共找到 {len(expired)} 个过期文件：")
    for key, ts in expired:
        print(f"  {key}  ({ts.strftime('%Y-%m-%d %H:%M:%S')} UTC)")

    if args.dry_run:
        print("\n[dry-run] 未执行删除。")
        return

    print("\n开始删除...")
    deleted = 0
    for key, _ in expired:
        try:
            bucket.delete_object(key)
            print(f"  已删除: {key}")
            deleted += 1
        except Exception as e:
            print(f"  删除失败: {key}  ({e})")

    print(f"\n清理完成，共删除 {deleted} / {len(expired)} 个文件。")


if __name__ == "__main__":
    main()
