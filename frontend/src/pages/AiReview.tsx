import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { AiReviewConfig, AiReviewRecord, AiReviewTask } from '../api/client'
import { Button, Card, EmptyState, ErrorBox, Field, ListRow, Loading, Tag, inputStyle, toast } from '../components/ui'

const LEVEL_META: Record<string, { label: string; color: string }> = {
  high: { label: '严重', color: '#dc2626' },
  medium: { label: '中等', color: '#ea580c' },
  low: { label: '轻微', color: '#64748b' },
}

export default function AiReview() {
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // 配置
  const [cfg, setCfg] = useState<AiReviewConfig | null>(null)
  const [cfgForm, setCfgForm] = useState({ base_url: '', api_key: '', model: '', enabled: false })
  const [cfgSaved, setCfgSaved] = useState('')

  // 触发
  const [scope, setScope] = useState('week')
  const [task, setTask] = useState<AiReviewTask | null>(null)
  const [running, setRunning] = useState(false)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // 历史
  const [history, setHistory] = useState<AiReviewRecord[]>([])
  const [current, setCurrent] = useState<AiReviewRecord | null>(null)

  const loadConfig = () => api.aiReviewConfig().then((c) => {
    setCfg(c)
    setCfgForm({ base_url: c.base_url || '', api_key: '', model: c.model || '', enabled: c.enabled })
  }).catch((e) => setError(String(e.message || e)))

  useEffect(() => {
    Promise.all([api.aiReviewHistory().then(setHistory).catch(() => {}), loadConfig()])
      .finally(() => setLoading(false))
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [])

  const stopPoll = () => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
  }

  const run = async () => {
    setRunning(true)
    setError('')
    try {
      const { task_id } = await api.aiReviewRun(scope)
      stopPoll()
      pollRef.current = setInterval(async () => {
        try {
          const t = await api.aiReviewResult(task_id)
          setTask(t)
          if (t.status === 'done') {
            stopPoll(); setRunning(false)
            setCurrent(t.review ?? null)
            setHistory(await api.aiReviewHistory())
            toast.success('复盘完成')
          } else if (t.status === 'failed') {
            stopPoll(); setRunning(false)
            setError(t.error || '复盘失败')
            toast.error(t.error || '复盘失败')
          }
        } catch { stopPoll(); setRunning(false) }
      }, 2000)
    } catch (e) {
      setError(String((e as Error).message)); setRunning(false)
      toast.error(String((e as Error).message))
    }
  }

  const saveConfig = async () => {
    setCfgSaved('')
    try {
      await api.aiReviewSaveConfig({
        base_url: cfgForm.base_url || undefined,
        api_key: cfgForm.api_key || undefined,
        model: cfgForm.model || undefined,
        enabled: cfgForm.enabled,
      })
      setCfgSaved('已保存')
      setCfgForm((f) => ({ ...f, api_key: '' }))
      toast.success('LLM 配置已保存')
      await loadConfig()
    } catch (e) {
      setError(String((e as Error).message))
      toast.error(String((e as Error).message))
    }
  }

  const markSuggestion = async (index: number, status: 'accepted' | 'rejected') => {
    if (!current) return
    try {
      const r = await api.aiReviewSuggestion(current.id, index, status)
      setCurrent({ ...current, suggestions: r.suggestions })
      setHistory(await api.aiReviewHistory())
    } catch (e) {
      setError(String((e as Error).message))
    }
  }

  if (loading) return <Loading />

  const issues = current?.rule_result?.issues ?? []
  const stats = current?.rule_result?.stats ?? {}

  return (
    <div>
      <h1 className="mb-4 text-[20px] font-semibold">AI 复盘</h1>
      {error && <ErrorBox message={error} />}

      {/* 触发复盘 */}
      <Card title="触发复盘(规则诊断 + LLM 深度分析)">
        <div className="flex flex-wrap items-end gap-2">
          <label className="flex items-center gap-1.5 text-[13px]">
            复盘范围
            <select value={scope} onChange={(e) => setScope(e.target.value)} style={inputStyle} className="w-24">
              <option value="week">本周</option>
              <option value="month">本月</option>
              <option value="all">全部</option>
            </select>
          </label>
          <Button onClick={run} disabled={running}>{running ? '复盘中...' : '开始复盘'}</Button>
          <span className="text-xs text-ink-muted">
            {task && task.status === 'running' ? `进度 ${task.progress}%` : (cfg?.enabled && cfg.has_key ? `LLM 已启用(${cfg.model})` : 'LLM 未启用, 仅规则诊断')}
          </span>
        </div>
      </Card>

      {/* 规则诊断 + LLM 结果 */}
      {current && (
        <Card title={`复盘结果 #${current.id} · ${current.time}`} className="mt-3">
          <div className="mb-3 flex flex-wrap gap-4">
            <Metric label="交易笔数" value={String(stats.trades ?? '-')} />
            <Metric label="已平仓" value={String(stats.closed ?? '-')} />
            <Metric label="胜率" value={`${stats.win_rate ?? '-'}%`} />
            <Metric label="总盈亏" value={(stats.total_pnl ?? 0) >= 0 ? `+${stats.total_pnl}` : String(stats.total_pnl)} color={(stats.total_pnl ?? 0) >= 0 ? '#dc2626' : '#16a34a'} />
          </div>

          <div className="mb-2 text-[13px] font-medium">规则诊断({issues.length})</div>
          {issues.length === 0 ? (
            <EmptyState>未发现规则问题(止损/追高/频繁/逆势均正常)。</EmptyState>
          ) : (
            issues.map((it, i) => {
              const meta = LEVEL_META[it.level] ?? LEVEL_META.low
              return (
                <div key={i} className="flex items-start gap-2.5 border-b border-divider py-2 text-[13px] last:border-b-0">
                  <Tag color={meta.color}>{meta.label}</Tag>
                  <div className="flex-1">
                    <div className="font-medium">{it.title}</div>
                    <div className="mt-0.5 text-ink-secondary">{it.detail}</div>
                  </div>
                </div>
              )
            })
          )}

          <div className="mb-2 mt-3 text-[13px] font-medium">
            LLM 分析{current.model ? `(${current.model})` : ''}
          </div>
          <div className="rounded-lg bg-slate-50 p-3 text-[13px] leading-[1.8] text-ink" style={{ whiteSpace: 'pre-wrap' }}>
            {current.content}
          </div>

          {current.suggestions.length > 0 && (
            <>
              <div className="mb-2 mt-3 text-[13px] font-medium">改进建议(点击标记是否采纳)</div>
              {current.suggestions.map((sg, i) => (
                <div key={i} className="flex items-center gap-2.5 border-b border-divider py-2 text-[13px] last:border-b-0">
                  <span className="flex-1">{sg.text}</span>
                  {sg.status === 'pending' && (
                    <span className="flex gap-1.5">
                      <Button kind="primary" onClick={() => markSuggestion(i, 'accepted')} style={{ padding: '4px 10px', fontSize: 12 }}>采纳</Button>
                      <Button kind="ghost" onClick={() => markSuggestion(i, 'rejected')} style={{ padding: '4px 10px', fontSize: 12 }}>忽略</Button>
                    </span>
                  )}
                  {sg.status === 'accepted' && <Tag color="#16a34a">已采纳</Tag>}
                  {sg.status === 'rejected' && <Tag color="#888">已忽略</Tag>}
                </div>
              ))}
            </>
          )}
        </Card>
      )}

      {/* LLM 配置 */}
      <Card title="LLM 配置(兼容 OpenAI 协议: DeepSeek / 通义 / Kimi / Ollama)" className="mt-3">
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <Field label="API 地址">
            <input style={inputStyle} value={cfgForm.base_url} onChange={(e) => setCfgForm({ ...cfgForm, base_url: e.target.value })} placeholder="如 https://api.deepseek.com/v1" />
          </Field>
          <Field label={`API Key(${cfg?.has_key ? '已配置, 留空则不修改' : '未配置'})`}>
            <input style={inputStyle} type="password" value={cfgForm.api_key} onChange={(e) => setCfgForm({ ...cfgForm, api_key: e.target.value })} placeholder={cfg?.has_key ? 'sk-****(已保存)' : '输入 DeepSeek Key'} />
          </Field>
          <Field label="模型">
            <input style={inputStyle} value={cfgForm.model} onChange={(e) => setCfgForm({ ...cfgForm, model: e.target.value })} placeholder="如 deepseek-chat" />
          </Field>
          <Field label="启用 LLM">
            <label className="flex items-center gap-2 pt-2 text-[13px]">
              <input type="checkbox" checked={cfgForm.enabled} onChange={(e) => setCfgForm({ ...cfgForm, enabled: e.target.checked })} />
              启用深度复盘(不启用则只做规则诊断)
            </label>
          </Field>
        </div>
        <div className="mt-2 flex items-center gap-3">
          <Button onClick={saveConfig}>保存配置</Button>
          {cfgSaved && <span className="text-xs text-fall">{cfgSaved}</span>}
        </div>
        <div className="mt-2 text-[11px] text-ink-faint">
          默认指向 DeepSeek(base_url 留空 + 填入 Key 即可)。Key 仅存本机数据库, 接口回传时脱敏。
        </div>
      </Card>

      {/* 历史复盘 */}
      <Card title={`复盘历史(${history.length})`} className="mt-3">
        {history.length === 0 ? (
          <EmptyState>暂无复盘记录, 点击「开始复盘」生成第一份。</EmptyState>
        ) : (
          history.map((r) => (
            <ListRow key={r.id} className="py-2.5">
              <span className="flex items-center gap-2.5">
                <span className="font-semibold">#{r.id}</span>
                <span className="text-ink-muted">{r.time}</span>
                <Tag color="#64748b">范围: {r.range}</Tag>
                {r.rule_result?.issues?.length ? <Tag color="#ea580c">规则问题 {r.rule_result.issues.length}</Tag> : null}
              </span>
              <Button kind="ghost" onClick={() => setCurrent(r)} style={{ padding: '4px 10px', fontSize: 12 }}>查看</Button>
            </ListRow>
          ))
        )}
      </Card>
    </div>
  )
}

function Metric({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="text-[13px]">
      <div className="text-xs text-ink-muted">{label}</div>
      <div className="text-[16px] font-bold" style={{ color: color || '#333' }}>{value}</div>
    </div>
  )
}
