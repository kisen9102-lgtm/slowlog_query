#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PolarDB 慢查询监控主程序

流程：
  1. 通过阿里云 PolarDB V2 API 拉取指定时间段的慢查询记录
  2. 保存原始 JSON 到本地
  3. 用 PolarDBSlowLogAnalyzer 生成 HTML 报告（含 EXPLAIN）
  4. 将 JSON 和 HTML 上传到 OSS（KMS 加密，STS 临时凭证）
  5. 推送 Lark 富文本卡片通知（含 Top1 SQL 摘要和报告链接）

用法：
  python3 main.py [-m 分钟数] [-c config.ini路径]
"""

import json
import argparse
import configparser
import os
import requests
from datetime import datetime, timedelta

from alibabacloud_sts20150401.client import Client as StsClient
from alibabacloud_sts20150401 import models as sts_models
from alibabacloud_polardb20170801.client import Client as PolardbClient
from alibabacloud_polardb20170801 import models as polardb_models
from alibabacloud_tea_openapi import models as open_api_models

from analyzer import PolarDBSlowLogAnalyzer
from oss_uploader import OSSUploader

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))


# ================================================================
# 配置加载
# ================================================================
def load_config(path=None):
    if path is None:
        path = os.path.join(SCRIPT_DIR, "config.ini")
    elif not os.path.isabs(path):
        path = os.path.join(SCRIPT_DIR, path)

    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到配置文件: {path}")

    cfg = configparser.ConfigParser()
    cfg.read(path, encoding="utf-8")
    return cfg, path


# ================================================================
# PolarDB 客户端（通过 STS 临时凭证）
# ================================================================
def get_sts_credentials(cfg):
    region = cfg["polardb"]["region"]
    api_cfg = open_api_models.Config(
        access_key_id=cfg["oss"]["access_key"],
        access_key_secret=cfg["oss"]["access_secret"],
        endpoint=f"sts-vpc.{region}.aliyuncs.com",
    )
    client  = StsClient(api_cfg)
    request = sts_models.AssumeRoleRequest(
        role_arn=cfg["oss"]["role_arn"],
        role_session_name=cfg["oss"]["role_session_name"],
    )
    return client.assume_role(request).body.credentials


def create_polardb_client(cfg, creds):
    region = cfg["polardb"]["region"]
    po_cfg = open_api_models.Config(
        access_key_id=creds.access_key_id,
        access_key_secret=creds.access_key_secret,
        security_token=creds.security_token,
        region_id=region,
        endpoint=f"polardb-vpc.{region}.aliyuncs.com",
    )
    return PolardbClient(po_cfg)


# ================================================================
# 拉取慢日志
# ================================================================
def fetch_slow_logs(client, cluster_id, minutes):
    end   = datetime.utcnow()
    start = end - timedelta(minutes=minutes)

    req = polardb_models.DescribeSlowLogRecordsRequest(
        dbcluster_id=cluster_id,
        start_time=start.strftime("%Y-%m-%dT%H:%MZ"),
        end_time=end.strftime("%Y-%m-%dT%H:%MZ"),
        page_size=100,
    )
    print(f"拉取慢查询: {start.strftime('%Y-%m-%d %H:%M:%S')} → {end.strftime('%Y-%m-%d %H:%M:%S')} UTC")

    try:
        resp    = client.describe_slow_log_records(req)
        records = []
        if resp.body.items and resp.body.items.sqlslow_record:
            records = [item.to_map() for item in resp.body.items.sqlslow_record]
        return records, start, end
    except Exception as e:
        print(f"[ERROR] 获取 PolarDB 慢日志失败: {e}")
        raise


# ================================================================
# 保存 JSON
# ================================================================
def save_json(output_dir, records, start, end):
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"slowlog_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"start": start.isoformat(), "end": end.isoformat(),
             "count": len(records), "records": records},
            f, ensure_ascii=False, indent=2,
        )
    print(f"JSON 已保存: {path}")
    return path


# ================================================================
# 生成 HTML 报告
# ================================================================
def generate_report(json_path, output_dir, cfg_path):
    analyzer  = PolarDBSlowLogAnalyzer(cfg_path)
    analyzer.load_json_files(json_path)
    html_path = os.path.join(output_dir, f"PolarDB_Report_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.html")
    analyzer.save_html(html_path)
    return html_path, analyzer.get_top1_summary()


# ================================================================
# 推送 Lark 富文本卡片
# ================================================================
def send_lark_card(cfg, cluster_name, time_range_str, html_url, json_url, record_count, top1):
    if record_count == 0 or not top1:
        print(f"[INFO] 无慢查询记录，跳过 Lark 推送。")
        return

    webhook = cfg["polardb"]["lark_webhook"]

    top_md = (
        f"**IP**：{top1.get('client', '-')}\n"
        f"**执行次数**：{top1.get('count', 0)}　**总耗时**：{top1.get('total_time', 0):.2f}s\n"
        f"**平均 / 最大 / 最小**："
        f"{top1.get('avg_time', 0):.3f}s / {top1.get('max_time', 0):.3f}s / {top1.get('min_time', 0):.3f}s\n"
        f"**P50 / P95**：{top1.get('p50', 0):.3f}s / {top1.get('p95', 0):.3f}s\n"
        f"**样例 SQL**：{top1.get('sample_sql', '') or '-'}\n"
    )

    md = (
        f"### 🐌 PolarDB 慢查询报告\n\n"
        f"- **Cluster**: `{cluster_name}`\n"
        f"- **时间范围**: {time_range_str}\n"
        f"- **慢查询总数**: {record_count}\n\n"
        f"{top_md}"
    )

    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "PolarDB 慢查询报告"},
                "template": "blue",
            },
            "elements": [
                {"tag": "markdown", "content": md},
                {
                    "tag": "action",
                    "actions": [
                        {"tag": "button", "text": {"tag": "plain_text", "content": "查看 HTML 报告"},
                         "type": "primary", "url": html_url},
                        {"tag": "button", "text": {"tag": "plain_text", "content": "下载原始 JSON"},
                         "type": "default", "url": json_url},
                    ],
                },
            ],
        },
    }

    try:
        r = requests.post(webhook, json=card, timeout=10)
        r.raise_for_status()
        print("Lark 推送成功")
    except Exception as e:
        print(f"Lark 推送失败: {e}")
        try:
            fallback = (f"PolarDB 慢查询报告\nCluster: {cluster_name}\n"
                        f"Range: {time_range_str}\nRecords: {record_count}\n"
                        f"HTML: {html_url}\nJSON: {json_url}")
            requests.post(webhook, json={"msg_type": "text", "content": {"text": fallback}}, timeout=6)
        except Exception as e2:
            print(f"Fallback 推送也失败: {e2}")


# ================================================================
# 主程序
# ================================================================
def main():
    parser = argparse.ArgumentParser(description="PolarDB 慢查询监控")
    parser.add_argument("-m", "--minutes", type=int, default=30, help="拉取最近 N 分钟的慢日志（默认 30）")
    parser.add_argument("-c", "--config",  type=str, default=None, help="config.ini 路径（默认脚本同目录）")
    args = parser.parse_args()

    print("=" * 70)
    print(f"[{datetime.utcnow()}] PolarDB 慢查询监控（最近 {args.minutes} 分钟）")
    print("=" * 70)

    cfg, cfg_path = load_config(args.config)
    print(f"使用配置文件: {cfg_path}")

    cluster_id   = cfg["polardb"]["db_cluster_id"]
    output_dir   = cfg["polardb"]["output_dir"]
    cluster_name = cfg["polardb"].get("cluster_name", cluster_id)

    # 获取慢日志
    creds     = get_sts_credentials(cfg)
    po_client = create_polardb_client(cfg, creds)
    records, start, end = fetch_slow_logs(po_client, cluster_id, args.minutes)
    print(f"获取慢查询 {len(records)} 条")

    if not records:
        print("该时段内无慢查询，流程结束。")
        return

    # 保存 JSON & 生成 HTML
    json_path = save_json(output_dir, records, start, end)
    html_path, top1 = generate_report(json_path, output_dir, cfg_path)

    # 上传 OSS
    time_range_str = (f"{start.strftime('%Y-%m-%d %H:%M:%S')} UTC ~ "
                      f"{end.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    html_url = json_url = None
    try:
        uploader = OSSUploader(cfg)
        json_url = uploader.upload_json_report(json_path, time_range_str)
        html_url = uploader.upload_html_report(html_path, time_range_str)
    except Exception as e:
        print(f"上传 OSS 失败: {e}")
        json_url = f"file://{os.path.abspath(json_path)}"
        html_url = f"file://{os.path.abspath(html_path)}"

    # 推送 Lark
    send_lark_card(cfg, cluster_name, time_range_str, html_url, json_url, len(records), top1)

    print("\n任务完成。")
    print(f"JSON: {json_path}")
    print(f"HTML: {html_path}")


if __name__ == "__main__":
    main()
