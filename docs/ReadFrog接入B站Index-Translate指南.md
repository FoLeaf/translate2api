# B站 Index-Translate 免费 API → ReadFrog（火狐）接入指南

> 结论先说：**可以直接用，但火狐下必须先解决一个 412 问题**。
> 帖子（linux.do/t/topic/2977401《B站开源Index-Translate 多语言翻译模型家族》）评论区那个 PS1 脚本**只适用于 Chromium 系浏览器 + 沉浸式翻译**，对火狐 + ReadFrog 无效，本文给出火狐方案。

---

## 一、接口是什么（已实测核实，2026-10-07）

B站 Index LLM 团队 2026-10-04 起开放了 Index-Translate-35B-A3B 旗舰翻译模型的**免费公网 API**，完全兼容 OpenAI 规范，**无需 API Key**：

| 项目 | 值 |
|---|---|
| Base URL | `https://index-translate.bilibili.com/v1` |
| 端点 | `/v1/chat/completions`（也支持 `/v1/responses`） |
| 模型名 | `Index-Translate-35B-A3B`（`/v1/models` 目前只有这一个） |
| API Key | 不需要 |
| 流式输出 | 支持（`stream:true` 实测正常） |
| 单次长度 | 约 4096 字符限制（ReadFrog 默认每批 ≤1000 字符，不会碰到） |

官方仓库：`github.com/bilibili/Index-Translate`（README「方式一：免费公网 API」）

### 关键坑：412（本次实测复现）

| 请求方式 | 结果 |
|---|---|
| 无 `Origin` 头（curl/脚本直调） | ✅ 200，正常翻译 |
| `Origin: chrome-extension://...`（Chrome 系扩展） | ❌ 412 拦截页 |
| `Origin: moz-extension://...`（火狐扩展） | ❌ 412 拦截页 |

B站网关的逻辑是：**见到非自家域名的 Origin 头就直接 412**。浏览器扩展发出的跨域请求必然带 `Origin: moz-extension://...`，所以 ReadFrog 直连必然 412——这就是帖子里 PS1 脚本要解决的那个问题（它的做法是给沉浸式翻译的 DNR 规则集加一条"发请求前删掉 Origin 头"的规则，但那只改 Chromium 浏览器里的沉浸式翻译文件，火狐没有这个文件）。

---

## 二、第一步：在 ReadFrog 里添加自定义服务商

1. 打开 ReadFrog **选项（设置）→ API 服务商**，点击**新增服务商**，选择 **OpenAI 兼容（自定义）**
2. 按下面填写：

| 字段 | 填写内容 |
|---|---|
| 名称 | 随意，如 `B站Index翻译` |
| Base URL | `https://index-translate.bilibili.com/v1` |
| API Key | 随便填一个非空值，如 `sk-free`（客户端通常要求非空，服务端不校验） |
| 模型 | 点"获取模型列表"能拉到 `Index-Translate-35B-A3B`；拉不到就手动填这个字符串 |

3. 启用该服务商，先**不要急着点测试连接**——此时十有八九是 412/失败，先把下面第二步做了再测。

---

## 三、第二步：解决 412（火狐方案，二选一）

### 方案 A：Header Editor 删 Origin 头（推荐，装完即用）

原理和帖子里 PS1 脚本完全一样——把发往 `index-translate.bilibili.com` 的请求里的 `Origin` 头删掉。火狐上的现成工具是老牌扩展 **Header Editor**（AMO 地址：`addons.mozilla.org/firefox/addon/header-editor/`，1.1 万日活、4.7 分、2026-05 仍在更新；火狐至今保留阻塞式 webRequest，改请求头是它的本职）。

安装后在 Header Editor 里 **导出/管理 → 添加规则**：

| 字段 | 填写内容 |
|---|---|
| 规则名称 | `B站翻译API去Origin` |
| 规则类型 | **修改请求头** |
| 匹配类型 | **域名** |
| 规则内容 | `index-translate.bilibili.com` |
| 头部名称 | `origin` |
| 头部操作 | **删除（remove）** |

保存并启用，然后回 ReadFrog 点**测试连接**。

> ⚠️ 待核实项：火狐的 webRequest 理论上可以拦截并修改其他扩展（ReadFrog）后台发出的请求（uBlock 等扩展正是如此工作的），但我无法在沙盒里代你实测。**判据很简单：测试连接通过 = 方案 A 成立；仍报错/412 = 直接换方案 B，一分钟的事。**

### 方案 B：本地小代理（100% 兜底，附带脚本）

用我写好的零依赖 Python 脚本 `bilibili-index-translate-proxy.py`（见同目录）：

```bash
python bilibili-index-translate-proxy.py        # 默认 127.0.0.1:8787
```

然后 ReadFrog 服务商的 Base URL 改成：

```
http://127.0.0.1:8787/v1
```

其余（Key、模型）不变。代理会把请求原样转发到 B站并**剥掉 Origin 头**，流式输出也做了透传。缺点：用的时候得开着这个终端窗口。

### 方案 C：等官方

ReadFrog 社区已经动起来了：issue #2309《添加Bilibili免费模型》、PR #2310《feat(providers): add free Bilibili Index provider》（**截至 2026-10-07 该 PR 已关闭但未合并**）。若后续有新 PR 合入，ReadFrog 会内置该服务商，届时以上步骤全部作废，直接在"内置服务商"里选即可。可以隔段时间去仓库搜 "Bilibili"。

---

## 四、调优建议（防限流）

免费公共 API，别按 ReadFrog 默认的 8 请求/秒去打。**设置 → 翻译 → 请求控制** 里建议：

- 每秒平均请求数：`2`（保守可设 `1`）
- 最大突发请求数：`4`
- 每批最大字符数：保持默认 `1000`（别调过 2000，接口有 4096 字符限制）

模型本身是翻译特化模型（150 语种、术语/格式/语气保留指令），做网页双语翻译正是它的本行；但 ReadFrog 的"AI 讲解/生词卡"等需要 JSON 结构化输出的功能它不一定稳，别把它分配给翻译以外的功能。

---

## 五、验证清单

1. ✅ API 可用性：`curl https://index-translate.bilibili.com/v1/models` 返回模型列表（本文已实测 200）
2. ✅ 无 Origin → 200；带扩展 Origin → 412（本文已实测复现）
3. ⬜ Header Editor 规则生效（ReadFrog 测试连接通过）
4. ⬜ 找一个英文网页按 Alt+A 沉浸式翻译，译文正常双语显示
5. ⬜ 若走方案 B：代理窗口能看到 `[proxy] POST /v1/chat/completions` 日志

## 六、注意事项

- 免费 API 的**开放期限未公布**，随时可能调整限流或下线；4096 字符外的长文会出问题（ReadFrog 批处理已规避）。
- 所有被翻译的文本会发送到 B站服务器，**别拿它翻敏感内容**。
- 若哪天突然集体 412/403，先怀疑官方加了校验，去官方仓库 issue 区看看。
- linux.do 帖子本身被 Cloudflare 盾拦着，我是通过搜索引擎+你补充的评论区脚本+官方 GitHub 还原的全部信息，帖子正文如还有其他接口细节，以帖子和官方仓库为准。

## 2026-10-07：上游 /v1 接口故障与网关适配

- B站 2026-10-05 部署后，`/v1/chat/completions` 对全部模型（2B/9B/35B-A3B）返回 500（`server: uvicorn` 的 Internal Server Error），复刻官方 call_api.py 的完整报文也一样；`/v1/models` 仍 200。截至本日仓库 issues 无人报告，只能等上游修复。
- 网页端（/?p=/site/translate.html）不受影响：它走的是 `/?p=/translate_stream` 端点，报文为 `{text, source_lang, target_lang, model, stream}`，模型更名为 `index-mt-2b / index-mt-9b / index-mt-35b`，返回标准 OpenAI chunk 形态的 SSE。
- 云端网关已适配：对外 OpenAI 兼容接口不变（ReadFrog 零改动），对内改调 translate_stream；旧模型名（Index-Translate-2B 等）自动映射到对应 index-mt 档位；目标语言默认 zh，可在后台设置 `bili_target_lang` 覆盖；上游错误依旧原样直返。
- **本地脚本方案（bilibili-index-translate-proxy.py）直连 /v1，同样受此故障影响**，待上游修复后自动恢复；急需使用请走云端网关或网页端。

### 上游配额与网关配速（2026-10-07 补充）

- translate_stream 对来源 IP 有 **60000 tokens/分钟**硬配额（429 响应体原文：TPM limit of 60000 tokens/min exceeded），耗尽后全量 429，每秒约回填 1000 tokens；并发本身无限制（16 路并发大文本 1.5 秒级完成）。
- 云端网关已按此配额在入口配速：估算 token 排队消化（等待上限 8 秒），差距过大回 429 并带按真实缺口计算的 Retry-After；上游实际 429 时同步清零本地预算。配额值可经环境变量 TH_BILI_TPM 调整。
- 体感参考：普通页面（40 段约 5k token）一次成型无感知；160 段大页面约 20k token，单页没问题，连续翻译多个大页面会进入每分钟配额节奏（约 2 请求/秒），网关排队消化，段落不会丢。
