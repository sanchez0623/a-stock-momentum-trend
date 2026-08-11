# 项目级长期记忆（Momentum Trader / A股动量趋势交易系统）

> 本文档是从 IDE 记忆库导出的**项目级长期记忆**，随仓库共享，供所有协作者（含 AI 助手）保持一致的项目上下文。
>
> **维护约定**：需求更新或 bug 修复后，若记忆发生变化，请同步更新本文档并随代码一起 commit + push。
> **敏感信息已脱敏**：所有 API Key / Token 请从本地 `.env` 读取，勿写入本文档或代码仓库。

---

## 0. 工作流约定（总览）

- **提交推送**：每次需求更新或 bug 修复完成后，应及时 `git commit` 并 `git push` 到 GitHub 远端，无需等待用户提醒（详见 §2.1）。
- **变更控制**：阅读/排查代码时顺带发现其它 bug 或隐患，严禁直接动手改；先说明现象/根因/影响，给出方案，等用户确认后再实施。
- **AI 配置禁区**：AI/LLM 相关配置（模型、API Key、开关、base_url，含 .env 与页面配置）禁止主动修改；发现异常只说明事实、给出选项，由用户决定（详见 §6）。

---

## 1. 项目信息

### 1.1 项目介绍（project_introduction，24 条）

#### 1.1.1 Momentum Trader 项目概述
项目定位：面向个人投资者的「交易副驾」系统，专注「选股→信号→计划→指引→记录→复盘→评分」全链路，**不接实盘下单**。
核心能力：多源自动切换（mootdx/腾讯/东财/AKShare）+ 健康分熔断；纯 pandas 指标库（MA/MACD/RSI/ADX 等）；五类信号（首仓/加仓/减仓/止损/做T）+ 金字塔仓位 + 凯利建议；三道风控（日亏熔断/连亏降仓/回撤防守）；AI 复盘（规则诊断先行 + LLM 兜底，兼容 OpenAI 协议，支持 DeepSeek/Ollama）；rich 终端面板 + Web 前端双入口。

#### 1.1.2 回测中心 v2 功能设计方案
现有回测存在严重准确性问题：动态前复权导致未来函数、股票池幸存者偏差、无法指定建仓时点、日线缓存期限过短、缺乏基准对比、做T逻辑口径不统一。回测中心 v2 聚焦三大模块：
- **P0 数据层改造**：新建独立 `backtest_kline` 表，用 baostock 按起止日期拉取前复权数据并冻结存储，彻底隔离实盘缓存；
- **P1 持仓回测**：模式A（从 Position 表一键回放真实持仓，生成躺平线/纪律线/系统线三线对照及差异归因）+ 模式B（多股票、不同时间建仓的组合回测，支持同票分批建仓）；
- **P2–P4 扩展**：信号审计回测（真实成交 vs 纪律曲线逐笔比对）、参数敏感性网格、基准常态化对比。
核心目标：让回测回答「我手上的持仓，按系统纪律管理会怎样」。

#### 1.1.3 回测中心 v2 核心设计决策：前复权冻结快照
为解决腾讯动态前复权缓存导致的「未来函数」问题，新建独立 `backtest_kline` 行式表，通过 baostock 按指定起止日期拉取前复权数据并冻结存储，与实盘缓存完全隔离，从数据源头确保回测准确性。

#### 1.1.4 AI 助理模块定位与边界
AI 助理是可开关的独立功能模块，位于 `core/assistant/`，不侵入现有 `screener/signals/plan` 等核心逻辑，仅作为编排层调用已有服务；开启后自动执行盘前选股点评、盘中信号提醒、盘后 AI 日报三阶段流水线。

#### 1.1.5 AI 助理开关驱动运行模式
AI 助理通过配置开关（`ai_assistant.enabled`）控制启停，支持盘中动态开启/关闭；开启后由 APScheduler 按配置间隔（如盘中每 5 分钟）触发流水线，关闭后完全退回纯规则手动流程，不影响原有功能。

#### 1.1.6 市况判断数据与规则
市况判断（择时闸门）需至少 220 根参考指数日线 K 线；当前配置沪深300（0.000300）和创业板指（0.399006）；判定逻辑：收盘价 > MA200 且 MA60 > MA200 → 单指数「多头」，2/2 多头→看多，0/2→看空，1/2→中性；该功能不依赖「择时闸门.enabled」开关，仅用于日报信息展示。

#### 1.1.7 市况不明（unknown）模式判定逻辑与实时性
市况分类新增 unknown 模式：ADX ≥ 18（有趋势强度），但均线结构既不满足严格多头排列（MA10 > MA20 > MA60），也不满足放宽多头判据（MA10 > MA60 且 MA20 > MA60），价格位置也不处于回踩等明确区间——本质是「有强度、没结构、没位置」的模糊态。每次生成交易计划或评估信号时通过 force=True 强制拉取最新日线（含盘中 K 线）和实时行情即时计算，完全实时，非缓存结果。

#### 1.1.8 交易计划市况分类逻辑：unknown 模式与买入信号观望机制
- 当前模式不再默认硬套 `trend_pullback`，新增 `unknown` 模式；
- unknown 模式配置为 `allow_add=False / max_stages=0 / 加仓门槛不可达`；
- unknown 模式下所有买入类信号（BUY_FIRST/BUY_ADD/T_BUY）统一转为 `hold`（观望），卖出与止损类信号照常执行（风控优先）；
- 放宽多头趋势判据：新增 `ma_uptrend` 判定（MA10 > MA60 且 MA20 > MA60，允许中间均线缠绕）；
- 市况不明时计划明确标注「市况不明」并给出保守操作建议（如「建议观望，暂不建仓加仓；等待市况明朗或信号转强后再动手」）。

#### 1.1.9 交易计划一致性说明行规范
计划内容中新增「一致性」说明行：显式比对信号原始类型（如 BUY_ADD、SELL_STOP）与最终计划动作（如 hold、sell_stop），一致则标注 ✓；不一致则清晰列出拦截原因（金字塔档位已用尽、市况特征不明确、T+1 限制、未达可执行条件等），使「信号与计划一致时才动手」的纪律提醒具备可核查依据。

#### 1.1.10 前端数据获取统一为 react-query
将原有 9 个页面（Plans/Trades/Review/Signals/Watchlist/Backtest/AiReview/Screener/Settings）的手动 `useState + useEffect + setInterval` 模式全部迁移至 `useQuery` 声明式数据获取，实现轮询、缓存、去重、错误处理、写后刷新统一管理，消除双轨制技术债。

#### 1.1.11 盘后 AI 日报功能范围限定
盘后 AI 日报功能**仅包含持仓和自选（观察池）两个模块**，不包含市况、信号、风险提示、纪律评分等其他原方案内容。

#### 1.1.12 选股结果一键进入信号流程
选股结果页每行股票新增「看信号」入口，点击跳转 `/signals?symbol=xxx&name=xxx`，信号中心自动填充股票代码与名称并自动触发评估（无需手动点「评估」按钮），实现「选股 → 信号 → 计划」端到端闭环。

#### 1.1.13 删除操作二次确认基础规则
所有删除类操作（删除分析记录、删除条件预设、移除自选、删除交易/计划等）必须弹出二次确认对话框，禁止无确认直接删除。后端接口保持幂等（重复调用返回 404 或 False），前端负责交互确认；确认框风格与项目 toast/Button 组件一致。

#### 1.1.14 分析记录时间字段需为东八区本地时间
分析记录（如 `screenerhistory` 表）的 time 字段必须准确反映东八区本地时间，用于结果展示、审计追踪和用户理解；历史记录已修正，新记录需保证写入即为 CST。

#### 1.1.15 选股模块参数语义缺陷及修复
前端选择「全部A股」时传参为 undefined，后端 `uni_name = universe or 配置默认 or 'all'` 回退至默认指数池（如上证50），造成「全部A股 + 光学光电子」实际执行为「sz50 ∩ 光学光电子 = 0」。已修复为显式传 `universe='all'`。

#### 1.1.16 复盘记忆 RAG 的双点调用链路
- ① 检索（A点）：`_llm_review` 阶段将本次复盘问题文本向量化，与历史记忆比相似度，注入 Step2 提示词；
- ② 索引（B点）：`index_review` 阶段将本次复盘生成的记忆文本（问题+建议+采纳状态+效果）向量化存入 `ReviewMemory` 表。
两者均调用同一 SiliconFlow API，任一环节失败静默降级，不影响主流程。

#### 1.1.17 AI 复盘模块需支持每日复盘
AI 复盘需补充「今日」复盘范围：后端支持 `day` 时间范围（0点至23:59:59），前端下拉菜单新增「今日」选项，测试覆盖边界与统计逻辑。

#### 1.1.18 数据源优先级自愈机制与理杏仁专用定位
数据源管理器具备优先级自愈：已注册但未在持久化优先级列表中的数据源（如理杏仁）出现时自动追加到优先级队列末尾。理杏仁为**专用型数据源**，仅服务申万 2021 行业分类刷新，不声明通用能力（K线/实时价等），不参与 manager 通用取数路由，仅由 classification 模块显式直连调用。

#### 1.1.19 选股功能默认配置与并发策略
默认选股池由全市场（`all`）调整为上证50（`sz50`）；扫描主循环并发数 3，每只保留 0.05 秒降压间隔；显式传入股票列表的扫描请求跳过选股池预筛。

#### 1.1.20 选股功能核心过滤逻辑重构
将原「股票池」（单选）和「筛选条件」（多选）合并为统一「股票条件」面板：指数池支持多选（后端 `universe` 支持逗号分隔多值）；所有条件统一展示，顶部为可交互条件 chips（带 × 可删除）+「清空全部」；底部固定说明「组内任一命中(OR)，组间全部满足(AND)」；支持条件组合预设（保存/一键应用/hover 删除）。

#### 1.1.21 选股扫描引擎的条件交集逻辑与默认指数池限制
各筛选维度（指数池、板块、申万三级行业）之间为交集(AND)关系，同一维度内多选为并集(OR)。默认指数池上证50 仅覆盖 33 个申万三级行业（全市场约 500 个），所选行业不在池内时扫描结果为 0；行业树显示的全市场口径与实际扫描范围不一致，易造成误导。

#### 1.1.22 信号分析生成计划功能交互优化
「生成计划」点击后弹出确认对话框：①「是，生成并跳转」→ 生成后跳转「交易计划」页；②「否，留在本页」→ 生成后停留当前页 + toast 提示「已生成 XXX 的交易计划」；③「取消」→ 不触发任何生成。替代原直接生成并强制跳转的行为。

#### 1.1.23 资金账户权益计算公式
- 可用资金 = 启动资金 + 已实现盈亏 − 持仓成本（含手续费）
- 总权益 = 可用资金 + 持仓市值 = 启动资金 + 已实现盈亏 + 浮动盈亏
- 已实现盈亏从 trade 表汇总卖出净额（已扣双边手续费）；浮动盈亏 = 持仓市值 − 持仓成本

#### 1.1.24 交易系统核心风控策略
- **日亏损熔断**：单日账户浮亏达 3% 时，当日禁止一切新开仓（已有持仓的止损、减仓仍可执行）；
- **防守模式**：净值从历史高点回撤超 10% 时进入只减不加状态，所有买入类信号被拦截；
- **总仓位控制**：持仓市值占总资金比例不得超过 80%，超限禁止加仓。

### 1.2 技术栈（project_tech_stack，6 条）

#### 1.2.1 全栈技术选型
后端：FastAPI + Uvicorn（端口 8002）；前端：Vite 5 + React 18 + TypeScript（开发端口 5175，构建产物由后端托管）；数据层：SQLite（SQLModel）+ CSV 双写；定时任务：APScheduler；容器化：Docker 多阶段构建（node 构建前端 → python 运行时）、docker-compose 单容器部署。

#### 1.2.2 前端技术栈配置
React 框架，数据获取统一使用 react-query（替代手写 `useState + useEffect + setInterval`）；代码质量：ESLint 9 flat 配置，集成 typescript-eslint 和 react-hooks v6（启用 `flat/recommended` 规则集），包含 react-refresh 插件。

#### 1.2.3 批量行情接口设计
后端新增批量实时行情接口 `POST /api/quote/batch`，复用现有 5 秒内存缓存机制，支持单只与批量行情共用同一序列化逻辑。

#### 1.2.4 SiliconFlow embedding 服务接入
使用 SiliconFlow 的 OpenAI 兼容 embedding 接口（`https://api.siliconflow.cn/v1/embeddings`）进行向量化，模型固定为 `BAAI/bge-m3`（1024 维），用于复盘记忆的检索与索引。

#### 1.2.5 SiliconFlow embeddings 技术选型
同 §1.2.4：OpenAI 兼容 embeddings API + `BAAI/bge-m3`（1024 维中文嵌入），用于向量检索（RAG）。

#### 1.2.6 AI 助理使用 LangGraph 进行流水线编排
AI 助理模块采用 LangGraph 构建状态图（StateGraph）实现多阶段流水线编排，节点包括选股、点评、计划生成、提醒推送和日报生成，支持 checkpointer 状态持久化与失败重试。

### 1.3 构建配置（project_build_configuration，3 条）

#### 1.3.1 标准化后端启动命令流程
1. `cd backend`
2. `python -m venv .venv`
3. 激活并安装依赖：`pip install -r requirements.txt -r requirements-dev.txt` 和 `pip install -e ".[optional]"`
4. 启动：`.venv\Scripts\python -m uvicorn app.main:app --reload --port 8002`

#### 1.3.2 Makefile 构建与开发命令
- `make install` / `install-dev`：安装后端依赖（含 ruff/mypy/pytest）
- `make dev-backend` / `dev-frontend`：本地热重载启动（uvicorn --reload / vite dev）
- `make build`：构建前端 dist 目录
- `make up`：Docker 生产模式一键启动；`make dev`：Docker 开发模式双容器
- `make lint`：ruff 检查 + mypy 类型检查

#### 1.3.3 ESLint 集成到构建与 CI
package.json 新增 npm 脚本 `lint` 和 `lint:fix`；CI 工作流（`.github/workflows/ci.yml`）在前端构建前强制执行 `npm run lint`，确保代码提交前通过 ESLint 检查。

### 1.4 依赖配置（project_dependency_configuration，3 条，敏感信息已脱敏）

#### 1.4.1 后端依赖与质量工具配置
pyproject.toml 管理后端依赖与工具链：Python >=3.11；可选依赖 `mootdx>=0.11`、`akshare>=1.14`；Lint：ruff（行宽 120，忽略 E501 等）；类型检查：mypy（Python 3.11，忽略缺失导入）；测试：pytest（asyncio_mode=auto，testpaths=backend/tests）。

#### 1.4.2 ESLint 开发依赖列表
`npm install -D`：eslint@^9、@eslint/js@^9、typescript-eslint、eslint-plugin-react-hooks@^6、eslint-plugin-react-refresh、globals。

#### 1.4.3 理杏仁 API 认证（凭据已脱敏）
理杏仁 API 使用个人 token 认证，通过请求头或请求体传递；**token 值请从本地 `.env` 的 `LIXINGER_TOKEN` 读取，勿写入仓库**。

### 1.5 环境配置（project_environment_configuration，12 条，敏感信息已脱敏）

#### 1.5.1 运行环境与启动配置
- Python 3.11+、Node.js 18+；环境变量：`.env` 中配置 LLM API KEY、数据源开关、时区(TZ)
- 数据持久化：`./data` 目录挂载至容器 `/app/data`
- 启动：Docker `docker compose up -d --build`（http://localhost:8002）；本地 `cd backend && uvicorn app.main:app --reload --port 8002` + `cd frontend && npm run dev`

#### 1.5.2 统一 Python 虚拟环境策略
仅维护 `backend/.venv` 作为唯一 Python 环境；项目根目录 `.venv` 已废弃删除（删除前需先停止运行中的后端进程）。此后所有 `pip install` 必须在 `backend/.venv` 内执行。

#### 1.5.3 后端时间戳强制东八区
后端进程时区不可控（当前为 UTC），所有写入数据库的时间戳（如 `screenerhistory.time`、`positions.updated_at` 等）必须显式使用 `datetime.now(timezone(+8))` 强制东八区，不能依赖进程时区。

#### 1.5.4 结构化日志框架需求
需搭建结构化日志框架：JSON Lines 格式、按天轮转、保留 14 天、自动注入上下文字段（如 llm_model、component 等）、记录完整异常堆栈与关键参数，用于生产问题排查。日志路径：`backend/logs/app.log`（AI 诊断降级原因的关键数据源）。

#### 1.5.5 LLM 配置加载策略：DB 优先 + 空 env 值不覆盖
LLM 配置优先从数据库加载，DeepSeek Key 存储在 DB（非 .env），默认模型 deepseek-v4-flash；`.env` 中空字符串（如 `LLM_API_KEY=`）不应覆盖 DB 中的有效 Key。

#### 1.5.6 推理模型 max_tokens 最低配置为 8192
deepseek-v4-flash 等推理模型的 max_tokens 最低要求为 **8192**，确保思考过程与正文输出均不被截断；已写入数据库 `llm.max_tokens` 字段，并在代码中作为推理模型预算下限强制生效。

#### 1.5.7 推理模型 max_tokens 最低要求（补充）
max_tokens 至少 4096（历史版本要求），否则因 reasoning_content 占用大量 token 导致 content 截断为空、JSON 解析失败、静默降级为规则模板。**现行标准以 §1.5.6 的 8192 为准**。

#### 1.5.8 embedding 环境变量启用机制
依赖两个关键环境变量启用 embedding：`EMBEDDING_ENABLED=1`（开关）和 `EMBEDDING_API_KEY`（密钥），二者同时满足才触发实际 API 调用。

#### 1.5.9 embedding 环境变量配置
通过环境变量控制 embedding：`EMBEDDING_API_KEY` 用于认证，`EMBEDDING_ENABLED`（值为 1）用于启用功能。

#### 1.5.10 SiliconFlow embedding 运行时配置（凭据已脱敏）
通过环境变量 `EMBEDDING_API_KEY` 和 `EMBEDDING_BASE_URL` 配置 SiliconFlow embedding 服务；Base URL 为 `https://api.siliconflow.cn/v1`；**API Key 请从本地 `.env` 的 `EMBEDDING_API_KEY` 读取，勿写入仓库**。

#### 1.5.11 理杏仁 API 限流策略
理杏仁 API 限流规则：每分钟最多 1000 次请求，每秒钟最多 36 次；超限返回 HTTP 429。

#### 1.5.12 K 线缓存策略按使用场景区分
信号评估（一键分析持仓、单票评估）和交易计划生成流程中，K 线数据必须强制刷新（绕过缓存，force=True），确保盘中分析反映最新行情；扫描、回测等盘后场景保持原有 K 线缓存策略不变。

### 1.6 规则（project_rule，2 条）

#### 1.6.1 A 股交易规则（申报单位 / T+1 / 涨跌停）
- **申报数量**：科创板（688/689 开头）买入 ≥200 股、1 股递增；北交所（43/83/87/88/92 开头）买入 ≥100 股、1 股递增；主板/创业板买入须为 100 股整数倍；卖出后剩余持仓不足最小单位（碎股）时必须一次性全部卖出
- **T+1 制度**：当日买入的持仓当日不可减仓/卖出
- **涨跌停限制**：按板块 10%（主板）/20%（创业板、科创板）/30%（北交所），开盘触及涨停不买入、触及跌停不卖出
- **回测成交**：信号日 T 收盘判定，T+1 开盘价买入、T+N 收盘价卖出
- 规则统一实现在 `app/core/lot_rules.py`，被回测撮合/交易计划生成/持仓录入共用。

#### 1.6.2 删除操作必须二次确认
系统内所有删除类操作必须弹出二次确认，禁止无确认直接删除；后端接口层对删除请求保持幂等（返回 404/False 可重复调用），前端负责交互确认；确认框风格与项目 toast/Button 组件一致。

---

## 2. 开发规范（development_practice_specification，5 条）

### 2.1 需求更新与 bug 修复后及时提交推送 GitHub
每次需求更新或 bug 修复完成后，应及时执行 `git commit` 并 `git push` 到 GitHub 远端仓库，保持远端与本地代码同步，无需等待用户额外提醒。

### 2.2 空环境变量不覆盖配置的加载规则
配置加载逻辑中，环境变量为空字符串（""）应视为未设置，不得覆盖数据库或默认配置中的有效值；这是保障用户在 UI/DB 中配置的密钥不被意外清空的关键实践。

### 2.3 选股条件过滤逻辑：组内 OR、组间 AND
同一维度（如指数池、板块、行业）内多选取并集（OR），不同维度之间取交集（AND）。例如：指数池[上证50 ∪ 沪深300] ∩ 板块[科创板] ∩ 行业[数字芯片设计]。

### 2.4 理杏仁 API 调用重试要求
调用理杏仁 API 时必须实现重试机制（指数退避），以应对网络波动或临时服务异常，避免抓取中断。

### 2.5 交易术语分层说明策略
- **字段级**：Plans 页关键字段（如「日亏损熔断」「防守模式」「总仓位」「一致性」「纪律提醒」）添加 hover tooltip 一句话含义；
- **系统级**：独立「交易说明书」页面（/guide），系统化讲解风控规则、信号分类、交易模式、仓位管理、T+1、申报规则及字段解读七大模块。
tooltip 面向「正在看计划」的用户，说明书面向「想理解系统」的用户。

---

## 3. 技能经验（learned_skill_experience）

### 3.1 组合技能一：前端与交易系统工程标准化技能（10 个子技能）

#### 3.1.1 前端多页面布局一致性标准化技能
**输入**：多页面 React 项目 UI 布局（标题区、内容宽度、表格样式、控件尺寸）不一致。
**步骤**：① 全面审计所有页面与 `components/ui/` 组件，识别不一致点；② 提炼高频模式为可复用 UI 原语（`PageHeader`、`Table/Th/Td`）；③ 创建组件、`index.ts` 统一导出；④ 全局替换旧代码并修复样式冲突 bug；⑤ tsc + eslint + build + 预览验证。
**输出**：页面标题区/表格/间距完全一致，主内容区统一居中限宽，修复至少 3 个真实布局 bug。
**注意**：审计必须全覆盖；props 设计兼顾灵活性与约束性；替换时同步更新导入、移除废弃样式。

#### 3.1.2 React 前端页面统一迁移到 react-query 技能
**步骤**：① 静态数据页 `useState+useEffect` → `useQuery`；② 轮询页删 setInterval → `refetchInterval`（如 10s）；③ 任务进度页条件轮询 `refetchInterval: q => q.data?.status === 'running' ? 2000 : false`；④ 写后刷新 → `queryClient.invalidateQueries({queryKey})`；⑤ 筛选参数加入 queryKey 自动重请求；⑥ 表单编辑页 draft 仅首次填充（useRef 防覆盖）；⑦ 清理无用类型导入；⑧ tsc + build + grep setInterval 零残留验证。
**注意**：任务轮询页终态需 invalidateQueries 刷新依赖数据；「实时任务+历史回看」双模式页面分离 liveTask（useQuery）与 historyTask（local state）。

#### 3.1.3 交易系统回测功能重构设计技能
**步骤**：① 扫描关键模块源码（strategy/factor/engine/models/datasource），定位数据基础能力与缺陷根源（如腾讯 qfq 动态复权未来函数）；② 结构化「缺陷-后果」对照表，识别可复用资产（baostock adjustflag=3）；③ 设计分层演进 P0 数据地基（backtest_kline 冻结快照）→ P1 持仓回测（模式A Position 直取/模式B Trade 导入建仓腿）→ P2-P4 增强；④ 输出可执行设计文档。
**注意**：Position 模型必须含 opened_at/cost/qty/pyramid_stage 才能支撑真实持仓回测。

#### 3.1.4 项目级 UI 规则落地技能：统一二次确认对话框实现
**步骤**：① 创建 ConfirmDialog 组件，createPortal 渲染到 body，支持 title/message/confirmText/onConfirm/onCancel；② 页面中引入组件、声明确认状态、拆分「触发确认」与「执行删除」；③ 导出至 ui/index.ts。
**注意**：必须 createPortal 避免 overflow 截断；状态需覆盖所有删除类型分支；后端接口保持幂等。

#### 3.1.5 多约束股票筛选器零结果根因诊断技能
**步骤**：① 验证行业映射数据完整性（stockclassification 表）；② 模拟扫描过滤链（指数池 → load_classification_map → _filter_by_industry）；③ 量化指数池行业覆盖广度。
**输出**：明确根因是「指数池 ∩ 行业」为空集（如 sz50 仅覆盖 33/500 个三级行业）而非逻辑错误。
**注意**：行业树显示为全市场口径，与实际扫描范围不一致易误导；需 SQL 直查、Python 过滤链模拟、覆盖率统计三方交叉验证。

#### 3.1.6 新数据源 K 线能力集成技能
**步骤**：① 能力验证（官方文档/SDK/探测）；② 安全接入：声明 `supports_realtime=False`、`supports_period` 严格校验、get_kline 串行限速 + 429/5xx 指数退避 + 统一列 + date 升序；③ 配置集成：priority 插入 + `_migrate_config` 自动补入 + `_ENV_MAP` 注册 token；④ 单元测试（mock HTTP）+ 真实接口冒烟脚本。
**注意**：理杏仁无实时行情接口，需按官方 `cn/company/candlestick` 实现；`type` 参数是复权类型枚举（fc_rights/lxr_fc_rights）非周期字符串；限流必须代码层硬性遵守。

#### 3.1.7 前端 N+1 行情与后端扫描性能优化技能
**步骤**：① 后端新增批量行情接口（复用缓存与序列化）；② 前端每行独立轮询改为页面级单一定时器批量拉取，按 symbol 分发；③ 默认配置 `universe: all → sz50` + 旧配置自动迁移；④ 扫描主循环 `asyncio.gather` 分批并发（3）+ per-item sleep(0.05)；⑤ 显式传 symbols 跳过 universe 预筛；⑥ 测试 mock `ensure_universe` 为 async。
**效果**：自选页请求 N→1；sz50 池扫描分钟级→秒级。
**注意**：并发数 3~8 为宜；默认值改动必须前后端配套 + 配置迁移。

#### 3.1.8 手动触发型双源分类刷新技能
**步骤**：① 专用数据源类（不声明通用能力，仅 get_sw_classification，内置限速/退避/token 注入）；② config 注册 + `LIXINGER_TOKEN` 环境变量映射 + 根 .env 自动加载（override=False）；③ 刷新 API 支持 `source` 参数（auto|lixinger|akshare 正则校验）；④ auto 先新源后回落原逻辑，stats 标记 source；⑤ 无板块数据时 `_upsert(..., update_boards=False)` 保留旧值。
**注意**：设计「一次请求覆盖全量」（2 次请求 5544 只）避免触限额；fallback 必须显式可监控（stats.source + DB source 字段）。

#### 3.1.9 前端离散字段筛选与计数展示技能
**步骤**：① 定义筛选选项数组（'all' + 有效值 + 'none'）；② 归一化函数映射缺失/空/占位值为 'none'；③ useMemo 计算各选项命中数；④ useMemo 过滤结果；⑤ 渲染带计数的筛选按钮组；⑥ 表格数据源替换为筛选结果，序号随筛选重排。
**注意**：归一化须覆盖 undefined/null/''/占位值；useMemo 依赖需含原始数据与筛选状态。

#### 3.1.10 证券交易系统总权益动态计算技能
**步骤**：① 汇总 trade 表所有 `action='sell'` 的 `pnl`（已扣手续费净额）；② 浮动盈亏 = 持仓市值 − 持仓成本；③ 总权益 = 启动资金 + 已实现盈亏 + 浮动盈亏；④ 可用资金 = 启动资金 + 已实现盈亏 − 持仓成本。
**注意**：已实现盈亏必须扣全部手续费；持仓成本须为含费摊薄成本；严禁前端硬编码启动资金为总权益。

### 3.2 组合技能二：后端与 AI 基础设施工程治理技能（10 个子技能）

#### 3.2.1 推理模型自动扩容 token 预算技能
**步骤**：① 按模型名特征词（flash/reasoner/r1/o1/thinking）识别推理模型；② 实际传入 max_tokens 设为 `max(配置值, 4096)`（自动提升下限，不覆盖用户显式大值）；③ 验证 content 非空可解析。
**注意**：推理模型典型表现 content 空、reasoning_content 长、finish_reason=length；适用于所有 OpenAI 兼容推理模型。

#### 3.2.2 env 空字符串不覆盖 DB 配置修复技能
**步骤**：① 查 DB 配置非空；② 查运行时配置为空定位覆盖点；③ 审查 `_apply_env_overrides` 对 `val == ""` 未跳过；④ 改为 `if val is None or val == "":`；⑤ 补充回归测试（monkeypatch.setenv 空值）。
**注意**：保持 env > DB 优先级不变（非空 env 仍覆盖）；空字符串语义 = 「未设置」而非「清空」。

#### 3.2.3 langchain 1.x ChatPromptTemplate 调用修复技能
**步骤**：① 捕获 `Invalid input type <ChatPromptTemplate>` 异常；② 调用前插入 `prompt.format_messages()` 转为消息列表；③ 同步更新 FakeLLM 测试桩 ainvoke 签名接收 messages；④ 补充测试。
**注意**：FakeLLM 不校验输入类型，此问题单测不暴露，必须真实链路冒烟。

#### 3.2.4 SQLModel 实体返回前调用 session.expunge 避免 DetachedInstanceError
**步骤**：commit()+refresh() 后立即 `session.expunge(row)`，使对象脱离 session 管理。
**注意**：必须在 session 关闭前（with session_scope() 块内）执行；适用于返回实体的异步上下文（pytest、FastAPI response）。

#### 3.2.5 Python 后端数据库时间戳强制东八区修复技能
**步骤**：① 探针脚本打印本地/UTC/_now()/DB 字段，确认 UTC 存储；② Grep `datetime.now()`/`_now()`/`strftime` 定位所有生成点；③ 统一 `datetime.now(timezone(timedelta(hours=8)))`；④ 存量数据 `UPDATE ... datetime(time, '+8 hours')`；⑤ 防回归测试断言小时差 7.5–8.5。
**注意**：计算类时间（缓存新鲜度、持仓天数）无需改；Dockerfile `TZ=Asia/Shanghai` 不保证进程时区，必须代码层强制。

#### 3.2.6 为 AI 分析服务新增时间范围支持技能
**步骤**：① `_scope_range` 添加新分支返回 ISO 日期元组；② API 请求模型注释同步；③ 前端 select 新增 option（按语义顺序插入）；④ 边界测试 + 集成测试（构造跨范围数据验证排他性）。
**注意**：语义明确（如 day = 当日 00:00–23:59 非最近 24h）；前端 value 与后端参数严格一致。

#### 3.2.7 代码驱动型文档同步技能
**步骤**：① `git log --oneline -20` + status 获取变更摘要；② `grep -r '@router\.'` 提取 API 路由；③ 读 config.py/manager.py 定位开关与 env 映射；④ 核对 Dockerfile/compose 端口；⑤ 综合更新 README/.env.example。
**注意**：API 路由 + 配置读取逻辑 + Git 历史三者一致才写入；.env.example 开关变量必须在代码有对应 os.getenv 读取。

#### 3.2.8 SiliconFlow embeddings OpenAI 兼容客户端技能
**步骤**：① EmbeddingClient 实例（base_url/api_key/model/timeout_sec）；② `.embed(texts)` 自动分批（BATCH=8）、空串替换占位符「无内容」、httpx.AsyncClient POST /embeddings；③ 解析 `data[i].embedding`；④ 余弦相似度 `a @ b / (norm(a)*norm(b))`。
**注意**：请求体 `{"input": [...], "model": "..."}` + `Authorization: Bearer <key>`；API 拒绝空 input；中文优先 BAAI/bge-m3（8192 token，1024 维）。

#### 3.2.9 架构健康静态审查技能
**步骤**：① Glob 扫描前后端结构；② 精读架构锚点文件（main/db/config/scheduler、main.tsx/App.tsx/client.ts）；③ Grep 高风险模式（setInterval/Session/PRAGMA/HTTPException 等）；④ 跨层关联对比（前端轮询 vs 后端批量 API）；⑤ 严重度×修复成本排序 P0/P1/P2；⑥ 输出 Markdown 报告（位置/现状/风险/建议）。
**注意**：纯静态走读不依赖运行时；优先关注架构一致性与隐性耦合；性能问题必须量化。

#### 3.2.10 SPA 文档网站 API 接口逆向解析技能
**步骤**：① 测基础域名连通性；② 从 JS bundle 提取 OPEN_API_KEYS 定义块；③ 解析 `"KEY":"".concat(...)` 映射常量枚举；④ 键名解析为真实路径；⑤ 用 token 实测验证。
**输出**：实测验证的接口清单（路径/参数/返回示例）。
**注意**：域名 `https://open.lixinger.com/api`；文档路径与接口路径一一对应。

### 3.3 独立技能（5 条）

#### 3.3.1 多子目录 Python 项目虚拟环境统一标准化技能
**步骤**：① 验证候选 venv 完整性（导入核心依赖 + 检查 uvicorn 版本）；② 确认无硬编码引用 + .gitignore 排除冗余环境；③ 重写 README 明确唯一 venv（backend/.venv）与启动命令；④ 同步更新终端面板/CLI 调用路径；⑤ 配置说明补充日志路径与轮转策略；⑥ 验证 AI 功能不依赖被删 venv。
**注意**：Windows 删除被占用 .venv 前必须终止进程；统一后所有 pip install 必须在唯一 venv 内；日志路径 `backend/logs/app.log` 是 AI 降级诊断关键数据源。

#### 3.3.2 申万三级行业树形选择实现技能
**步骤**：① 后端按 l1→l2→l3 分组计数构建嵌套字典树；② GET API 返回树（name/count/children）；③ 前端递归 TreeRow 组件（depth 缩进/箭头展开/复选框多选）；④ Set 管理展开节点、数组管理已选节点。
**注意**：children 缺省即叶子；测试需 patch 分类映射 mock 确保 sw_l2/sw_l3 存在。

#### 3.3.3 A 股交易所申报规则跨模块统一技能
**步骤**：① 定位所有申报数量逻辑模块（grep '200'/'688'/'round_buy'）；② 提取共性到 `lot_rules.py`（min_buy_unit/round_buy_qty/sell_qty）；③ 替换分散实现为统一导入；④ 计划生成器加仓 round_buy_qty 取整、减仓 sell_qty、附 board_note；⑤ 持仓录入 open_or_add 校验合规否则抛异常；⑥ 测试同步改合规值并新增规则覆盖。
**注意**：板块前缀映射：科创板 688/689、北交所 43/83/87/88/92、主板/创业板其余。

#### 3.3.4 可热启停 AI 模块的零侵入设计模式
**步骤**：① DEFAULT_CONFIG 新增模块配置段（enabled 总开关）；② 独立模块包仅 import 复用现有服务；③ config_manager.listeners 回调动态增删 APScheduler 任务；④ configSchema.ts 声明字段类型自动渲染开关；⑤ LLM 不可用降级规则模板。
**注意**：模块是「编排器而非实现者」，禁止复制粘贴代码；开关变更必须 config_manager.update() 写 DB；通知统一走 notify.py 抽象层。

#### 3.3.5 framer-motion v13 AnimatePresence mode=wait 自定义组件退出修复技能
**步骤**：① 组件导入 usePresence；② `const [isPresent, safeToRemove] = usePresence()`；③ useEffect 监听 !isPresent，退出动画时长（0.2s）后调 safeToRemove；④ 移除 motion() 包装，直接返回带 initial/animate/exit 的 motion.div。
**注意**：safeToRemove 延迟必须 ≥ exit.transition.duration，否则提前挂载导致闪烁/状态错乱；v13 官方推荐替代 motion() 包装方式。

---

## 4. 坑点（common_pitfalls_experience，13 条）

### 4.1 SQLModel 单列查询返回标量而非行对象
`select(BacktestKline.date)` 返回标量 `str` 而非行对象，不能用 `r.date` 访问。正确做法：需标量直接用 `str(r)`；需行对象用 `select(BacktestKline).where(...)`。

### 4.2 多目录 Python 项目需 cd 到 backend 才能导入 app 包
`from app import db` 报 `ModuleNotFoundError`，因当前工作目录不在 backend/ 下。必须先 `cd backend` 再执行 Python 命令。

### 4.3 双 venv 混用致运行进程找不到依赖
**根因**：项目曾存在两个 .venv（根 + backend），进程用「系统 Python + 根.venv uvicorn」启动，sys.path 混搭；依赖装进 backend/.venv 后运行进程 import langchain_core 失败 → AI 静默降级，而本地脚本正常，极具迷惑性。
**修复**：用环境探测日志确认运行进程真实 Python 与 site-packages，把依赖装进实际使用的环境；pip 安装指定目标 python。
**教训**：排查「代码/配置都对但运行异常」先确认 sys.executable 与 sys.path。**现状（2026-08-10）**：已统一为 backend/.venv，根 .venv 已删。

### 4.4 运行进程使用项目根 .venv 而非 backend/.venv
（历史问题，已被 §4.3 统一策略解决）运行进程实际用根 .venv 时，仅装在 backend/.venv 的依赖报 ModuleNotFoundError；现统一为唯一 backend/.venv。

### 4.5 eslint-plugin-react-hooks v6 flat 配置路径为 configs['flat/recommended']
v6 的 flat 配置路径是 `configs['flat/recommended']`，不是 v7 的 `configs.flat.recommended`；错误引用报 `TypeError: Cannot read properties of undefined (reading 'recommended')`。

### 4.6 推理模型 max_tokens 预算不足致正文为空
**根因**：推理模型先输出大段 reasoning_content（可达 5000+ 字符）再出正文，max_tokens 预算包含思考过程；默认 2000 被吃光后 finish_reason=length、content 空 → 解析失败 → 日报/复盘静默降级为规则模板，前端显示「未启用 LLM」极具误导。普通模型无此问题。
**修复**：build_chain_llm 按模型名特征（flash/reasoner/r1/o1/o3/thinking/pro）识别推理模型，max_tokens 提升下限 4096（现行标准 8192，见 §1.5.6）。

### 4.7 langchain 1.x ainvoke 不接受 ChatPromptTemplate
`llm.ainvoke(ChatPromptTemplate)` 在 langchain-core 1.x 抛 `Invalid input type ... Must be a PromptValue, str, or list of BaseMessages`。修复：调用前 `prompt.format_messages()`。FakeLLM 单测无法暴露此问题，真实链路冒烟不可省；ai_review/chain.py 与 report/chain.py 共用 `_call_parsed`，修一处两边生效。

### 4.8 .env 空字符串覆盖 DB 配置导致 Key 丢失
`_apply_env_overrides` 中空字符串不是 None 照样执行覆盖，`.env` 留空变量（如 `LLM_API_KEY=`）把 DB 中页面保存的 Key 抹掉，表现为「页面配置了 Key，重启后 AI 静默降级」。修复：`if val is None or val == "": continue`（_ENV_MAP 主循环与 ENABLE_* 开关循环都要改）。

### 4.9 前端 undefined 导致后端回退配置默认 sz50 的传参语义 bug
前端未选指数池传 `universe=undefined`，后端 `uni_name = universe or 配置默认 or 'all'` 回退 sz50，造成「全A+光学光电子」实际扫描 `sz50 ∩ 光学光电子 = 0`。修复：前端显式传 `universe='all'`（Screener.tsx）。

### 4.10 embedding.enabled 环境变量需显式映射到 _ENV_MAP
`EMBEDDING_ENABLED` 未在 `_ENV_MAP` 声明导致 .env 设置不生效，默认 `llm.embedding.enabled=False` 静默禁用。修复：`_ENV_MAP` 添加 `'EMBEDDING_ENABLED': 'llm.embedding.enabled'`。

### 4.11 langchain-core 1.x OutputParserException 导入路径变更
1.x 中 OutputParserException 已从 `langchain_core.output_parsers.base` 移至 `langchain_core.exceptions`，旧路径导入被 try/except 静默吞掉表现为 CHAIN_AVAILABLE=False。

### 4.12 ChatPromptTemplate 避免 f-string 解析 JSON 花括号
`ChatPromptTemplate.from_messages([('system', s), ('user', u)])` 会把 user 字符串按 f-string 解析，提示词含 JSON 花括号报 `ValueError: Invalid format specifier`。正确：传 Message 对象 `[SystemMessage(content=s), HumanMessage(content=u)]`。

### 4.13 langchain-core 1.x 集成两个易踩坑
（§4.11 + §4.12 汇总）① OutputParserException 导入路径变更；② from_messages 元组默认 f-string 模板解析，含 JSON 花括号抛 Nested replacement fields。集成 langchain 前先用小脚本验证 import 路径与模板渲染；判定边界：langchain-core>=1.0。

---

## 5. 关键决策（important_decision_experience，4 条）

### 5.1 回测数据通道采用前复权冻结快照而非后复权
**结论**：回测专用通道（backtest_kline 表 + baostock 区间拉取）统一使用**前复权(qfq)冻结快照**：区间拉取后同 (symbol, period, adjust, date) 行不再覆盖，与实盘 kline_cache 完全隔离。
**权衡**：未来函数风险只来自日常增量刷新；冻结后区间内价格恒定，结果与后复权等价；且所有数据源与券商 APP 默认前复权，口径统一。代价：快照后大比例分红送转时与实盘最新价有换算差（区间内结论不受影响）。
**被拒**：后复权标准口径（需单独通道且与实盘割裂）；复用实盘 kline_cache（动态前复权 + 800 根上限，v2 诊断为「不准确」根因）。
**失效条件**：用户要求与除权后实盘价严格对齐（如信号审计逐笔对价）时需 hfq 或除权因子校正。

### 5.2 盘后 AI 日报模块设计与降级策略
**结论**：scheduler 16:30（mon-fri）或 POST /api/report/daily/run 手动触发；DailyReportService 组装「市况/今日操作/持仓(止损线+ATR止盈档)/今日信号/风控/纪律诊断」素材 → LLM 链（复用 ai_review.chain 的 _call_parsed/build_chain_llm + 记忆检索注入）→ 结构化 DailyReportOutput 落库 DailyReport（date 唯一，同日覆盖）→ 站内 Notification + 可选企业微信 webhook。LLM 失败降级规则模板（status=degraded），任一步失败不中断。前端 Dashboard 日报卡片。
**权衡**：日报只读不产生配置变更（调参闭环留在复盘）；明日关注仅覆盖持仓+自选（用户限定）；持仓止盈档用 ATR 动态（失败降级 fixed）；素材与 LLM 输出双留存便于调试。
**被否**：全市场 screener 观察池（用户否定）；日报复用调参闭环（定位是行动清单不是诊断）。

### 5.3 AI 复盘升级 LangChain 两步链并保留降级路径
**结论**：AI 复盘从单步 prompt 升级为两步链（Step1 行为特征归纳 → Step2 基于特征生成建议），Pydantic 结构化输出替代手工 JSON 解析，解析失败带错误信息自动重试一次；依赖缺失自动降级旧单步路径 `_llm_review_legacy`，LLM 失败由 run() 降级纯规则诊断。入口：`ReviewService._llm_review` 优先 `chain.run_review_chain`。
**权衡**：两步链多一次 LLM 调用（token 约 2 倍），换来更高质量行为归因与结构化输出；参数调优护栏留在 service 层统一把关。
**被否**：旧单步路径作默认 + 两步链做开关（新链自带完整降级链路）。依赖：langchain-core>=1.0 + langchain-openai>=1.0 已入 requirements.txt。

### 5.4 AI 复盘记忆 RAG 实现与效果归因设计
**结论**：历史 AiReview+ConfigChange → 文本化记忆条目（问题/建议/采纳状态/效果归因「胜率 X→Y」）→ SiliconFlow BAAI/bge-m3 embedding（1024 维）→ ReviewMemory 表存向量 → 两步链 Step2 余弦检索 top3 注入；规则「已采纳且无效的建议不重复提，有效则引用效果」。
**权衡**：不引入向量库，numpy 全量余弦检索（几十条毫秒级）；embedding 未配置/失败静默降级空记忆；索引失败只记日志不阻塞主流程。
**被否**：向量库 + 增量索引（数据量不达阈值）；每复盘后自动补建单条索引（幂等）+ rebuild_index 批量补建历史。
**失效条件**：复盘频率过低时记忆只有问题无效果；切换 embedding 模型后旧向量维度不一致被余弦过滤跳过，需重建索引。

---

## 6. 用户行为约定（user_behavior，1 条）

### 6.1 AI 配置禁止主动修改，只提示给选项
AI/LLM 相关配置（模型、API Key、开关、base_url，含 .env 与页面配置）是**用户禁区**：即使发现配置异常（如 env 覆盖导致页面配置的模型不生效），也只能向用户说明事实、给出选项，由用户决定后按其指示操作，绝不代改。

---

*本文档由 IDE 记忆库导出生成（共 80 条记忆）。新增/变更记忆时请同步更新本文档并随代码提交。*
