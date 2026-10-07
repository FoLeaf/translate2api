# TransHub Go（译枢）

Python 版 TransHub 的 Go 重写。对外路径、鉴权语义、响应格式与 Python 版完全一致，
ReadFrog 等客户端配置零改动，切换时替换容器即可。

## 与 Python 版的差异

- 单静态二进制，Docker 镜像含 Chromium 约 400MB（Python 版 1.96GB），内存上限 700m（原 1200m）
- 豆包上游：串行 + 凑批（300 毫秒窗口、单次最多 8 段）不变，新增 45 秒调用总超时与网络错误自动重试一次
  （治理上游间歇停滞：实测停滞 121-189 秒会占住串行锁拖垮整条线路）
- 豆包入口：全局 48 + 单 Key 12 双上限排队，凑批按 Key 轮询取段，多用户公平
- B站线路：入口令牌桶（后台可调）+ 上游并发信号量 8 + 连接池，流式透传；鉴权头剥离后不外发
- 后台极简：概览 / Key / 用量 / 设置（含扫码登录与 Cookie 手动导入），会话 Cookie 与 Python 版同格式
- Key 校验热路径不再逐请求写 last_used_at，改由后台协程每分钟批量刷新
- 首次启动若发现同目录 Python 版 transhub.db，自动迁移 Key（哈希直接搬，用户零改动）、设置与凭据；用量历史不迁移

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| TH_DATA_DIR | /data | 数据目录（SQLite 所在） |
| TH_PORT | 8000 | 监听端口 |
| ADMIN_PASSWORD | 空 | 首次启动管理员密码；空则随机生成写入数据目录 |
| TH_DOUBAO_QUEUE_WAIT | 12 | 豆包入口排队等待上限（秒），超限回 429 带 Retry-After |
| TH_DOUBAO_QUEUE_CAP | 48 | 豆包全局同时在网关内排队上限 |
| TH_DOUBAO_KEY_CAP | 12 | 单 Key 同时排队上限（公平性） |
| TH_DOUBAO_TIMEOUT | 45 | 豆包上游单次调用总超时（秒） |
| TH_DOUBAO_BATCH_MAX | 8 | 单次上游调用最多段数 |
| TH_BILIBILI_UPSTREAM_CONC | 8 | B站上游并发信号量 |
| TH_TRUSTED_PROXIES | 127.0.0.1 | 可信代理（nginx），仅信任其 X-Forwarded-For |
| TH_CHROMIUM_BIN | chromium | go-rod 使用的浏览器二进制 |

## 双轨运行与切换

docker-compose.yml 默认映射 8301 端口，与 Python 版（8300）并行：

```bash
docker compose up -d --build
curl http://127.0.0.1:8301/healthz
```

验收通过后，把 nginx 反代指向 8301（或对调端口）即完成切换。
