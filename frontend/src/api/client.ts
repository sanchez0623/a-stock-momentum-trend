// 后端 API 统一封装: 响应 {code, msg, data}
const BASE = '/api'

export async function request<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
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
  addPosition: (p: { symbol: string; name?: string; qty: number; price: number; reason?: string; action?: string }) =>
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
  evaluateSignal: (symbol: string) => request<{ symbol: string; signal: Signal | null }>(`/signals/evaluate/${symbol}`, { method: 'POST' }),
  evaluateBatch: (symbols: string[]) =>
    request<Array<{ symbol: string; name: string; price: number; signal: Signal | null; error?: string }>>(
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
  ) => {
    const q = new URLSearchParams({ market, top_n: String(topN) })
    if (board) q.set('board', board)
    if (industry) q.set('industry', industry)
    if (universe) q.set('universe', universe)
    if (opts?.perIndustry && opts.perIndustry > 0) q.set('per_industry', String(opts.perIndustry))
    if (opts?.industryLevel) q.set('industry_level', opts.industryLevel)
    if (opts?.applyGate === false) q.set('apply_gate', 'false')
    if (opts?.applyFactors === false) q.set('apply_factors', 'false')
    return request<{ task_id: string }>(`/screener/run?${q.toString()}`, { method: 'POST' })
  },
  screenerResult: (taskId: string) => request<ScreenerTask>(`/screener/result?task_id=${taskId}`),
  screenerLatest: () => request<ScreenerTask | null>('/screener/result/latest'),
  screenerHistory: () => request<{ items: ScreenerHistoryItem[] }>('/screener/history'),
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

  // ---------------------------------------------------------------- 回测中心(方案C: 阶段分桶)
  backtestFactor: (body: { symbols?: string[] | null; hold_days?: number[]; min_bars?: number; cost?: boolean }) =>
    request<BacktestFactorReport>('/backtest/factor', { method: 'POST', body: JSON.stringify(body) }),
  // 全流程策略回测(异步任务: 建仓/加仓/止盈/止损/做T + 风控)
  backtestStrategy: (body: { symbols?: string[] | null; initial_capital?: number }) =>
    request<{ task_id: string }>('/backtest/strategy', { method: 'POST', body: JSON.stringify(body) }),
  backtestTask: (taskId: string) => request<BacktestTaskState>(`/backtest/tasks/${taskId}`),

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
    request<{ realized_pnl: number }>(`/positions/${symbol}/close`, { method: 'POST', body: JSON.stringify({ qty: 0, price, reason }) }),
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
  aiReviewSaveConfig: (cfg: { base_url?: string; api_key?: string; model?: string; enabled?: boolean }) =>
    request<AiReviewConfig>('/ai-review/config', { method: 'PUT', body: JSON.stringify(cfg) }),
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

export interface ScreenerTask {
  id: string
  status: string
  market: string
  top_n: number
  total: number
  done: number
  progress: number
  result: Array<{
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
    stage_bonus?: number
    stage_penalty?: number
    stage_note?: string
    // 基本面/事件因子叠加(apply_fundamental_factors 产出): base_total 为叠加前三因子+阶段分, factor_delta 为叠加值
    base_total?: number
    factor_delta?: number
    quality_score?: number
    event_score?: number
  }>
  error: string
}

/** 申万行业树节点(递归: children 缺省=叶子) */
export interface IndustryNode {
  name: string
  count: number
  children?: IndustryNode[]
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
}

// ---------------------------------------------------------------- 策略回测类型
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

export interface BacktestStrategyReport {
  meta: {
    pool: number
    skipped: number
    initial_capital: number
    final_equity: number
    total_return_pct: number
    annual_return_pct: number
    max_drawdown_pct: number
    sharpe: number
    days: number
    notes: string
  }
  stats: {
    trades: number
    closed: number
    win_rate: number
    profit_factor: number
    expectancy: number
    avg_win: number
    avg_loss: number
    consecutive_losses_max: number
    turnover_pct: number
    t_sell_count: number
    t_contribution: number
    fuse_triggered: boolean
    defense_mode: boolean
  }
  equity_curve: { date: string; equity: number }[]
  trades: BacktestTrade[]
}

export interface BacktestTaskState {
  status: 'running' | 'done' | 'error'
  progress: number
  result: BacktestStrategyReport | null
  error: string
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
