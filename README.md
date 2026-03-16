# slowlog_query · PolarDB 慢查询监控

针对阿里云 PolarDB（MySQL 兼容版）的慢查询监控与报告系统，分两条主线：

- **实时告警**：定时检查正在执行的慢 SQL，超阈值立即推送 Lark
- **历史分析**：定时拉取慢查询日志，生成 HTML 报告，上传 OSS，推送 Lark 卡片

---

## 目录结构

```
.
├── main.py              # 历史慢日志分析主入口
├── alert.py             # 实时慢 SQL 告警
├── cleanup_oss.py       # OSS 历史文件清理
├── analyzer.py          # PolarDB 慢查询分析器（聚合、EXPLAIN、HTML 报告）
├── oss_uploader.py      # OSS 上传器（STS 临时凭证 + KMS 加密）
├── slowlog_analyzer.py  # 通用 MySQL 慢日志分析器（备用）
├── login.sh             # 快捷登录 PolarDB
├── config.ini           # 生效配置（含敏感信息，勿提交）
└── config.ini.example   # 配置模板
```

---

## 配置

```bash
cp config.ini.example config.ini
# 编辑 config.ini，填写以下内容：
```

| 配置项 | 说明 |
|--------|------|
| `[polardb] access_key / access_secret` | 阿里云 RAM 用户 AK/SK |
| `[polardb] db_cluster_id` | PolarDB 集群 ID（`pc-xxx`） |
| `[polardb] db_host / db_port / db_user / db_pass` | 直连 PolarDB 的连接信息（用于 EXPLAIN） |
| `[polardb] lark_webhook` | Lark 自定义机器人 Webhook 地址 |
| `[polardb] output_dir` | 本地报告存放目录（默认 `/opt/slowlog_query/polardb_slowlog`） |
| `[oss] bucket_name` | OSS Bucket 名称 |
| `[oss] role_arn` | STS AssumeRole 的 RAM 角色 ARN |

---

## 使用方式

### 历史慢日志分析（`main.py`）

```bash
# 分析最近 30 分钟（默认）
python3 main.py

# 分析最近 60 分钟
python3 main.py -m 60

# 指定配置文件
python3 main.py -m 30 -c /path/to/config.ini
```

**执行流程：**
1. 通过 STS AssumeRole 获取临时凭证
2. 调用 PolarDB V2 API 拉取慢查询记录，保存为 JSON
3. 按 `SQLHash` 聚合，计算 avg / P95 / 扫描行等指标，对每条 SQL 执行 EXPLAIN
4. 生成 HTML 报告，上传 JSON 和 HTML 到 OSS（KMS 加密）
5. 推送 Lark 富文本卡片，包含 Top1 SQL 摘要和报告链接

### 实时慢 SQL 告警（`alert.py`）

```bash
python3 alert.py

# 指定配置文件
python3 alert.py -c /path/to/config.ini
```

查询 `PROCESSLIST` 中正在执行且运行时间 ≥ 60 秒的 SQL，按以下规则触发告警：
- 出现 **1 条** 超过 180 秒的 SQL → 立即告警
- 出现 **3 条及以上** 超过 60 秒的 SQL → 立即告警

告警以 Lark 文本消息发出，包含进程 ID、用户、库名和 SQL 内容。

### OSS 历史文件清理（`cleanup_oss.py`）

```bash
# 删除 90 天前的文件（默认）
python3 cleanup_oss.py

# 删除 30 天前的文件
python3 cleanup_oss.py --days 30

# 仅列出，不删除
python3 cleanup_oss.py --dry-run
```

列出并删除 OSS 上超过指定天数的慢日志文件，复用 `OSSUploader` 的 STS 临时凭证，无需单独配置 `ossutil`。

---

## 定时任务（crontab 示例）

```cron
# 每 5 分钟检查一次实时慢 SQL
*/5 * * * * python3 /opt/slowlog_query/alert.py >> /opt/slowlog_query/logs/alert.log 2>&1

# 每 30 分钟拉取一次历史慢日志并生成报告
*/30 * * * * python3 /opt/slowlog_query/main.py >> /opt/slowlog_query/logs/main.log 2>&1

# 每周日凌晨 2 点清理 OSS 上 90 天前的文件
0 2 * * 0 python3 /opt/slowlog_query/cleanup_oss.py >> /opt/slowlog_query/logs/cleanup.log 2>&1
```

---

## 依赖安装

```bash
pip install pymysql requests oss2 \
    alibabacloud-sts20150401 \
    alibabacloud-polardb20170801 \
    alibabacloud-tea-openapi \
    alibabacloud-tea-util
```

---

## 注意事项

- `config.ini` 含 AK/SK 等敏感信息，**请勿提交到代码仓库**
- OSS 上传走内网 VPC 域名（`oss-{region}-internal.aliyuncs.com`），需在 VPC 内运行
- STS 临时凭证有效期 1 小时，每次运行时自动刷新，无需手动管理
- EXPLAIN 执行依赖直连 PolarDB，若网络不通会降级为错误提示，不影响报告生成
