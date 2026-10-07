# -*- coding: utf-8 -*-
"""全局配置：只从环境变量读“部署级”参数；运行时可调项放 SQLite settings 表。"""
import os

APP_NAME = "TransHub"
APP_TITLE = "译枢 · TransHub"

DATA_DIR = os.environ.get("TH_DATA_DIR", "/data")
PORT = int(os.environ.get("TH_PORT", "8000"))

# 仅首次启动时用于设置管理员密码；为空则生成随机密码并写入 DATA_DIR/initial_admin_password.txt
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# 上游地址（可覆盖，便于调试）
DOUBAO_UPSTREAM = os.environ.get("TH_DOUBAO_UPSTREAM", "https://www.doubao.com").rstrip("/")
BILIBILI_UPSTREAM = os.environ.get("TH_BILIBILI_UPSTREAM", "https://index-translate.bilibili.com").rstrip("/")

# 会话有效期（天）
SESSION_DAYS = int(os.environ.get("TH_SESSION_DAYS", "7"))

# 默认限流（每秒请求数 / 突发），可在后台设置页覆盖
# bilibili 由 4/8 提到 8/16：整页翻译并发批次多，旧值在真实页面下频繁 429 触发重试，体感明显变慢
DEFAULT_RATE = {"doubao": (1.5, 4), "bilibili": (8.0, 16)}

# 豆包入口排队（ReadFrog 客户端默认突发 20、稳态 8/秒，远超上游串行消化能力）。
# 入口不再用令牌桶直接拒绝，改为网关内排队消化，等待超限才回 429 并带 Retry-After，
# 避免客户端无头部时整队列退避 5 秒起步、累计 8 次 429 后放弃段落。
DOUBAO_QUEUE_WAIT = float(os.environ.get("TH_DOUBAO_QUEUE_WAIT", "12"))  # 排队等待上限（秒）
DOUBAO_QUEUE_CAP = int(os.environ.get("TH_DOUBAO_QUEUE_CAP", "12"))  # 同时排队的请求数上限
