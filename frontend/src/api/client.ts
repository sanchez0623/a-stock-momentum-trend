// 后端 API 统一封装: 响应 {code, msg, data}
const BASE = '/api'

export async function request<T = unknown>(path: string, init?: RequestInit, timeoutMs?: number): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
    // 超时中断: 后端进程崩溃时(uvicorn --reload 监督进程仍占端口), 请求会无限挂起,
    // 必须有客户端超时才能把"后端已死"暴露给 UI. 默认不启用(慢接口如预热不受影响).
    signal: timeoutMs ? AbortSignal.timeout(timeoutMs) : undefined,
  })
  if (!resp.ok) {
    // 透传后端错误详情(如 FastAPI 400 的 detail), 避免只显示 HTTP 400 无法定位原因
    let detail = ''
    try {
      const b = await resp.json()
      detail = b?.detail ?? b?.msg ?? ''
    } catch {
      /* 非 JSON 响应, 忽略 */
    }
    throw new Error(detail || `HTTP ${resp.status}`)
  }
  const body = await resp.json()
  if (body.code !== 0) {
    throw new Error(body.msg || '请求失败')
  }
  return body.data as T
}

export const api = {
  health: () => request<HealthData>('/health'),
  config: () => request<Record<string, unknown>>('/config'),
  configDefaults: () => request<Record<string, unknown>>('/config/defaults'),
  updateConfig: (config: Record<string, unknown>) =>
    request<Record<string, unknown>>('/config', { method: 'PUT', body: JSON.stringify({ config }) }),
  dataSourceStatus: () => request<SourceStatus[]>('/data-sources/status'),
  testSource: (name: string) => request(`/data-sources/test/${name}`, { method: 'POST' }),
  quote: (symbol: string) => request<Quote>('/quote/' + symbol),
  /** 批量实时行情(自选页用): 一次请求多只, 替代每行独立轮询 */
  quoteBatch: (symbols: string[]) =>
    request<Quote[]>('/quote/batch', { method: 'POST', body: JSON.stringify({ symbols }) }),
  kline: (symbol: string, period = 'daily', count = 120) =>
    request<KlineData>(`/kline/${symbol}?period=${period}&count=${count}`),
  indicators: (symbol: string, period = 'daily') =>
    request<IndicatorData>(`/indicators/${symbol}?period=${period}`),

  // 自选股
  watchlist: () => request<WatchlistItem[]>('/watchlist'),
  addWatch: (symbol: string, name = '') =>
    request<{ symbol: string }>('/watchlist', { method: 'POST', body: JSON.stringify({ symbol, name }) }),
  removeWatch: (symbol: string) => request(`/watchlist/${symbol}`, { method: 'DELETE' }),

  // 持仓
  positions: () => request<Portfolio>('/positions'),
  addPosition: (p: { symbol: string; name?: string; qty: number; price: number; reason?: string; action?: string; force?: boolean }) =>
    request('/positions', { method: 'POST', body: JSON.stringify(p) }),
  positionDetail: (symbol: string) => request<PositionDetail>(`/positions/${symbol}`),
  updatePositionTime: (symbol: string, opened_at: string) =>
    request(`/positions/${symbol}`, { method: 'PATCH', body: JSON.stringify({ opened_at }) }),

  // 资金账户
  account: () => request<AccountInfo>('/account'),
  updateAccount: (start_capital: number) =>
    request<AccountInfo>('/account', { method: 'PUT', body: JSON.stringify({ start_capital }) }),

  // 信号
  signals: (symbol?: string, limit = 50) => {
    const q = new URLSearchParams()
    if (symbol) q.set('symbol', symbol)
    q.set('limit', String(limit))
    return request<SignalRecord[]>(`/signals?${q.toString()}`)
  },
  evaluateSignal: (symbol: string) => request<EvaluateResult>(`/signals/evaluate/${symbol}`, { method: 'POST' }),
  evaluateBatch: (symbols: string[]) =>
    request<EvaluateResult[]>(
      '/signals/evaluate-batch',
      { method: 'POST', body: JSON.stringify({ symbols }) },
    ),

  // 交易计划
  generatePlan: (symbol: string, name = '') =>
    request<PlanRecord | null>('/plan/generate', { method: 'POST', body: JSON.stringify({ symbol, name }) }),
  currentPlans: () => request<PlanRecord[]>('/plan/current'),
  planStatus: (id: number, status: 'done' | 'ignored') =>
    request<PlanRecord>(`/plan/${id}/status`, { method: 'PUT', body: JSON.stringify({ status }) }),

  // 风控
  riskStatus: () => request<RiskStatus>('/risk/status'),
  riskReset: () => request<RiskStatus>('/risk/reset', { method: 'POST' }),
  // 选股
  screenerRun: (
    market = 'all',
    topN = 30,
    board?: string,
    industry?: string,
    universe?: string,
    opts?: { perIndustry?: number; industryLevel?: string; applyGate?: boolean; applyFactors?: boolean },
    resumeTaskId?: string,
  ) => {
    const q = new URLSearchParams({ market, top_n: String(topN) })
    if (board) q.set('board', board)
    if (industry) q.set('industry', industry)
    if (universe) q.set('universe', universe)
    if (opts?.perIndustry && opts.perIndustry > 0) q.set('per_industry', String(opts.perIndustry))
    if (opts?.industryLevel) q.set('industry_level', opts.industryLevel)
    if (opts?.applyGate === false) q.set('apply_gate', 'false')
    if (opts?.applyFactors === false) q.set('apply_factors', 'false')
    if (resumeTaskId) q.set('resume_task_id', resumeTaskId)
    return request<{ task_id: string }>(`/screener/run?${q.toString()}`, { method: 'POST' })
  },
  screenerResult: (taskId: string) => request<ScreenerTask>(`/screener/result?task_id=${taskId}`),
  screenerLatest: () => request<ScreenerTask | null>('/screener/result/latest'),
  screenerInterruptedTasks: () => request<{ items: InterruptedTask[] }>('/screener/tasks/interrupted'),
  deleteScreenerTask: (taskId: string) => request(`/screener/tasks/${taskId}`, { method: 'DELETE' }),
  screenerHistory: (page = 1, pageSize = 20) =>
    request<{ items: ScreenerHistoryItem[]; total: number; page: number; page_size: number }>(`/screener/history?page=${page}&page_size=${pageSize}`),
  screenerHistoryDetail: (id: number) => request<ScreenerHistoryItem>(`/screener/history/${id}`),
  deleteScreenerHistory: (id: number) => request<{ id: number }>(`/screener/history/${id}`, { method: 'DELETE' }),
  /** 选股池(指数成分股)缓存概况: key=指数标识, value=数量/更新时间/名称 */
  universeStats: () => request<UniverseStats>('/screener/universe/stats'),
  /** 可选行业列表(合并东财行业+申万一级, 按股票数降序) */
  screenerIndustries: () => request<{ items: IndustryItem[]; total: number }>('/screener/industries'),
  /** 申万三级行业树(一级->二级->三级, 每级带股票数), 供选股页树形多选 */
  screenerIndustryTree: () => request<{ items: IndustryNode[]; total: number }>('/screener/industries/tree'),
  screenerPresets: () => request<{ items: ScreenerPreset[] }>('/screener/presets'),
  saveScreenerPreset: (p: { name: string; universe?: string; board?: string; industry?: string }) =>
    request<{ id: number }>('/screener/presets', { method: 'POST', body: JSON.stringify(p) }),
  deleteScreenerPreset: (id: number) => request<{ id: number }>(`/screener/presets/${id}`, { method: 'DELETE' }),

  // ---------------------------------------------------------------- 得分追踪(选股结果一键追踪, 每日3次采样)
  trackingAdd: (body: { symbol: string; name?: string; score?: number; stage?: string; stage_sub?: string }) =>
    request<{ symbol: string; score_at_track: number }>('/tracking', { method: 'POST', body: JSON.stringify(body) }),
  trackingRemove: (symbol: string) => request<{ ok: boolean }>(`/tracking/${symbol}`, { method: 'DELETE' }),
  trackingDeletePoint: (pointId: number) => request<{ ok: boolean }>(`/tracking/points/${pointId}`, { method: 'DELETE' }),
  trackingList: () => request<{ items: TrackedStock[] }>('/tracking'),
  trackingHistory: () => request<{ items: TrackedHistory[] }>('/tracking/history'),
  trackingPoints: (symbol: string) => request<{ items: ScorePoint[] }>(`/tracking/points/${symbol}`),
  trackingSampleNow: () => request<{ total: number; ok: number; failed: number }>('/tracking/sample-now', { method: 'POST' }),

  // ---------------------------------------------------------------- 选股缓存管理
  screenerClearCache: (password: string) =>
    request<{ cleared: number }>('/screener/cache/clear', { method: 'POST', body: JSON.stringify({ password }) }),

  // ---------------------------------------------------------------- 回测中心(方案C: 阶段分桶)
  backtestFactor: (body: { symbols?: string[] | null; hold_days?: number[]; min_bars?: number; cost?: boolean }) =>
    request<BacktestFactorReport>('/backtest/factor', { method: 'POST', body: JSON.stringify(body) }),
  // 变体对比回测(消融实验): 同池同种子跑多变体, 量化各风控开关贡献
  backtestStrategyCompare: (body: {
    pool_size?: number
    seed?: number
    board?: string
    industry?: string
    universe?: string
    start?: string
    end?: string
    initial_capital?: number
    variants?: { label?: string; cooldown_days?: number; defense?: string }[]
  }) => request<{ task_id: string; variants: number }>('/backtest/strategy-compare', { method: 'POST', body: JSON.stringify(body) }),
  backtestCompareTask: (taskId: string) => request<CompareTaskState>(`/backtest/tasks/${taskId}`, undefined, 10_000),
  /** 对比回测历史: 运行列表(倒序, 含各变体标签与收益) */
  backtestHistoryList: (limit = 50, offset = 0) =>
    request<{ items: BacktestRunSummary[] }>(`/backtest/history?limit=${limit}&offset=${offset}`),
  /** 对比回测历史: 单次运行详情(参数 + 变体摘要, 可直接渲染对比页) */
  backtestHistoryDetail: (runId: number) =>
    request<BacktestRunDetail>(`/backtest/history/${runId}`),
  /** 对比回测历史: 逐笔交易明细(按变体/股票过滤; 默认一次拉全量, 前端本地筛选) */
  backtestHistoryTrades: (runId: number, variant = '', symbol = '', limit = 20_000) => {
    const q = new URLSearchParams()
    if (variant) q.set('variant', variant)
    if (symbol) q.set('symbol', symbol)
    q.set('limit', String(limit))
    return request<{ items: BacktestCompareTrade[]; total: number }>(
      `/backtest/history/${runId}/trades?${q.toString()}`)
  },
  /** 对比回测历史: 删除一次运行及其全部明细 */
  backtestHistoryDelete: (runId: number) =>
    request<{ id: number }>(`/backtest/history/${runId}`, { method: 'DELETE' }),
  /** 卡死诊断: dump 回测工作线程实时调用栈(连点两次对比栈顶, 不变=真卡死) */
  backtestTaskStack: (taskId: string) =>
    request<{ progress: number; idle_seconds: number; stack: string }>(`/backtest/tasks/${taskId}/stack`, undefined, 10_000),
  /** 放弃回测任务: 协作取消 + 立即标记 error(守卫放行新任务) */
  cancelBacktestTask: (taskId: string) =>
    request<{ msg: string }>(`/backtest/tasks/${taskId}/cancel`, { method: 'POST' }, 10_000),
  // 持仓回测(方案 v2 §5: 三线对照 + 差异归因)
  backtestPortfolio: (body: {
    mode?: string
    legs?: { symbol: string; name?: string; entry_date?: string; cost?: number; qty?: number }[]
    manage?: string
    symbols?: string[] | null
    start?: string
    end?: string
    initial_capital?: number
    intraday_minutes?: number
  }) => request<PortfolioBacktestReport>('/backtest/portfolio', { method: 'POST', body: JSON.stringify(body) }),
  portfolioPreview: () => request<PortfolioPreview>('/backtest/portfolio/preview'),
  // 建仓腿模板(快捷复用)
  backtestPresets: () => request<BacktestPreset[]>('/backtest/presets'),
  saveBacktestPreset: (body: { name: string; legs: { symbol: string; name?: string; entry_date?: string; cost?: number; qty?: number }[] }) =>
    request<{ id: number; name: string }>('/backtest/presets', { method: 'POST', body: JSON.stringify(body) }),
  deleteBacktestPreset: (id: number) => request<{ id: number }>(`/backtest/presets/${id}`, { method: 'DELETE' }),
  // 信号审计(真实 vs 纪律)
  backtestAudit: (body: { symbols?: string[] | null; start?: string; end?: string }) =>
    request<SignalAuditReport>('/backtest/audit', { method: 'POST', body: JSON.stringify(body) }),
  // ---------------------------------------------------------------- K线数据管理(数据管理 tab)
  /** 日线缓存新鲜度统计 */
  klineStats: () => request<KlineStats>('/kline-cache/stats'),
  /** 触发增量补拉(异步任务): 只补 陈旧/不足/未缓存 的票, 断点续跑 */
  backfillStart: (opts?: { target?: number; symbols?: string[]; force?: boolean }) => {
    const q = new URLSearchParams()
    if (opts?.target) q.set('target', String(opts.target))
    if (opts?.symbols?.length) q.set('symbols', opts.symbols.join(','))
    if (opts?.force) q.set('force', 'true')
    return request<{ task_id: string }>(`/kline/backfill?${q.toString()}`, { method: 'POST' })
  },
  /** 补拉任务状态(task_id 空 = 最近一次) */
  backfillStatus: (taskId?: string) =>
    request<BackfillTask | null>(taskId ? `/kline/backfill/status/${taskId}` : '/kline/backfill/status'),
  /** 回测冻结快照预热(baostock 前复权, 3年): 默认池=自选+持仓 */
  backtestWarmup: (body: { symbols?: string[] | null; start?: string; end?: string; force?: boolean }) =>
    request<{ meta: Record<string, unknown>; results: Record<string, unknown> }>('/backtest/data/warmup', { method: 'POST', body: JSON.stringify(body) }),
  /** 回测冻结快照状态 */
  backtestDataStatus: () => request<BacktestDataStatus>('/backtest/data/status'),

  // ---------------------------------------------------------------- 三期: 交易日志/统计
  trades: (params: { symbol?: string; action?: string; limit?: number; offset?: number } = {}) => {
    const q = new URLSearchParams()
    if (params.symbol) q.set('symbol', params.symbol)
    if (params.action) q.set('action', params.action)
    if (params.limit) q.set('limit', String(params.limit))
    if (params.offset) q.set('offset', String(params.offset))
    return request<TradePage>(`/trades?${q.toString()}`)
  },
  exportTrades: () => {
    window.open('/api/trades/export', '_blank')
  },
  reducePosition: (symbol: string, qty: number, price: number, reason = '') =>
    request<{ realized_pnl: number }>(`/positions/${symbol}/reduce`, { method: 'POST', body: JSON.stringify({ qty, price, reason }) }),
  closePosition: (symbol: string, price: number, reason = '清仓') =>
    request<{ realized_pnl: number }>(`/positions/${symbol}/close`, { method: 'POST', body: JSON.stringify({ price, reason }) }),
  statsSummary: () => request<StatsSummary>('/stats/summary'),
  statsEquityCurve: () => request<{ curve: EquityPoint[] }>('/stats/equity-curve'),
  statsMonthly: () => request<{ months: MonthStat[] }>('/stats/monthly-heatmap'),
  statsSignals: () => request<{ items: SignalDistItem[] }>('/stats/signal-distribution'),
  statsScores: () => request<{ items: TradeScore[]; health: number }>('/stats/scores'),

  // ---------------------------------------------------------------- 四期: AI 复盘
  aiReviewRun: (scope = 'week') =>
    request<{ task_id: string }>('/ai-review/run', { method: 'POST', body: JSON.stringify({ scope }) }),
  aiReviewResult: (taskId: string) => request<AiReviewTask>(`/ai-review/result?task_id=${taskId}`),
  aiReviewHistory: () => request<AiReviewRecord[]>('/ai-review/history'),
  aiReviewSuggestion: (reviewId: number, index: number, status: 'accepted' | 'rejected') =>
    request<AiReviewMarkResponse>('/ai-review/suggestion', {
      method: 'POST', body: JSON.stringify({ review_id: reviewId, index, status }),
    }),
  aiReviewConfig: () => request<AiReviewConfig>('/ai-review/config'),
  // 参数变更记录(采纳建议 -> 热写回配置 -> 可回滚)
  aiReviewChanges: () => request<ConfigChange[]>('/ai-review/changes'),
  aiReviewRevert: (changeId: number) =>
    request<ConfigChange>('/ai-review/changes/' + changeId + '/revert', { method: 'POST' }),
  // 调参护栏策略(供前端展示边界)
  aiReviewTuningPolicy: () => request<TuningPolicy>('/ai-review/tuning-policy'),

  // ---------------------------------------------------------------- 盘后 AI 日报 + 站内通知
  /** 手动生成今日日报(验证用, 不等到 16:30 定时任务) */
  reportDailyRun: () => request<DailyReportRecord>('/report/daily/run', { method: 'POST' }),
  reportDaily: (date?: string) => {
    const q = date ? `?date=${date}` : ''
    return request<DailyReportRecord | null>(`/report/daily${q}`)
  },
  notifications: (limit = 20) => request<NotificationItem[]>(`/notifications?limit=${limit}`),
  notificationRead: (id: number) => request<NotificationItem>(`/notifications/${id}/read`, { method: 'POST' }),

  // ---------------------------------------------------------------- AI 助理(LangGraph 流水线)
  /** 手动触发单阶段流水线(验证用): premarket / intraday / after_close */
  assistantRun: (phase = 'intraday') => request<AssistantRunResult>(`/assistant/run?phase=${phase}`, { method: 'POST' }),
  assistantStatus: () => request<AssistantStatus>('/assistant/status'),

  // ---------------------------------------------------------------- 盘中实时监控预警
  intradayStatus: () => request<IntradayStatus>('/intraday/status'),
  intradayRun: () => request<IntradayRunResult>('/intraday/run', { method: 'POST' }),
  intradayAlerts: (limit = 50) => request<NotificationItem[]>(`/intraday/alerts?limit=${limit}`),
}

export interface Quote {
  symbol: string
  name: string
  price: number
  open: number
  high: number
  low: number
  prev_close: number
  volume: number
  amount: number
  change: number
  change_pct: number
  timestamp: string
}

export interface KlineData {
  symbol: string
  period: string
  klines: Array<{ date: string; open: number; high: number; low: number; close: number; volume: number; amount: number }>
}

export interface IndicatorData {
  symbol: string
  period: string
  snapshot: Record<string, number | string | null>
}

export interface SourceStatus {
  name: string
  label: string
  enabled: boolean
  circuit_open: boolean
  consecutive_failures: number
  avg_latency_ms: number
  request_count: number
  success_count: number
  preferred: boolean
}

export interface HealthData {
  status: string
  time: string
  tz: string
  date: string
  data_sources: SourceStatus[]
  db: string
}

// ---------------------------------------------------------------- 二期接口类型
export interface WatchlistItem {
  symbol: string
  name: string
  added_at: string
}

/** 持仓周期内单笔操作(时间线): build=建仓 add=加仓 reduce=减仓 */
export interface PositionAction {
  type: 'build' | 'add' | 'reduce'
  label: string
  time: string
  price: number
  qty: number
  /** 仅减仓有值(已实现盈亏净额, 已扣双边手续费) */
  pnl: number | null
}

export interface PositionItem {
  symbol: string
  name: string
  qty: number
  /** 含费摊薄成本(已摊入买入手续费), 券商 APP 口径 */
  cost: number
  /** 纯成交均价(不含费), 仅用于顺向加仓判断 */
  cost_raw: number
  /** 摊在当前持仓上的买入手续费 = (cost - cost_raw) * qty */
  fee_cost: number
  /** 持仓时间(首仓录入时间) */
  opened_at: string
  /** 当前持仓周期的操作时间线(建仓/加仓/减仓) */
  actions: PositionAction[]
  price: number
  market_value: number
  /** 浮盈已扣买入手续费(因 cost 含费) */
  unrealized_pnl: number
  unrealized_pct: number
}

export interface Portfolio {
  positions: PositionItem[]
  market_value: number
  /** 含费总成本 */
  cost_value: number
  /** 纯成交额总成本(不含费) */
  cost_raw_value: number
  /** 已摊入持仓的买入手续费合计 */
  fee_cost: number
  unrealized_pnl: number
  /** 已实现盈亏(历史卖出净额, 已扣双边手续费) */
  realized_pnl: number
  unrealized_pct: number
}

export interface PositionDetail {
  position: PositionItem
  pyramid: { used_stage: number; remaining_ratios: number[]; suggest_next_pct: number; mode?: string }
  take_profit: Array<{ level: number; target_price: number; target_pct: number; suggest_reduce_ratio: number }>
  mode?: string
  mode_label?: string
  mode_reason?: string
  history: Array<{ time: string; action: string; price: number; qty: number; pnl: number; reason: string }>
}

/** 资金账户: 仅含启动资金; 可用资金/总权益由前端按持仓市值派生 */
export interface AccountInfo {
  start_capital: number
  updated_at: string
}

/** 信号评估结果(单票/批量共用): signal 为 null 表示无信号, error 表示评估失败 */
export interface EvaluateResult {
  symbol: string
  name: string
  price: number
  signal: Signal | null
  error?: string
}

export interface Signal {
  type: string
  symbol: string
  name: string
  direction: string
  strength: number
  reason: string
  price: number
  indicators_snapshot: Record<string, number>
}

export interface SignalRecord {
  id: number
  time: string
  symbol: string
  name: string
  type: string
  direction: string
  strength: number
  reason: string
  indicators_json: string
}

export interface PlanRecord {
  id: number
  time: string
  symbol: string
  name: string
  action: string
  content: string
  status: string
}

export interface RiskStatus {
  day_loss_tripped: boolean
  defense_mode: boolean
  consecutive_losses: number
  day_pnl: number
  last_trade_pnl: number
  position_multiplier: number
  config: Record<string, number>
}

/** 选股池(指数成分股)缓存概况. key: hs300/zz500/sz50 */
export interface UniverseStats {
  [indexKey: string]: {
    count: number
    updated_at: string
    label: string
  }
}

/** 行业选项(数量为覆盖股票数) */
export interface IndustryItem {
  name: string
  count: number
}

/** 选股理由标签. kind: good 利多 / warn 需注意 / bad 偏空 / info 中性 */
export interface ScreenerTag {
  text: string
  kind: 'good' | 'warn' | 'bad' | 'info' | string
}

/** 选股结果行(三因子得分 + 人话理由 + 趋势阶段 + 因子叠加) */
export interface ScreenerResult {
  symbol: string
  name: string
  total: number
  trend_score: number
  momentum_score: number
  volume_score: number
  attention: string
  close: number
  adx: number
  roc: number
  rsi: number
  volume_ratio: number
  amount_avg: number
  // 以下为人话理由字段(后端 _build_reason 产出; 旧任务结果可能缺失, 前端需容错)
  bias?: number
  reason?: string
  risk?: string
  tags?: ScreenerTag[]
  detail?: Record<string, string>
  // 趋势阶段(方案B): 启动/加速/过热/衰竭; 旧任务结果缺失, 前端需容错
  stage?: string
  // 加速期细分: early 前期 / mid 中期 / late 后期(仅 stage=accelerate 时有值)
  stage_sub?: string
  // 趋势年龄: 距最近一次短均线上穿中均线的交易日数(null=超回看窗口的老趋势)
  trend_age?: number | null
  stage_bonus?: number
  stage_penalty?: number
  stage_note?: string
  // 基本面/事件因子叠加(apply_fundamental_factors 产出): base_total 为叠加前三因子+阶段分, factor_delta 为叠加值
  base_total?: number
  factor_delta?: number
  quality_score?: number
  event_score?: number
}

export interface ScreenerTask {
  id: string
  status: string
  market: string
  top_n: number
  total: number
  done: number
  progress: number
  result: ScreenerResult[]
  error: string
}

/** 申万行业树节点(递归: children 缺省=叶子) */
export interface IndustryNode {
  name: string
  count: number
  children?: IndustryNode[]
}

/** 中断的扫描任务(断点续传: 可继续扫描) */
export interface InterruptedTask {
  task_id: string
  status: string
  market: string
  board: string
  industry: string
  top_n: number
  universe: string
  total: number
  done: number
  error: string
  created_at: string
  updated_at: string
}

/** 得分追踪: 追踪中的股票(附最近一次采样) */
export interface TrackedStock {
  symbol: string
  name: string
  track_time: string
  score_at_track: number
  stage_at_track: string
  /** 追踪时的加速期子阶段(early/mid/late; 旧数据缺失, 前端容错) */
  stage_sub_at_track?: string
  status: string
  archived_at?: string
  archive_reason?: string
  sim_qty: number
  sim_cost: number
  sim_open_at: string
  sim_realized_pnl: number
  // 归档成绩(结算后写入; 旧归档数据可能缺失, 前端按未结算兜底)
  final_pnl?: number
  final_stage?: string
  final_stage_sub?: string
  latest?: {
    time: string
    score: number
    price: number
    stage: string
    stage_sub?: string
    trend_age?: number | null
    signal_type: string
    sample_kind: string
    sim_qty: number
    sim_cost: number
    sim_pnl: number
    sim_action: string
  }
}

/** 得分追踪: 历史档(已归档)成绩单(list_history 聚合产出) */
export interface TrackedHistory extends TrackedStock {
  first_time?: string
  last_time?: string
  days?: number
  first_price?: number
  last_price?: number
  /** 纯持有收益%(首->末采样价), 基准 */
  hold_pnl?: number | null
  first_score?: number
  last_score?: number
  max_score?: number
  action_counts?: { open: number; add: number; reduce: number; close: number }
}

/** 得分追踪: 采样点 */
export interface ScorePoint {
  id: number
  time: string
  score: number
  trend_score: number
  momentum_score: number
  volume_score: number
  stage: string
  stage_sub?: string
  trend_age?: number | null
  price: number
  volume_ratio: number
  signal_type: string
  sample_kind: string
  sim_qty: number
  sim_cost: number
  sim_pnl: number
  sim_action: string
}

/** 选股条件组合预设(指数池+板块+行业) */
export interface ScreenerPreset {
  id: number
  name: string
  universe: string
  board: string
  industry: string
  created_at: string
}

/** 选股扫描历史(列表项不含结果, 点击后再取详情) */
export interface ScreenerHistoryItem {
  id: number
  time: string
  market: string
  board: string
  industry: string
  top_n: number
  per_industry: number
  industry_level: string
  apply_gate: boolean
  universe: string
  apply_factors: boolean
  total: number
  result_count: number
  status: string
  /** 仅详情接口返回 */
  result?: ScreenerTask['result']
  error?: string
}

// ---------------------------------------------------------------- 回测中心类型
export interface BacktestHoldStats {
  n: number
  win_rate: number
  avg: number
  median: number
  expectancy: number
}

export interface BacktestStageResult {
  label: string
  holds: Record<string, BacktestHoldStats> // hold_5 / hold_10 / hold_20
}

export interface BacktestFactorReport {
  meta: {
    symbols_total: number
    symbols_used: number
    hold_days: number[]
    cost_included: boolean
    date_from: string
    date_to: string
    notes: string
  }
  by_stage: Record<string, BacktestStageResult>
  stage_distribution: Record<string, number>
  /** 总分分桶(0-40/40-50/50-60/60-70/70+), 验证评分体系有效性 */
  by_score: Record<string, BacktestStageResult>
  score_distribution: Record<string, number>
}

// ---------------------------------------------------------------- 持仓回测类型(方案 v2 §5)
export interface PortfolioLegPreview {
  symbol: string
  name: string
  entry_date: string
  cost: number
  qty: number
  pyramid_stage?: number
}

export interface PortfolioPreview {
  positions: PortfolioLegPreview[]
  trades: PortfolioLegPreview[]
  manage_options: { key: string; label: string }[]
}

export interface BacktestPreset {
  id: number
  name: string
  created_at: string
  legs: { symbol: string; name?: string; entry_date?: string; cost?: number; qty?: number }[]
}

// ---------------------------------------------------------------- 信号审计(方案 v2 §6)
export interface SignalAuditReport {
  meta: {
    symbols: number
    days: number
    start: string
    end: string
    notes: string
  }
  curves: {
    real: { date: string; equity: number }[]
    discipline: { date: string; equity: number }[]
  }
  by_symbol: {
    symbol: string
    name: string
    real_return_pct: number
    discipline_return_pct: number
    gap_pct: number
  }[]
  stats: {
    gap_total_pct: number
    audit_count: number
    agree: number
    violate: number
    lag: number
    early: number
  }
  audits: {
    date: string
    symbol: string
    real_action: string
    advice: string
    deviation: string
  }[]
}

export interface PortfolioBacktestReport {
  meta: {
    manage: string
    manage_label: string
    legs: number
    skipped: number
    initial_capital: number
    days: number
    benchmark: string
    notes: string
  }
  curves: {
    hold: { date: string; equity: number }[]
    stop: { date: string; equity: number }[]
    signal: { date: string; equity: number }[]
    benchmark: { date: string; equity: number }[]
  }
  stats: {
    hold_return_pct: number
    stop_return_pct: number
    signal_return_pct: number
    signal_annual_pct: number
    managed_max_drawdown_pct: number
    managed_sharpe: number
    excess_vs_hold_pct: number
    trades: number
    closed: number
    win_rate: number
    t_sell_count: number
    t_contribution: number
    fuse_triggered: boolean
    defense_mode: boolean
  }
  legs: {
    symbol: string
    name: string
    entry_date: string
    cost: number
    qty: number
    hold_return_pct: number
    managed_return_pct: number
    excess_pct: number
    attribution: Record<string, number>
  }[]
  trades: BacktestTrade[]
}

// ---------------------------------------------------------------- 回测交易明细(持仓/对比回测共用)
export interface BacktestTrade {
  date: string
  symbol: string
  name: string
  action: string // buy_first/buy_add/sell_reduce/sell_stop/t_sell/t_buy
  price: number
  qty: number
  fee: number
  pnl: number
  reason: string
}

// ---------------------------------------------------------------- 变体对比回测(消融实验)
export interface CompareVariantResult {
  label: string
  cooldown_days: number
  defense: string
  error?: string
  date_from?: string
  date_to?: string
  total_return_pct?: number
  annual_return_pct?: number
  max_drawdown_pct?: number
  sharpe?: number
  days?: number
  win_rate?: number
  profit_factor?: number
  expectancy?: number
  trades?: number
  cooldown_blocks?: number
  final_defense?: boolean
  by_action?: Record<string, { n: number; pnl: number }>
  equity_curve?: { date: string; equity: number }[]
}

export interface CompareReport {
  pool: { size: number; seed: number; symbols: number; note?: string }
  variants: CompareVariantResult[]
  run_id?: number  // 落库后的运行 ID(可查历史与逐笔明细)
}

// ---------------------------------------------------------------- 对比回测历史(落库可回看)
export interface BacktestCompareTrade {
  variant: string
  date: string
  symbol: string
  name: string
  action: string // buy_first/buy_add/sell_reduce/sell_stop/t_sell/t_buy
  price: number
  qty: number
  fee: number
  pnl: number
  reason: string
}

export interface BacktestRunSummary {
  id: number
  time: string
  mode: string
  pool_size: number
  seed: number
  universe: string
  board: string
  industry: string
  start: string
  end: string
  initial_capital: number
  symbols: number
  variants: { label: string; total_return_pct?: number | null; trades?: number; error?: string }[]
}

export interface BacktestRunDetail extends BacktestRunSummary {
  report: CompareReport
}

export interface CompareTaskState {
  status: 'running' | 'done' | 'error'
  progress: number
  result: CompareReport | null
  error: string
  last_active?: number  // 任务心跳(epoch 秒): 区分"慢"与"死"
  stall_stack?: string  // 看门狗自动抓取的停滞栈(心跳>30s无活动时)
  stall_at?: number
  stall_progress?: number
}

// ---------------------------------------------------------------- K线数据管理
export interface KlineStats {
  ok: number
  stale: number
  missing: number
  cached: number
  symbols: number
  date_from: string
  date_to: string
  days_behind: number | null
  target: number
  note: string
}

export interface BackfillTask {
  id: string
  status: 'pending' | 'running' | 'done' | 'failed'
  progress: number
  total?: number
  done?: number
  created_at?: string
  result?: {
    target?: number
    universe?: number
    pending?: number
    done?: number
    insufficient?: { symbol: string; detail: string }[]
    failed?: { symbol: string; error: string }[]
    started_at?: string
    finished_at?: string
  }
  error?: string
}

export interface BacktestDataStatus {
  symbols: number
  adjusts: string[]
  last_fetched_at: string
  note: string
}

// ---------------------------------------------------------------- 三期类型
export interface TradeRecord {
  id: number
  time: string
  symbol: string
  name: string
  action: string
  price: number
  qty: number
  amount: number
  fee: number
  reason: string
  pnl: number
  note: string
}

export interface TradePage {
  total: number
  items: TradeRecord[]
}

export interface StatsSummary {
  trades: number
  wins: number
  losses: number
  win_rate: number
  total_pnl: number
  profit_factor: number
  avg_win: number
  avg_loss: number
  expectancy: number
  max_win: number
  max_loss: number
  max_consecutive_losses: number
}

export interface EquityPoint {
  time: string
  equity: number
  pnl: number
  symbol?: string
  name?: string
}

export interface MonthStat {
  month: string
  pnl: number
  trades: number
  win_rate: number
}

export interface SignalDistItem {
  type: string
  count: number
}

export interface TradeScore {
  id: number
  time: string
  symbol: string
  name: string
  pnl: number
  pnl_pct: number
  score: number
  comment: string
}

// ---------------------------------------------------------------- 四期: AI 复盘类型
export interface AiReviewIssue {
  code: string
  level: string
  title: string
  detail: string
  evidence: string
}
export interface AiReviewSuggestion {
  text: string
  status: string
  source?: string
  /** 可执行参数补丁(采纳时经三道闸门热写回配置); 纯文字建议无此字段 */
  patch?: {
    group: string
    key: string
    from: number | null
    to: number
    label: string
  }
  /** 闸门状态: ok / clamped / not_whitelisted / cooldown / drift_limit / invalid / no_change / duplicate / text_only */
  guard?: string
  guard_msg?: string
  /** 已采纳并生效后回填的变更记录 id; 回退请到「参数变更记录」 */
  change_id?: number
  applied_at?: string
}

/** mark_suggestion 返回: 含本次是否真的改了参数的 info */
export interface AiReviewApplyInfo {
  applied: boolean
  message: string
  change_id?: number
  group?: string
  key?: string
  label?: string
  from?: number
  to?: number
}
export interface AiReviewMarkResponse {
  id: number
  suggestions: AiReviewSuggestion[]
  applied: AiReviewApplyInfo
}

/** 参数变更记录(采纳建议导致, 可一键回滚) */
export interface ConfigChange {
  id: number
  time: string
  group: string
  key: string
  label: string
  from: number
  to: number
  source: string
  review_id: number | null
  status: string // active | reverted
  reverted_at: string
  note: string
  days_active: number | null
}

/** 调参护栏策略(后端 tuning.py 常量) */
export interface TuningPolicy {
  max_step_pct: number
  max_drift_pct: number
  cooldown_days: number
  max_accept_per_review: number
  field_count: number
  allowed_groups: Record<string, string[]>
  forbidden_groups: string[]
}
export interface AiReviewRecord {
  id: number
  time: string
  range: string
  content: string
  suggestions: AiReviewSuggestion[]
  model: string
  rule_result: {
    issues: AiReviewIssue[]
    stats: Record<string, number>
  }
}
export interface AiReviewTask {
  status: string
  progress: number
  review?: AiReviewRecord
  error?: string
}
export interface AiReviewConfig {
  provider: string
  base_url: string
  api_key: string
  has_key: boolean
  model: string
  enabled: boolean
}

// ---------------------------------------------------------------- 盘后 AI 日报类型
/** 日报内容: LLM 结构化输出; 规则模板降级时 text 兜底 */
export interface DailyReportContent {
  market_summary: string
  trade_summary: string
  holdings_review: string[]
  signals_today: string[]
  tomorrow_watch: string[]
  risk_notes: string[]
  discipline_score: number
  text: string
}

export interface DailyReportRecord {
  id: number
  date: string
  /** ok = LLM 生成 / degraded = 规则模板降级 */
  status: string
  model: string
  created_at: string
  content: DailyReportContent
}

export interface NotificationItem {
  id: number
  time: string
  category: string
  title: string
  content: string
  read: boolean
}

// ---------------------------------------------------------------- AI 助理类型
/** 手动触发单阶段流水线的返回(状态摘要) */
export interface AssistantRunResult {
  phase: string
  signals: number
  fresh: number
  insights: number
  notifications: Array<{ id: number; symbol: string; type: string }>
  market: string
}

export interface AssistantStatus {
  enabled: boolean
  premarket: { enabled: boolean; hour: number; minute: number }
  intraday: { enabled: boolean; interval_min: number }
  after_close: { enabled: boolean; hour: number; minute: number }
  push_webhook: boolean
  jobs: Record<string, boolean>
  recent_notifications: NotificationItem[]
}

// ---------------------------------------------------------------- 盘中监控类型
export interface IntradayAlertRule {
  enabled: boolean
  threshold?: number
  threshold_pct?: number
}

export interface IntradayStatus {
  enabled: boolean
  interval_sec: number
  scope: string
  cooldown_sec: number
  alert_rules: Record<string, IntradayAlertRule>
  today_alerts: number
}

export interface IntradayRunResult {
  checked: number
  alerts: number
  errors: number
  skipped?: string
  error?: string
}
