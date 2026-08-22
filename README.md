# Momentum Trader — 个人动量趋势交易系统

> 给一个人的「交易副驾」:自动盯盘 + 出策略 + 给人话操作指引 + 事后复盘打分。
> **不接实盘下单**,只做「选股 → 信号 → 计划 → 指引 → 记录 → 复盘 → 评分 → 调参 → 回测」全链路闭环。

## 功能特性

- 六数据源自动切换(mootdx / 腾讯财经 / baostock / 东方财富 / AKShare / 理杏仁),健康分熔断 + 优先级自愈兜底
- 纯 pandas 指标库(MA/MACD/RSI/ADX/ROC/布林/唐奇安/ATR/量比)
- 五类交易信号:首仓 / 加仓 / 减仓 / 止损(结构离场 + 亏损区静态线 + 浮盈移动止损)/ 做T + 趋势阶段识别(启动加分 / 加速期细分前·中·后期 / 过热·衰竭扣分,强趋势死动量自动折扣)
- 市况分类器驱动的多交易模式(趋势强攻 / 趋势回踩 / 震荡 / 防守),金字塔加仓 + 分批止盈(ATR 自适应)+ 凯利仓位建议
- 风控闸门:日亏损熔断 / 连亏降仓 / 回撤软防守(防守期仓位上限减半,回撤修复至限额一半自动解除)+ 止损冷却期(同票止损后 N 个交易日内禁止再入场,防连环接刀;回测验证 +29.5pp 收益)
- A 股规则合规:T+1、申报单位(科创板 200 股起 / 主板 100 整数倍)、涨跌停
- 人话交易计划 + 手动回填成交 + 双写日志(CSV + SQLite)
- 历史回顾(盈亏曲线 / 月度热力图 / 信号分布)+ 交易评分
- AI 复盘:规则诊断先行,LLM 两步链兜底(LangChain 结构化输出,兼容 OpenAI 协议,可接 DeepSeek/Ollama 等);
  复盘记忆 RAG(历史建议 embedding 入库,相似经验注入)+ 建议采纳一键调参闭环(白名单 + 三道闸门校验 + 可回滚)
- 得分追踪:选股结果一键入池,每日自动采样 2 次得分轨迹(午间/盘后),内置模拟交易状态机(10 万元/笔,持仓视角评估信号);衰竭期自动归档结算,30 天期满/手动终止同样结算,历史成绩单对比持有收益 vs 模拟收益;过热期仅预警不归档
- 回测中心五模块:持仓回测 / 信号审计 / 策略对比回测(同池同种子多风控变体消融,冷却×防守开关矩阵,回测区间可选,异步任务)/ 阶段·得分双维分桶因子回测 / K线缓存管理(新鲜度统计 + 增量补拉 + 回测快照预热)
- 选股增强:申万三级行业树形筛选 / 指数池多选 / 条件组合预设 / 基本面 + 业绩事件因子 / 择时闸门 / 行业限配 / 扫描历史持久化回看 / 信号快览弹窗(批量扫描 Top30 出信号徽标,键盘翻票,弹窗内直连生成计划)
- rich 终端面板 + Web 前端(Vite + React)

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | FastAPI + Uvicorn(端口 **8002**) |
| 前端 | Vite 5 + React 18 + TS + TanStack Query(开发 **5175**,构建产物由后端托管) |
| 数据 | SQLite(SQLModel)+ CSV 双写 + K线/行情内存缓存 |
| 定时 | APScheduler(盘后自选/持仓 K 线预热等) |
| AI | langchain-core / langchain-openai 两步链 + SiliconFlow embedding RAG(可选,缺失自动降级) |
| 容器 | Docker 多阶段构建 + docker-compose |

## 快速开始

### ① Docker 一键(推荐)

```bash
cp .env.example .env        # 按需填 LLM key / 理杏仁 token / 数据源开关
docker compose up -d --build
# 打开 http://localhost:8002
```

### ② 本地开发(统一使用 `backend/.venv`, 勿混用多环境)

```bash
# 0) 首次: 创建后端虚拟环境(全项目只维护这一个; 已存在则跳过)
cd backend
python -m venv .venv

# 1) 安装依赖(核心 + 开发 + 可选数据源)
pip install -r requirements.txt -r requirements-dev.txt
pip install -e ".[optional]"        # 可选: mootdx / akshare 数据源

# 2) 启动后端(在 backend 目录下, 端口 8002) —— 二选一
# 方式 A(推荐): 激活 venv 后直接敲, 每个新终端窗口激活一次
.venv\Scripts\Activate.ps1         # Windows PowerShell(CMD: .venv\Scripts\activate.bat)
source .venv/bin/activate           # Linux / macOS
uvicorn app.main:app --reload --port 8002
# 方式 B(不激活): 显式指定 venv 的 Python(注意 PowerShell 需 .\ 前缀)
.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8002

# 3) 前端(另开终端, 需 Node 18+)
cd frontend
npm install
npm run dev                         # http://localhost:5175
```

> ⚠️ **环境一致性(重要)**: 全项目**只使用 `backend/.venv` 一个虚拟环境**。
> - 不要用系统 Python 直接运行项目, 也不要在项目根目录保留第二个 `.venv`(历史遗留, 依赖不完整会导致 AI 功能静默降级为规则模板)。
> - 依赖变更后只需在 `backend/.venv` 里 `pip install -r requirements.txt`。
> - 日志输出: `backend/logs/app.log`(JSON Lines, 按天轮转保留 14 天, 可用 `LOG_DIR`/`LOG_LEVEL` 调整)。

### ③ 终端面板(前端不可用时的兜底)

```bash
cd backend
.venv\Scripts\python.exe -m cli.panel
```

## 界面预览

> 截图存放于 `docs/screenshots/`(自行截图替换), 或直接启动后访问 http://localhost:8002 体验。

| 页面 | 说明 |
|------|------|
| 仪表盘 | 自选/持仓实时行情(批量接口)、风控状态、数据源健康、盈亏总览 |
| 选股 | 三因子全市场扫描,统一条件面板(指数池多选 / 板块 / 申万三级行业树),加速期前/中/后期细分筛选,条件组合预设,基本面 + 业绩事件因子,历史扫描回看,信号快览弹窗(批量看信号 + 键盘翻票) |
| 自选与持仓 | 录入持仓(代码自动带出名称)、加仓、减仓、清仓 |
| 追踪 | 得分追踪:入池自动采样得分轨迹与模拟交易,衰竭自动归档结算;历史档成绩单(持有 vs 模拟收益、操作统计、得分曲线) |
| 信号中心 | 单/多代码评估、持仓一键分析(持仓感知: 加仓/减仓/止损)、趋势阶段与市况模式展示 |
| 交易计划 | 生成人话操作指引(遵守申报单位规则),标记已执行/已忽略 |
| 交易日志 | 成交明细(双写 CSV+SQLite)、手动回填、CSV 导出 |
| 历史回顾 | 盈亏曲线/月度热力图/信号分布/单笔评分/健康度 |
| AI 复盘 | 规则诊断 + LLM 两步链深度复盘(可接 DeepSeek/通义/Ollama),建议一键采纳调参、参数变更可回滚 |
| 回测中心 | 五模块:持仓回测 / 信号审计 / 策略对比回测(风控变体消融,同池同种子,回测区间可选)/ 阶段·得分分桶 / K线缓存管理(增量补拉 + 快照预热),异步任务含 T+1 / 申报单位 / 风控闸门 |
| 设置 | 全局参数热生效修改(趋势/动量/量能/风控/仓位/做T/交易模式/趋势阶段/评分权重等) |

## 数据源与限流声明(重要)

- 默认优先级:**mootdx > 腾讯财经 > baostock > 东方财富 > 理杏仁 > AKShare**,按健康分自动切换;新增源自动补入队尾(自愈),近期成功源临时置顶
- 东方财富接口存在**连接级间歇风控**:本系统已内置降频(≥2s/请求)、串行、指数退避、UA + NID 补丁、熔断(连续 5 次风控失败熔断 10 分钟)、可选代理池轮换
- 公共通达信服务器(mootdx)偶发不可达,属正常现象,系统会自动切换备用源
- baostock:免费无风控,日线更稳,用于选股池(指数成分)/基本面/业绩事件;不支持分钟线(自动跳过)
- 理杏仁(付费 API):限流 1000 次/分、36 次/秒,超限 429 已内置指数退避重试;申万 2021 分类刷新仅 2 次请求、手动触发;日线 K 线(前复权)作最终兜底,前 4 源健康时不会被调用
- 全市场扫描默认上证 50 池(可切换全市场),并发 3 + 降压间隔;盘后定时预热自选/持仓 K 线,白天仅增量刷新

## 配置说明

- 环境变量:见 `.env.example`(LLM key / Embedding / 理杏仁 token / 数据源开关 / 东财风控 / 日志 / TZ)
- 日志:`backend/logs/app.log`(JSON Lines,按天轮转保留 14 天),`LOG_DIR`/`LOG_LEVEL` 可调;LLM 降级原因带完整异常堆栈
- 全局参数:启动后在 Web 端「设置」页修改(趋势/动量/量能/风控/仓位/做T/交易模式/评分权重/择时闸门/行业限配/基本面/业绩事件/趋势阶段),热生效
- AI 调参:复盘建议可一键采纳(仅白名单数值参数,经三道闸门校验),变更记录可回滚
- 配置读取优先级:**env > 数据库配置 > 默认值**

## 文档

- [信号中心-信号分析逻辑](docs/信号中心-信号分析逻辑.md)
- [回测中心-回测功能设计方案](docs/回测中心-回测功能设计方案.md)
- [多数据源备用机制分析](docs/多数据源备用机制分析.md)
- [架构审查报告-代码规范与性能问题](docs/架构审查报告-代码规范与性能问题.md)

## 目录结构

```
backend/    FastAPI 应用(api/ core/{datasource,indicators,signals,risk,screener,backtest,ai_review,modes,...} cli/ tests/)
frontend/   Vite + React 前端
docs/       设计文档
.github/    CI / 镜像发布 workflow
Dockerfile / docker-compose*.yml / Makefile / pyproject.toml
```

## 免责声明

本系统仅供学习研究,不构成任何投资建议。投资有风险,入市需谨慎。
数据来源于第三方公开接口,可能存在延迟或中断,请自行核验。

## License

Apache-2.0,见 [LICENSE](LICENSE)。
