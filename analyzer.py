#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PolarDB 慢查询分析器
- 从阿里云 API 返回的 JSON 中按 SQLHash 聚合慢查询
- 生成 HTML 报告（含 EXPLAIN 执行结果）
- 提供 Top1 摘要供 Lark 推送使用
"""

import json
import sys
import os
import math
from datetime import datetime
import configparser
import pymysql


class PolarDBSlowLogAnalyzer:

    def __init__(self, config_path=None):
        self.stats = {}

        if config_path is None:
            config_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "config.ini")

        cfg = configparser.ConfigParser()
        cfg.read(config_path, encoding="utf-8")

        self.db_conf = {
            "host": cfg.get("polardb", "db_host"),
            "user": cfg.get("polardb", "db_user"),
            "password": cfg.get("polardb", "db_pass"),
            "port": cfg.getint("polardb", "db_port", fallback=3306),
        }

    def load_json_files(self, paths):
        if isinstance(paths, str):
            paths = [paths]
        for path in paths:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
                self.add_records(data.get("records", []))

    def add_records(self, records):
        for r in records:
            h = r.get("SQLHash")
            if not h:
                continue

            qt = r.get("QueryTimeMS", 0) / 1000.0

            if h not in self.stats:
                self.stats[h] = {
                    "hash": h,
                    "db": r.get("DBName"),
                    "HostAddress": r.get("HostAddress"),
                    "count": 0,
                    "total_time": 0.0,
                    "total_rows": 0,
                    "max_rows": 0,
                    "times": [],
                    "samples": [],
                    "first": r.get("ExecutionStartTime"),
                    "last": r.get("ExecutionStartTime"),
                }

            s = self.stats[h]
            s["count"] += 1
            s["total_time"] += qt

            rows = r.get("ParseRowCounts", 0)
            s["total_rows"] += rows
            s["max_rows"] = max(s["max_rows"], rows)
            s["times"].append(qt)

            sql = r.get("SQLText", "").strip()
            if len(s["samples"]) < 3 and sql and sql not in s["samples"]:
                s["samples"].append(sql)

            ts = r.get("ExecutionStartTime")
            if ts:
                if not s["first"] or ts < s["first"]:
                    s["first"] = ts
                if not s["last"] or ts > s["last"]:
                    s["last"] = ts

    def percentile(self, values, p):
        if not values:
            return None
        values = sorted(values)
        k = (len(values) - 1) * p / 100
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return values[int(k)]
        return values[int(f)] * (c - k) + values[int(c)] * (k - f)

    def run_explain(self, dbname, sql):
        first_word = sql.strip().split()[0].lower() if sql.strip() else ""
        if first_word not in ("select", "update", "delete"):
            return "EXPLAIN 不支持此语句类型"
        try:
            conn = pymysql.connect(db=dbname, cursorclass=pymysql.cursors.DictCursor, **self.db_conf)
            cur = conn.cursor()
            cur.execute("EXPLAIN " + sql)
            rows = cur.fetchall()
            conn.close()
            result = "\n".join(" | ".join(f"{k}: {v}" for k, v in r.items()) for r in rows)
            return result or "EXPLAIN 无结果"
        except Exception as e:
            return f"EXPLAIN 执行失败: {e}"

    def _build_items(self):
        items = []
        for v in self.stats.values():
            times_sorted = sorted(v["times"])
            avg_time = v["total_time"] / v["count"] if v["count"] else 0
            avg_rows = v["total_rows"] // v["count"] if v["count"] else 0
            items.append({
                "rank": 0,
                "count": v["count"],
                "total_time": v["total_time"],
                "avg_time": avg_time,
                "p95": self.percentile(times_sorted, 95),
                "avg_rows": avg_rows,
                "max_rows": v["max_rows"],
                "db": v["db"] or "未知",
                "sample_sql": v["samples"][0] if v["samples"] else "",
                "first": v["first"],
                "last": v["last"],
                "HostAddress": v.get("HostAddress"),
            })
        items.sort(key=lambda x: x["total_time"], reverse=True)
        for i, item in enumerate(items, 1):
            item["rank"] = i
        return items

    def generate_html(self, title="PolarDB 慢查询分析报告"):
        items = self._build_items()

        all_firsts = [it["first"] for it in items if it["first"]]
        all_lasts  = [it["last"]  for it in items if it["last"]]
        start = min(all_firsts)[:19].replace("T", " ") if all_firsts else ""
        end   = max(all_lasts)[:19].replace("T", " ")  if all_lasts  else ""
        now   = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

        html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ font-family: "Microsoft YaHei", Arial, sans-serif; margin:40px; background:#f7f9fc; }}
  h1 {{ color:#2c3e50; }}
  table {{ width:100%; border-collapse:collapse; background:white;
           box-shadow:0 4px 12px rgba(0,0,0,0.1); border-radius:8px; overflow:hidden; }}
  th {{ background:#3498db; color:white; padding:15px; }}
  td {{ padding:12px 15px; border-bottom:1px solid #eee; }}
  tr:hover {{ background:#f0f8ff; }}
  .sql {{ background:#2c3e50; color:#f1f1f1; padding:15px; border-radius:6px;
          white-space:pre-wrap; font-family:Consolas,Monaco,monospace; font-size:14px; }}
  .critical {{ background:#ffebee !important; }}
  .high {{ background:#fff3e0 !important; }}
  summary {{ cursor:pointer; color:#2980b9; }}
</style>
</head>
<body>
<h1>{title}</h1>
<p>
  <strong>分析时间段：</strong>{start} ～ {end}<br>
  <strong>生成时间：</strong>{now}<br>
  <strong>共发现慢查询事件：</strong>{len(items)} 件
</p>
<table>
<tr>
  <th>排名</th><th>事件数</th><th>总耗时(s)</th><th>平均(s)</th><th>P95(s)</th>
  <th>平均扫描行</th><th>最大扫描行</th><th>数据库</th><th>样例 SQL（点击展开）</th>
</tr>
"""
        for r in items:
            if r["avg_rows"] > 10_000_000:
                severity = "critical"
            elif r["total_time"] > 30 or r["avg_rows"] > 1_000_000:
                severity = "high"
            else:
                severity = ""

            p95_str = f"{r['p95']:.3f}" if r["p95"] is not None else "-"
            explain_html = self.run_explain(r["db"], r["sample_sql"]).replace("\n", "<br>")

            html += f"""<tr class="{severity}">
  <td><strong>{r["rank"]}</strong></td>
  <td>{r["count"]:,}</td>
  <td>{r["total_time"]:.2f}</td>
  <td>{r["avg_time"]:.3f}</td>
  <td>{p95_str}</td>
  <td>{r["avg_rows"]:,}</td>
  <td>{r["max_rows"]:,}</td>
  <td>{r["db"]}</td>
  <td>
    <details>
      <summary>查看完整 SQL + 执行计划</summary>
      <div class="sql">
        <strong>SQL：</strong><br>{r["sample_sql"]}<br><br>
        <strong>EXPLAIN：</strong><br><pre>{explain_html}</pre>
      </div>
    </details>
  </td>
</tr>
"""
        html += """</table>
<p style="color:#7f8c8d;margin-top:50px;">报告由 jackhu 提供</p>
</body></html>"""
        return html

    def save_html(self, output_path, title="PolarDB 慢查询分析报告"):
        html = self.generate_html(title)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)
        return output_path

    def get_top1_summary(self):
        if not self.stats:
            return None

        items = []
        for v in self.stats.values():
            times_sorted = sorted(v["times"])
            avg_time = v["total_time"] / v["count"] if v["count"] else 0
            items.append({
                "count": v["count"],
                "total_time": v["total_time"],
                "avg_time": avg_time,
                "max_time": max(times_sorted) if times_sorted else 0,
                "min_time": min(times_sorted) if times_sorted else 0,
                "p50": self.percentile(times_sorted, 50),
                "p95": self.percentile(times_sorted, 95),
                "db": v["db"] or "未知",
                "sample_sql": v["samples"][0] if v["samples"] else "",
                "first": v["first"],
                "last": v["last"],
                "client_ip": v.get("HostAddress", "unknown"),
            })

        top = sorted(items, key=lambda x: x["total_time"], reverse=True)[0]
        return {
            "client": top["client_ip"],
            "count": top["count"],
            "db": top["db"],
            "total_time": round(top["total_time"], 3),
            "avg_time": round(top["avg_time"], 3),
            "max_time": round(top["max_time"], 3),
            "min_time": round(top["min_time"], 3),
            "p50": round(top["p50"], 3) if top["p50"] is not None else None,
            "p95": round(top["p95"], 3) if top["p95"] is not None else None,
            "sample_sql": top["sample_sql"],
            "first": top["first"],
            "last": top["last"],
        }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 analyzer.py <slowlog.json> [slowlog2.json ...]")
        sys.exit(1)

    analyzer = PolarDBSlowLogAnalyzer()
    analyzer.load_json_files(sys.argv[1:])

    out_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), "polardb_slowlog")
    os.makedirs(out_dir, exist_ok=True)

    out_file = os.path.join(out_dir, "PolarDB_慢查询报告_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".html")
    analyzer.save_html(out_file)
    print("报告生成成功:", os.path.abspath(out_file))
    print(analyzer.get_top1_summary())
