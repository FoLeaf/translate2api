# 豆包翻译接口 → ReadFrog（陪读蛙）接入指南

> 结论先说：**官方陪读蛙没法在设置里直接填这个接口**——豆包协议不是 OpenAI 兼容的（自定义请求体 + SSE 三事件流 + Cookie 鉴权），自定义服务商那条路走不通。帖子的作者是把 Read Frog 整个 fork 了，把豆包三个引擎编译成插件内置 Provider。
> 但你要在**火狐 + 官方陪读蛙**里用它，有一条干净的路：**官方陪读蛙原生支持 DeepLX 服务商（自定义 baseURL）**，我写了一个本地适配器把两种协议接起来，实测通过。见方案 A。

---

## 一、帖子内容与接口协议（已交叉核实）

帖子：linux.do 有人用 dsh 逆向了 Windows 豆包 App 自带浏览器（豆包浏览器）的翻译接口，打包成 Read Frog 改版发布。帖子给了源码 zip 和 chrome-mv3.7z 两个包（网盘签名链接，**有效期到约 2026-10-13**，要下趁早）。

我把 472MB 源码完整下载核对了 Provider 实现，协议与帖子描述一致：

| 项目 | 值 |
|---|---|
| 端点 | `POST https://www.doubao.com/samantha/plugin/stream_article_translate` |
| 鉴权 | 无签名（无 a_bogus），仅靠豆包登录 Cookie（`sessionid`/`sid_tt`/`uid_tt` 等） |
| 请求体 | `raw_text`(字符串数组，≤100段，建议≤50段/≤10000字符)、`target_lang`(19种语言码)、`translate_service`(**字符串** `"0"`火山/`"1"`豆包AI/`"3"`微软)、`scene`(**必须是整数** 1整页/2AI阅读器/3划词/6悬停)、`frontend_source: 1` |
| 响应 | 成功 = `text/event-stream`；参数错/未登录 = HTTP 200 但纯 JSON 带 `code`（本文实测：无 Cookie → `710012001 登录已过期`） |
| SSE 三事件 | `event:json`(data 是 `{code:0,data:{items:[{index,res,detect_lang}]}}`，同段多帧取完整的那条)、`event:err`(整单失败，必须处理)、`event:done`(结束) |
| 已知错误码 | 0 成功；710012001 登录过期；710010202 系统错误(scene传了字符串就是它)；710020202 插件层错误(段数超限/同步端点)；710020702 流错误帧 |

源码里几个帖子没细说的坑（作者注释里写的，可信）：`scene` 传字符串 `"2"` 必报 710010202；同步版端点 `plugin/translate` 恒报 710020202 不可用；`event:err` 帧不处理的话表现为"翻不出来但也不报错"。

## 二、三条接入路径对比

| 路径 | 浏览器 | 陪读蛙 | 成本 | 推荐度 |
|---|---|---|---|---|
| **A. 本地适配器**（本指南主线） | 火狐 ✅ | **官方版不动** | 跑个小脚本 + 粘一次 Cookie | ⭐⭐⭐⭐⭐ |
| B. 装作者的预编译改版 | 仅 Brave/Chrome 系 | 换成改版 | 最低，但换浏览器 | ⭐⭐⭐ |
| C. 用作者源码自编火狐版 | 火狐 | 换成改版 | 装 Node+pnpm 编译，且未签名扩展每次重启要重载 | ⭐⭐ |

**为什么预编译包装不进火狐**：我解包核对了 `chrome-mv3` 的 manifest，后台是 `service_worker`，火狐的 MV3 不支持扩展 service worker，直接加载必挂。作者在 SOURCE_CODE_REVIEW.md 里留了火狐编译说明（`pnpm install && pnpm zip:firefox`），但火狐正式版只认 AMO 签名的扩展，自编包只能 about:debugging 临时加载（重启即失效），日常用太难受——所以才做了方案 A。

## 三、方案 A：本地适配器（火狐 + 官方陪读蛙，推荐）

原理：官方陪读蛙 → DeepLX 协议 → `doubao2deeplx.py` 适配器 → 豆包原生接口。官方陪读蛙的 DeepLX 服务商本来就支持自定义 baseURL，这是它设计的扩展点，不动插件一行代码。

### 第 1 步：拿豆包 Cookie

火狐登录 `www.doubao.com` → F12 → 网络 → 刷新页面随便点一个 doubao.com 的请求 → 请求头里复制 **Cookie 整行**（里面应有 `sessionid`、`sid_tt`、`uid_tt`）→ 粘进脚本同目录的 `doubao-cookie.txt`（整串一行粘进去即可）。Cookie 过期后重新粘一次就行，脚本每次请求都会重读文件，不用重启。

### 第 2 步：跑适配器

```bash
python doubao2deeplx.py            # 默认 127.0.0.1:1188
# 想换引擎/场景：DOUBAO_ENGINE=0(火山,最快)/1(豆包AI,默认)/3(微软)  DOUBAO_SCENE=1
```

### 第 3 步：先做预检（强烈建议）

```bash
curl -X POST http://127.0.0.1:1188/translate -H "Content-Type: application/json" -d "{\"text\":\"hello world\",\"target_lang\":\"ZH\"}"
```

返回里有 `"data": "你好世界"` 就通了；返回 401 说明 Cookie 没配好/已过期。

### 第 4 步：陪读蛙里配置

选项 → API 服务商 → 新增服务商 → **纯翻译服务商 → DeepLX**：

| 字段 | 填写内容 |
|---|---|
| 名称 | `豆包翻译` |
| Base URL | `http://127.0.0.1:1188/translate` |
| API Key | 留空/不填 |

启用后**测试连接**，通过就把它设为翻译功能的默认服务商。整页/划词/悬停/输入翻译都走它。

### 适配器已验证的部分（沙盒实测 2026-10-07）

✅ DeepLX 协议完整实现（陪读蛙发什么、收什么，对照官方源码核对）
✅ SSE 解析：增量帧取最长、心跳注释、`event:err` 显式报错、`done` 检测
✅ 错误透传：无 Cookie/假 Cookie → 401 + `710012001 登录已过期`（真实端点实测）
✅ 长文自动分块（>9000 字符按行切，批内 ≤豆包 10000 上限）
✅ 语言映射：陪读蛙大写码（ZH/ZH-HANT/EN…）→ 豆包码（zh/zh-Hant/en…）
⬜ **真实 Cookie + 真实译文**：需要你的豆包账号，跑第 3 步预检即完成最后验证

## 四、方案 B/C 简述

**B（Brave 装）**：解压 `chrome-mv3.7z` → Brave 地址栏 `brave://extensions` → 开发者模式 → "加载已解压的扩展程序" → 选解压目录。然后直接在 Brave 里登录 doubao.com，改版会自动带浏览器 Cookie，零配置。缺点：豆包翻译只在 Brave 里可用；和官方陪读蛙功能有差异（改版砍掉了通用模型配置）。

**C（火狐自编）**：源码目录 `pnpm install --frozen-lockfile && pnpm zip:firefox`，产物在 `.output/`，用 about:debugging 临时加载。仅适合折腾。

## 五、风险与红线（务必看）

1. **Cookie = 豆包账号钥匙**。`doubao-cookie.txt` 只放本机，别上传别外发；用完可删。
2. **封号风险自担**：这是逆向非公开接口，作者声明里写明禁止商用/批量抓取/大并发。陪读蛙"请求控制"里把每秒请求调到 ≤2、突发 ≤4，别拿它爬站。
3. **接口随时可能变/关**：豆包加固鉴权（加 a_bogus 签名）或风控收紧，这条路就死了，届时回到 B站 Index-Translate 方案（免登录，此前那份指南继续有效）。
4. **第三方改版慎装**：装 B 方案前建议自己过一遍源码（源码包里有作者自己的 SOURCE_CODE_REVIEW.md；我核过核心翻译链路无外传行为，但 472MB 全量审计没做，别当审计结论）。
5. 网盘链接 2026-10-13 左右过期，源码要存档自己留一份。

## 六、两套翻译方案怎么选

| | B站 Index-Translate | 豆包（本方案） |
|---|---|---|
| 登录 | 免登录免 Key | 要豆包账号 Cookie |
| 质量 | 翻译特化模型，150 语种 | 火山/豆包AI/微软三引擎，网页翻译体验好、并发高 |
| 风险 | 官方明示的免费 API，无 ToS 问题 | 逆向接口，有风控/封号不确定性 |
| 配置 | Header Editor 删 Origin 或代理 | 本地适配器 + Cookie |

建议：豆包当日常主力（质量速度好），B站那条当兜底（无账号依赖、最稳）。
