// 后端 API 统一封装: 响应 {code, msg, data}
const BASE = '/api'

export async function request<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!resp.ok) {
    throw new Error(`HTTP ${resp.status}`)
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
  updateConfig: (config: Record<string, unknown>) =>
    request<Record<string, unknown>>('/config', { method: 'PUT', body: JSON.stringify({ config }) }),
  dataSourceStatus: () => request<SourceStatus[]>('/data-sources/status'),
  testSource: (name: string) => request(`/data-sources/test/${name}`, { method: 'POST' }),
  quote: (symbol: string) => request<Quote>('/quote/' + symbol),
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
    request<PlanRecord>('/plan/generate', { method: 'POST', body: JSON.stringify({ symbol, name }) }),
  currentPlans: () => request<PlanRecord[]>('/plan/current'),
  planStatus: (id: number, status: 'done' | 'ignored') =>
    request<PlanRecord>(`/plan/${id}/status`, { method: 'PUT', body: JSON.stringify({ status }) }),

  // 风控
  riskStatus: () => request<RiskStatus>('/risk/status'),
  riskReset: () => request<RiskStatus>('/risk/reset', { method: 'POST' }),

  // 选股
  screenerRun: (market = 'all', topN = 30) =>
    request<{ task_id: string }>(`/screener/run?market=${market}&top_n=${topN}`, { method: 'POST' }),
  screenerResult: (taskId: string) => request<ScreenerTask>(`/screener/result?task_id=${taskId}`),
  screenerLatest: () => request<ScreenerTask | null>('/screener/result/latest'),

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
  cost: number
  price: number
  market_value: number
  unrealized_pnl: number
  unrealized_pct: number
}

export interface Portfolio {
  positions: PositionItem[]
  market_value: number
  cost_value: number
  unrealized_pnl: number
  unrealized_pct: number
}

export interface PositionDetail {
  position: PositionItem
  pyramid: { used_stage: number; remaining_ratios: number[]; suggest_next_pct: number }
  take_profit: Array<{ level: number; target_price: number; target_pct: number; suggest_reduce_ratio: number }>
  history: Array<{ time: string; action: string; price: number; qty: number; pnl: number; reason: string }>
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
  }>
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
