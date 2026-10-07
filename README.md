# translate2api

把豆包网页翻译与 B 站 Index-Translate 免费 API 接入 ReadFrog（陪读蛙）浏览器的工具集，
包含两个零依赖的本地脚本和一个 Docker 化的云端网关项目。

## 目录结构

```
translate2api/
├── doubao2deeplx.py                  # 豆包翻译 → DeepLX 协议本地适配器（仅标准库）
├── bilibili-index-translate-proxy.py # B站 Index-Translate 本地代理：剥 Origin 头防 412（仅标准库）
├── transhub/                         # TransHub 译枢：云端翻译网关（FastAPI + Docker）
│   ├── app/                          #    网关、后台管理、服务商/登录流插件
│   ├── Dockerfile / docker-compose.yml
│   └── README.md                     #    项目说明与部署步骤
└── docs/                             # 使用指南
    ├── 豆包翻译接入ReadFrog指南.md
    ├── ReadFrog接入B站Index-Translate指南.md
    └── 云端翻译网关部署与使用指南.md
```

## 快速开始

### 方案一：本地脚本（无需服务器）

| 线路 | 脚本 | 指南 |
|---|---|---|
| 豆包翻译（DeepLX 协议） | `python doubao2deeplx.py` | [豆包翻译接入ReadFrog指南](docs/豆包翻译接入ReadFrog指南.md) |
| B站 Index-Translate（OpenAI 协议） | `python bilibili-index-translate-proxy.py` | [ReadFrog接入B站Index-Translate指南](docs/ReadFrog接入B站Index-Translate指南.md) |

两个脚本只依赖 Python 3 标准库，监听 127.0.0.1，按指南里的地址填进 ReadFrog 即可。

### 方案二：TransHub 云端网关（推荐）

豆包 + B站两条线路聚合成一个 Docker 服务：带密码保护的后台、扫码自动获取 Cookie、
API Key 鉴权、令牌桶限流、用量统计。部署与使用见
[云端翻译网关部署与使用指南](docs/云端翻译网关部署与使用指南.md) 和 [transhub/README.md](transhub/README.md)。

```bash
cd transhub
docker compose up -d --build
docker logs -f transhub   # 未设 ADMIN_PASSWORD 时，随机初始密码打印在这里
```

## 免责声明

- 豆包线路为逆向非公开接口，仅供个人学习研究，禁止商用或批量抓取，存在风控与失效风险。
- B站 Index-Translate 免费 API 的开放期限未公布，随时可能调整或下线。
- 所有被翻译文本会经过本机或服务器与对应上游，请勿翻译敏感内容。
