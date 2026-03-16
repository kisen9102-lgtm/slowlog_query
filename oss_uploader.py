#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OSS 上传器
- 通过 STS AssumeRole 获取临时凭证（避免 AK/SK 直接使用）
- 上传慢日志 JSON 和 HTML 报告至阿里云 OSS
- 启用 KMS 服务端加密
- 清理本地超过指定时长的历史文件
"""

import os
import shutil
import configparser
from datetime import datetime, timedelta

from alibabacloud_sts20150401.client import Client as StsClient
from alibabacloud_sts20150401 import models as sts_models
from alibabacloud_tea_openapi import models as open_api_models
import oss2


class OSSUploader:

    def __init__(self, cfg):
        """
        :param cfg: configparser.ConfigParser 实例，已加载 config.ini
        """
        region = cfg.get("polardb", "region")

        self._access_key     = cfg.get("oss", "access_key")
        self._access_secret  = cfg.get("oss", "access_secret")
        self.bucket_name     = cfg.get("oss", "bucket_name")
        self.role_arn        = cfg.get("oss", "role_arn")
        self.role_session    = cfg.get("oss", "role_session_name")

        self.sts_endpoint          = f"sts-vpc.{region}.aliyuncs.com"
        self.oss_endpoint_internal = f"https://oss-{region}-internal.aliyuncs.com"
        self.oss_endpoint_public   = f"https://oss-{region}.aliyuncs.com"

        self.local_dir  = cfg.get("polardb", "output_dir")
        self.oss_prefix = "polardb_slowlog/"
        self.kms_headers = {"x-oss-server-side-encryption": "KMS"}

        creds = self._get_sts_token()
        self.auth   = oss2.StsAuth(creds.access_key_id, creds.access_key_secret, creds.security_token)
        self.bucket = oss2.Bucket(self.auth, self.oss_endpoint_internal, self.bucket_name)

        os.makedirs(self.local_dir, exist_ok=True)
        print(f"[OSS] 认证成功 (AssumeRole)，已启用 KMS 加密")

    def _get_sts_token(self):
        api_cfg = open_api_models.Config(
            access_key_id=self._access_key,
            access_key_secret=self._access_secret,
            endpoint=self.sts_endpoint,
        )
        client  = StsClient(api_cfg)
        request = sts_models.AssumeRoleRequest(
            role_arn=self.role_arn,
            role_session_name=self.role_session,
            duration_seconds=3600,
        )
        try:
            return client.assume_role(request).body.credentials
        except Exception as e:
            print(f"[ERROR] STS 获取失败: {e}")
            raise

    def _safe_filename(self, prefix: str, time_range_str: str, ext: str) -> str:
        safe = (time_range_str.replace(" ", "_").replace(":", "")
                              .replace("~", "to").replace("-", "").replace("/", ""))
        return f"{prefix}_{safe}.{ext}"

    def _upload(self, file_path: str, oss_key: str) -> str:
        print(f"[OSS] 上传中: {file_path} -> {oss_key}")
        self.bucket.put_object_from_file(oss_key, file_path, headers=self.kms_headers)
        url = f"https://{self.bucket_name}.{self.oss_endpoint_public.replace('https://', '')}/{oss_key}"
        print(f"[OSS] 上传成功: {url}")
        return url

    def upload_html_report(self, html_path: str, time_range_str: str) -> str:
        filename   = self._safe_filename("slowlog_report", time_range_str, "html")
        final_path = os.path.join(self.local_dir, filename)
        shutil.copyfile(html_path, final_path)
        return self._upload(final_path, f"{self.oss_prefix}{filename}")

    def upload_json_report(self, json_path: str, time_range_str: str) -> str:
        filename   = self._safe_filename("slowlog_raw", time_range_str, "json")
        final_path = os.path.join(self.local_dir, filename)
        shutil.copyfile(json_path, final_path)
        return self._upload(final_path, f"{self.oss_prefix}{filename}")

    def upload_file(self, file_path: str, time_range_str: str = "") -> str:
        """上传任意单个文件（无重命名）"""
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")
        filename = os.path.basename(file_path)
        oss_key  = f"{self.oss_prefix}{filename}"
        return self._upload(file_path, oss_key)

    def cleanup_local(self, hours: int = 24):
        """删除本地目录中超过 hours 小时的 .json / .html 文件"""
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        for fname in os.listdir(self.local_dir):
            if not fname.endswith((".json", ".html")):
                continue
            fpath = os.path.join(self.local_dir, fname)
            if datetime.utcfromtimestamp(os.path.getmtime(fpath)) < cutoff:
                os.remove(fpath)
                print(f"[OSS] 已清理旧文件: {fname}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="OSS 上传工具")
    parser.add_argument("--file",   help="上传指定文件")
    parser.add_argument("--hours",  type=int, default=24, help="清理超过多少小时的本地文件（默认 24）")
    parser.add_argument("--config", default=None, help="config.ini 路径（默认脚本同目录）")
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.realpath(__file__))
    cfg_path   = args.config or os.path.join(script_dir, "config.ini")
    cfg = configparser.ConfigParser()
    cfg.read(cfg_path, encoding="utf-8")

    uploader = OSSUploader(cfg)

    if args.file:
        uploader.upload_file(args.file)
        return

    uploader.cleanup_local(hours=args.hours)
    print("清理完成")


if __name__ == "__main__":
    main()
