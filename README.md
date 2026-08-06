# Momentum Trader — 个人动量趋势交易系统

> 给一个人的「交易副驾」:自动盯盘 + 出策略 + 给人话操作指引 + 事后复盘打分。
> **不接实盘下单**,只做「选股 → 信号 → 计划 → 指引 → 记录 → 复盘 → 评分」全链路。

## 功能特性

- 多数据源自动切换(mootdx / 腾讯财经 / 东方财富 / AKShare),健康分熔断兜底
- 纯 pandas 指标库(MA/MACD/RSI/ADX/ROC/布林/唐奇安/ATR/量比)
- 五类交易信号:首仓 / 加仓 / 减仓 / 止损 / 做T
- 金字塔加仓 + 分批止盈 + 凯利仓位建议
- 三道风控闸门:日亏损熔断 / 连亏降仓 / 回撤防守模式
- 人话交易计划 + 手动回填成交 + 双写日志(CSV + SQLite)
- 历史回顾(盈亏曲线 / 月度热力图 / 信号分布)+ 交易评分
- AI 复盘:规则诊断先行,LLM 兜底(兼容 OpenAI 协议,可接 DeepSeek/Ollama 等)
- rich 终端面板 + Web 前端(Vite + React)

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | FastAPI + Uvicorn(端口 **8000**) |
| 前端 | Vite 5 + React 18 + TS(开发 **5173**,构建产物由后端托管) |
| 数据 | SQLite(SQLModel)+ CSV 双写 |
| 定时 | APScheduler |
| 容器 | Docker 多阶段构建 + docker-compose |

## 快速开始

### ① Docker 一键(推荐)

```bash
cp .env.example .env        # 按需填 LLM key / 数据源开关
docker compose up -d --build
# 打开 http://localhost:8000
```

### ② 本地开发

```bash
# 后端(需 Python 3.11+)
cd backend
pip install -r requirements.txt -r requirements-dev.txt
pip install -e ".[optional]"        # 可选: mootdx / akshare 数据源
uvicorn app.main:app --reload --port 8000

# 前端(需 Node 18+)
cd frontend
npm install
npm run dev                         # http://localhost:5173
```

### ③ 终端面板(前端不可用时的兜底)

```bash
cd backend && python -m cli.panel
```

## 数据源与限流声明(重要)

- 默认优先级:**mootdx > 腾讯财经 > 东方财富 > AKShare**,按健康分自动切换
- 东方财富接口存在**连接级间歇风控**:本系统已内置降频(≥2s/请求)、串行、指数退避、
  UA + NID 补丁、熔断(连续 3 次风控失败熔断 10 分钟)、可选代理池轮换
- 公共通达信服务器(mootdx)偶发不可达,属正常现象,系统会自动切换备用源
- 全市场扫描约 5000 只,盘后定时批量执行,白天仅增量刷新自选

## 配置说明

- 环境变量:见 `.env.example`(LLM key / 数据源开关 / TZ)
- 全局参数:启动后在 Web 端「设置」页修改(趋势/动量/量能/风控/仓位/做T/评分权重),热生效
- 配置读取优先级:**env > 数据库配置 > 默认值**

## 目录结构

```
backend/    FastAPI 应用(api/ core/ models/ services/ cli/ tests/)
frontend/   Vite + React 前端
.github/    CI / 镜像发布 workflow
docs/       文档
Dockerfile / docker-compose*.yml / Makefile / pyproject.toml
```

## 免责声明

本系统仅供学习研究,不构成任何投资建议。投资有风险,入市需谨慎。
数据来源于第三方公开接口,可能存在延迟或中断,请自行核验。

## License

Apache-2.0,见 [LICENSE](LICENSE)。
