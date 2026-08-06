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
