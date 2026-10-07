# TransHub · 译枢 — 自托管云端翻译网关

把「豆包网页翻译」与「B站 Index-Translate 免费 API」聚合成一个 Docker 化的云端翻译服务，
带密码保护的后台管理系统、扫码自动获取 Cookie（类 OAuth 体验）、可扩展的服务商插件架构。
客户端只认两个地址，ReadFrog（陪读蛙）零改动接入。

```
ReadFrog ──DeepLX 协议──▶ /doubao/translate ──▶ www.doubao.com（SSE 流式翻译，带 Cookie）
ReadFrog ──OpenAI 协议──▶ /bilibili/v1/...  ──▶ index-translate.bilibili.com（剥 Origin 防 412）
                              ▲
                        后台管理 /admin（密码登录、扫码取 Cookie、API Key、限流、用量）
```

## 功能

- **后台管理系统**（`/admin`）：密码登录（scrypt 哈希 + HMAC 会话 Cookie + 防爆破限速）
  - 仪表盘：服务商状态 / 24h 用量 / ReadFrog 接入配置一键复制
  - 服务商管理：扫码登录（自动收割 Cookie 入库）、引擎/场景配置、连通性测试、应急手动导入
  - API Key 管理：创建即全站强制鉴权（Bearer / DeepL-Auth-Key 均可）
  - 使用记录、限流调整（令牌桶，默认豆包 1.5/s 突发 4）、修改密码
- **自动获取 Cookie**：
  - 豆包：容器内 Playwright 真实浏览器打开登录页，截图实时回传后台，手机扫码后
    自动收割 Cookie（含 HttpOnly）。豆包 SSO 扫码接口被字节风控(bdms)保护，纯 API 方案不可行。
  - B站：passport.bilibili.com 公开 QR API，纯服务端轮询，扫码即存。
- **B站 412 免疫**：网关转发时剥离 `Origin/Referer` 等头，等效于"PS1 补丁"云端化，浏览器零配置。
- **高内聚低耦合**：`app/providers/` 一个文件一个服务商（`@register` 自动发现），
  `app/login/` 一个文件一个登录流（`@register_flow`），新服务商两步接入，路由与后台自动出现。

## 部署

```bash
# 服务器上
git clone / 上传本目录到 /opt/transhub
cd /opt/transhub
echo 'ADMIN_PASSWORD=你的初始密码' > .env      # 可选
docker compose up -d --build
docker logs -f transhub                        # 没设 .env 的话，随机密码打印在这里
```

数据持久化在 `./data`（SQLite + 初始密码文件），升级重跑 `up -d --build` 即可。

## ReadFrog 配置

| | 豆包 | B站 |
|---|---|---|
| 服务商类型 | 纯翻译服务商 DeepLX | OpenAI 兼容（自定义） |
| Base URL | `http://<服务器>:8300/doubao/{{apiKey}}/translate` | `http://<服务器>:8300/bilibili/v1` |
| API Key | 后台创建的 `th-...`（必填，用于替换 URL 里的占位符） | 同左 |
| 模型 | — | `Index-Translate-35B-A3B` |

> 陪读蛙的 DeepLX 服务商不发鉴权头，API Key 只能经 Base URL 里的 `{{apiKey}}`
> 占位符（替换自 API Key 字段）嵌进路径，网关已支持从路径读取 Key；
> 会带 `Authorization: Bearer` 头的客户端也可以直填 `http://<服务器>:8300/doubao/translate`。

请求控制建议：每秒 ≤2、突发 ≤4（豆包线路防风控）。

## 新增服务商（开发指南）

1. `app/providers/xxx.py`：继承 `TranslationProvider`，实现 `deeplx_translate()` 或
   `openai_proxy()`，类上 `@register`；
2. （如需登录）`app/login/xxx_qr.py`：继承 `LoginFlow`，`@register_flow("xxx")`；
3. 在 `app/gateway.py` 加一行路由（或复用现有通用路由），后台页面自动渲染。

## 常见运维

```bash
docker compose logs -f          # 日志
docker compose restart          # 重启
docker compose pull && docker compose up -d --build   # 升级
sqlite3 data/transhub.db        # 数据（凭据/Key/用量）
```

- 豆包 Cookie 一般 30 天左右过期：后台会标记"已失效"，去服务商页重新扫码即可。
- 忘记密码：删掉 `data` 目录会重置一切；或 `python - <<'EOF'` 重算
  （推荐：进容器 `docker exec -it transhub python -c "from app import db,security; security.set_admin_password('新密码')"`）。

## 风险提示

- 豆包线路为逆向非公开接口，仅供个人学习，禁止商用/批量抓取，有风控与失效风险。
- 所有被翻译文本会经过本服务器与对应上游（豆包/B站），勿翻译敏感内容。
- 后台是 HTTP 明文时建议走 SSH 隧道访问（见设置页提示）。
