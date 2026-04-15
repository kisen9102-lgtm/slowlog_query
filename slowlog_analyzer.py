import re
import math
from collections import defaultdict
from datetime import datetime
import json
import sys

class SlowLogAnalyzer:
    """
    SlowLogAnalyzer:
    - 用于分析 MySQL / PolarDB 的慢查询日志
    - 支持 SQL fingerprint、聚合统计、分位数、样例 SQL
    - 支持生成 JSON 报告和 HTML 报告
    """

    # ---------- 静态正则模式 ----------
    _pat_number = re.compile(r'\b\d+\b')
    _pat_float = re.compile(r'\b\d+\.\d+\b')
    _pat_hex = re.compile(r"0x[0-9a-fA-F]+")
    _pat_uuid = re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b')
    _pat_single_quote = re.compile(r"'(?:\\'|[^'])*'")
    _pat_double_quote = re.compile(r'"(?:\\"|[^"])*"')
    _pat_in_list = re.compile(r'\(\s*(?:\?|\d+|\'[^\']*\'|0x[0-9a-fA-F]+)(?:\s*,\s*(?:\?|\d+|\'[^\']*\'|0x[0-9a-fA-F]+))*\s*\)')
    _pat_whitespace = re.compile(r'\s+')

    # ---------- 初始化 ----------
    def __init__(self):
        self.aggregator = {}

    # ---------- SQL fingerprint 方法 ----------
    @staticmethod
    def fingerprint_sql(sql: str) -> str:
        if not sql:
            return ""
        s = sql.strip()
        # 删除注释
        s = re.sub(r'(--[^\n]*\n)|(/\*.*?\*/)', ' ', s, flags=re.DOTALL)
        # 替换字符串 / hex / uuid
        s = SlowLogAnalyzer._pat_single_quote.sub("?", s)
        s = SlowLogAnalyzer._pat_double_quote.sub("?", s)
        s = SlowLogAnalyzer._pat_hex.sub("?", s)
        s = SlowLogAnalyzer._pat_uuid.sub("?", s)
        # 替换浮点和整数
        s = SlowLogAnalyzer._pat_float.sub("?", s)
        s = SlowLogAnalyzer._pat_number.sub("?", s)
        # 替换 IN 列表为 (?)
        s = SlowLogAnalyzer._pat_in_list.sub("(?)", s)
        # 合并空白字符
        s = SlowLogAnalyzer._pat_whitespace.sub(" ", s).strip()
        return s.lower()

    # ---------- 分位数计算 ----------
    @staticmethod
    def percentile(sorted_list, p):
        if not sorted_list:
            return None
        k = (len(sorted_list) - 1) * (p / 100.0)
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return sorted_list[int(k)]
        d0 = sorted_list[int(f)] * (c - k)
        d1 = sorted_list[int(c)] * (k - f)
        return d0 + d1

    # ---------- 聚合日志 ----------
    def add_logs(self, logs):
        for rec in logs:
            sql = rec.get("SQLText") or rec.get("sql") or ""
            if not sql:
                continue

            try:
                qt = float(rec.get("QueryTimes") or rec.get("Query_time") or 0)
            except (ValueError, TypeError):
                continue

            fp = self.fingerprint_sql(sql) or "<empty>"

            ent = self.aggregator.get(fp)
            if ent is None:
                ent = {
                    "count": 0,
                    "total_time": 0.0,
                    "times": [],
                    "samples": [],
                    "user": rec.get("DBUser"),
                    "host": rec.get("HostAddress"),
                    "first_time": rec.get("ExecutionStartTime"),   # Fix #5：原为错误的 ReturnRows/ParseRowNum
                    "last_time": None
                }
                self.aggregator[fp] = ent

            ent["count"] += 1
            ent["total_time"] += qt
            ent["times"].append(qt)

            # 样例 SQL
            if len(ent["samples"]) < 3 and sql not in ent["samples"]:
                ent["samples"].append(sql)

            # 记录最早与最晚时间
            t = rec.get("ExecutionStartTime")
            if t:
                if ent["first_time"] is None or t < ent["first_time"]:
                    ent["first_time"] = t
                if ent["last_time"] is None or t > ent["last_time"]:
                    ent["last_time"] = t


    # ---------- 生成 JSON 报告 ----------
    def generate_report(self):
        reports = []
        for fp, v in self.aggregator.items():
            times = sorted(v["times"])
            count = v["count"]
            total = v["total_time"]
            avg = total / count if count else 0.0
            min_t = times[0] if times else None
            max_t = times[-1] if times else None
            p50 = self.percentile(times, 50) if times else None
            p95 = self.percentile(times, 95) if times else None
            reports.append({
                "fingerprint": fp,
                "count": count,
                "total_time": round(total, 6),
                "avg_time": round(avg, 6),
                "min_time": round(min_t, 6) if min_t is not None else None,
                "max_time": round(max_t, 6) if max_t is not None else None,
                "p50": round(p50, 6) if p50 is not None else None,
                "p95": round(p95, 6) if p95 is not None else None,
                "samples": v["samples"]
            })
        reports.sort(key=lambda x: x["total_time"], reverse=True)
        return reports

    # ---------- 生成 HTML 报告 ----------
    def generate_html_report(self, title="Slow Query Report"):
        reports = self.generate_report()
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        html = f"""
        <html>
        <head>
        <meta charset="utf-8">
        <title>{title}</title>
        <style>
        body {{ font-family: Arial, sans-serif; }}
        table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ border: 1px solid #ddd; padding: 8px; }}
        th {{ background-color: #f2f2f2; }}
        tr:hover {{ background-color: #f5f5f5; }}
        </style>
        </head>
        <body>
        <h2>{title}</h2>
        <p>Generated at: {now}</p>
        <table>
        <tr>
            <th>Fingerprint</th>
            <th>Count</th>
            <th>Total Time(s)</th>
            <th>Avg Time(s)</th>
            <th>Min Time(s)</th>
            <th>Max Time(s)</th>
            <th>P50(s)</th>
            <th>P95(s)</th>
            <th>Samples</th>
        </tr>
        """
        for r in reports:
            samples_html = "<br>".join(r["samples"])
            html += f"""
            <tr>
                <td>{r['fingerprint']}</td>
                <td>{r['count']}</td>
                <td>{r['total_time']}</td>
                <td>{r['avg_time']}</td>
                <td>{r['min_time']}</td>
                <td>{r['max_time']}</td>
                <td>{r['p50']}</td>
                <td>{r['p95']}</td>
                <td>{samples_html}</td>
            </tr>
            """
        html += """
        </table>
        </body>
        </html>
        """
        return html

    # ---------- 从 JSON 文件加载日志 ----------
    def load_json_files(self, paths):
        """Fix #4：补充 load_json_files 方法，供 __main__ 和外部调用"""
        if isinstance(paths, str):
            paths = [paths]
        for path in paths:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
                records = data.get("records", data) if isinstance(data, dict) else data
                self.add_logs(records)

    # ---------- 清空数据 ----------
    def reset(self):
        self.aggregator = {}

    # ---------- 生成带时间范围的报告 ----------
    def generate_report_with_time_range(self):
        reports = []
        for fp, v in self.aggregator.items():
            times = sorted(v["times"])
            count = v["count"]
            total = v["total_time"]
            avg = total / count if count else 0.0

            min_t = times[0] if times else None
            max_t = times[-1] if times else None

            p50 = self.percentile(times, 50) if times else None
            p95 = self.percentile(times, 95) if times else None

            first_time = v.get("first_time")
            last_time = v.get("last_time")

            reports.append({
                "fingerprint": fp,
                "count": count,
                "total_time": round(total, 6),
                "avg_time": round(avg, 6),
                "min_time": round(min_t, 6) if min_t is not None else None,
                "max_time": round(max_t, 6) if max_t is not None else None,
                "p50": round(p50, 6) if p50 is not None else None,
                "p95": round(p95, 6) if p95 is not None else None,
                "samples": v["samples"],
                "user": v.get("user"),
                "host": v.get("host"),
                "first_time": first_time,
                "last_time": last_time
            })

        reports.sort(key=lambda x: x["total_time"], reverse=True)
        return reports


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 slowlog_analyzer.py slowlog_*.json")
        sys.exit(1)

    analyzer = SlowLogAnalyzer()
    analyzer.load_json_files(sys.argv[1:])           # Fix #4：使用正确的 load_json_files 方法

    report_html = analyzer.generate_html_report("UAT 环境慢查询分析报告")   # Fix #4：正确方法名
    with open("slow_query_report.html", "w", encoding="utf-8") as f:
        f.write(report_html)

    print("报告已生成：slow_query_report.html  (双击打开浏览器即可查看)")
