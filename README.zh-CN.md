# aillm · AI 模型统一管理网关

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10+-informational.svg)](https://www.python.org/)

**aillm** 是一款在你自己电脑上本地运行的 AI 模型网关：把多个模型服务（多厂商、多把密钥）
集中接入、统一管理、自动调度，并对外提供一个 OpenAI 兼容的 `/v1` 接口，让任意 AI 客户端
一键接入。

> **本地优先，自带 Key。** aillm 不预置任何模型渠道、不收集远程清单、没有会员/订阅体系、
> 不做联网授权校验。所有模型服务由你自行提供访问权限（API Key）；密钥、配置、用量只存在
> 本机 `data/config.json`。

![aillm 管理面板](docs/aillm-home.png)

## 与 one-api / new-api 的对比

各有所长，按你的主要场景选：

| | aillm | one-api | new-api |
|---|---|---|---|
| 定位 | 本地优先、单机/单用户 | 多用户分发、令牌额度 | 多用户分发、更多渠道与功能 |
| 智能路由（按任务选模型） | ✅ 规则速判 + EWMA 四维评分 | ⚠️ 权重/优先级调度 | ⚠️ 同类 |
| 自动容灾（熔断+Fallback 链） | ✅ 原生 | ⚠️ 简单错误重试 | ⚠️ 简单重试 |
| 多 Key 轮询 / 限流分散 | ✅ 轮询 + 健康统计 | ✅ 配额体系 | ✅ 配额体系 |
| 多用户 / 令牌额度 | ❌ 单机单用户（本意如此） | ✅ 完整 | ✅ 完整+更多 |
| 多协议互转 | ✅ OpenAI/Anthropic/Gemini 原生 | ⚠️ 适配器实现、覆盖有限 | ✅ 渠道覆盖较全 |
| 官方接口模板 | ✅ 内置 16 家，一键填入 | ❌ 需手动填 | ❌ 需手动填 |
| 数据库 | ❌ 一个 JSON 文件 | SQLite / MySQL | SQLite / MySQL（可选 Redis） |
| Windows 安装包 | ✅ 一键 exe / zip | ❌ 需自建运行环境 | ❌ 需自建运行环境 |

aillm 专注「个人 / 本地优先」场景：无需数据库、没有账户体系、不内置任何渠道——自带 Key，
给你智能路由、自动容灾、三协议互转和一键桌面版。

## 特性

- **多模型统一管理**：多厂商 / 多把 Key 一处维护，启停、分组（免费池 / 付费池）、健康状态一目了然
- **智能路由**：按问题难度自动挑选最合适的模型（规则速判 + 可选的 LLM 分类器）
- **自动容灾**：熔断 + 可配置 Fallback 链，某家服务异常时对话不中断
- **官方接口模板**：内置主流厂商官方公开接口地址，一键填入，再粘贴你自己的 Key 即可；另含「官方免费额度」分区（额度以官方为准，Key 到官方网页领取）与「本机/自建模型」免密钥分区（Ollama / LM Studio / vLLM，点击自动填入 `__KEYLESS__`）
- **三协议互转**：OpenAI / Anthropic / Gemini 报文之间透明转换
- **Token 压缩**：超长上下文自动截断/摘要，省额度
- **用量统计**：今日 / 近七日 / 近一月 Token 趋势与各供应商占比
- **兼容任意客户端**：标准 `/v1` 接口，ChatBox、沉浸式翻译、Dify、Coze 等即插即用
- **可选 Tauri 桌面客户端**：托盘常驻、开机自启（`gateway-client/`）

## 快速开始

```bash
# 方式 A：直接跑
pip install -r requirements.txt
python server.py          # 启动后打开 http://127.0.0.1:18111/

# 方式 B：Docker
docker build -t aillm .   # 先构建一次
docker run -d -p 18111:18111 \
  -v aillm-data:/data \
  --name aillm aillm      # 配置/密钥保存在 aillm-data 卷里

# 方式 C：Tauri 桌面客户端（可选）
cd gateway-client && npm install && npm run tauri dev
```

用 curl 快速验证网关可用（任意 OpenAI 兼容客户端同理）：

```bash
curl http://127.0.0.1:18111/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer local" \
  -d '{"model":"auto","messages":[{"role":"user","content":"你好"}]}'
```

> Docker 镜像默认监听 `0.0.0.0`（环境变量 `AILLM_HOST` 可改），配置/密钥落在挂载的
> `/data` 卷，无需数据库。

打开面板后：

1. 进入「➕ 添加模型源」，点开《官方接口模板》选择厂商 → 地址自动填入
2. 粘贴**你自己**的 API Key → 「⬇ 获取模型」→ 勾选所需模型 →「✅ 保存选中」
3. 直接网页对话；或把任意 OpenAI 兼容客户端指向 `http://127.0.0.1:18111/v1`，Key 填 `local`
   （模型名填 `auto` 自动挑选，或填具体模型 ID）

> aillm 不提供、也不包含任何免密钥渠道——模型服务来自你的 Key。

## 工作机制

一次请求的路由决策链：协议识别 → 别名解析（`auto` / `auto:free`）→ 池过滤 →
能力过滤 → 配额感知排序（付费优先）→ 熔断检查 → Key 轮询 → 出站协议转换，失败自动 Fallback。

```
客户端请求                                 model=auto
  │
  ├ ① 别名解析              auto / auto:<池> / 别名表达式 (#5)
  ├ ② 池过滤                只在该池「启用」的供应商中找
  ├ ③ 能力过滤              vision / 长上下文 / 推理 / 代码 (#5)
  ├ ④ 配额感知排序          无限额(付费)优先，有限额(免费)后置 (#2)
  ├ ⑤ 熔断检查              跳过 open 状态的供应商 (#1)
  ├ ⑥ Key 轮询              多把 Key 轮询；Key 可限定支持的模型 (#3)
  ├ ⑦ 出站适配              openai/anthropic/gemini 转换 (#10)；供应商级 proxy (#4)
  │
  ├─ 成功 → 写健康统计 + 用量记账
  └─ 失败 → 熔断计数+1 → 沿 Fallback 链换下一家 → 耗尽返回 502
```

## 配置

- 全部配置在 `data/config.json`（首次运行自动创建）
- 结构字段（如 `port`）需重启生效；热字段（供应商/池/提示词/压缩）保存即生效
- 端口冲突改 `"port"`；局域网访问设 `"bindHost": "0.0.0.0"`

## 接口一览

| 方法 & 路径 | 说明 |
|---|---|
| `GET /api/health` | 供应商健康与熔断状态 |
| `GET /api/version` | 版本信息 |
| `GET/POST /api/config` | 读 / 写完整配置 |
| `GET /api/metrics?minutes=30` | 请求量曲线 |
| `GET /api/logs?lines=300` | 网关日志尾部 |
| `POST /api/supplier/test` | 测试连通并拉取模型列表 |
| `POST /api/suppliers/upsert` | 新增 / 更新供应商 |
| `DELETE /api/suppliers/{id}` | 删除供应商 |
| `POST /v1/chat/completions` | OpenAI 兼容对话接口（支持流式） |

## 目录结构

```
server.py                FastAPI 网关入口
client.py                CLI 辅助
core/                    config / router / breaker / protocols / token_compress /
                         prober / smart_router / proxy / model_params / stats
web/                     管理面板（单文件 Web UI）
gateway-client/          可选 Tauri 桌面客户端
packaging/               PyInstaller / Inno / deb 打包工具
tests/                   离线单测
```

## 开发与测试

```bash
# 离线单测（不依赖任何外部服务）
python -m unittest discover -s tests
```

## 合规说明

aillm 仅提供本地单机软件。请确保你接入的模型服务符合服务方官方条款与你所在地的法律法规；
若向他人提供接入服务，请自行评估并承担相应义务。

## License

[Apache-2.0](LICENSE) © yysoulhaha and aillm contributors