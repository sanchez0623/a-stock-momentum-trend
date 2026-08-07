import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { AiReviewConfig, AiReviewRecord, AiReviewTask } from '../api/client'
import { Button, Card, ErrorBox, Field, Loading, Tag, inputStyle, toast } from '../components/ui'

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
      <h1 style={{ fontSize: 20, marginBottom: 16 }}>AI 复盘</h1>
      {error && <ErrorBox message={error} />}

      {/* 触发复盘 */}
      <Card title="触发复盘(规则诊断 + LLM 深度分析)">
        <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end', flexWrap: 'wrap' }}>
          <label style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 6 }}>
            复盘范围
            <select value={scope} onChange={(e) => setScope(e.target.value)} style={inputStyle}>
              <option value="week">本周</option>
              <option value="month">本月</option>
              <option value="all">全部</option>
            </select>
          </label>
          <Button onClick={run} disabled={running}>{running ? '复盘中...' : '开始复盘'}</Button>
          <span style={{ fontSize: 12, color: '#888' }}>
            {task && task.status === 'running' ? `进度 ${task.progress}%` : (cfg?.enabled && cfg.has_key ? `LLM 已启用(${cfg.model})` : 'LLM 未启用, 仅规则诊断')}
          </span>
        </div>
      </Card>

      {/* 规则诊断 + LLM 结果 */}
      {current && (
        <>
          <Card title={`复盘结果 #${current.id} · ${current.time}`} style={{ marginTop: 12 }}>
            <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', marginBottom: 12 }}>
              <Metric label="交易笔数" value={String(stats.trades ?? '-')} />
              <Metric label="已平仓" value={String(stats.closed ?? '-')} />
              <Metric label="胜率" value={`${stats.win_rate ?? '-'}%`} />
              <Metric label="总盈亏" value={(stats.total_pnl ?? 0) >= 0 ? `+${stats.total_pnl}` : String(stats.total_pnl)} color={(stats.total_pnl ?? 0) >= 0 ? '#dc2626' : '#16a34a'} />
            </div>

            <div style={{ fontSize: 13, fontWeight: 500, marginBottom: 8 }}>规则诊断({issues.length})</div>
            {issues.length === 0 ? (
              <div style={{ color: '#999', fontSize: 13, marginBottom: 12 }}>未发现规则问题(止损/追高/频繁/逆势均正常)。</div>
            ) : (
              issues.map((it, i) => {
                const meta = LEVEL_META[it.level] ?? LEVEL_META.low
                return (
                  <div key={i} style={{ display: 'flex', gap: 10, alignItems: 'flex-start', padding: '8px 0', borderBottom: '1px solid #f0f1f3', fontSize: 13 }}>
                    <Tag color={meta.color}>{meta.label}</Tag>
                    <div style={{ flex: 1 }}>
                      <div style={{ fontWeight: 500 }}>{it.title}</div>
                      <div style={{ color: '#666', marginTop: 2 }}>{it.detail}</div>
                    </div>
                  </div>
                )
              })
            )}

            <div style={{ fontSize: 13, fontWeight: 500, margin: '12px 0 8px' }}>
              LLM 分析{current.model ? `(${current.model})` : ''}
            </div>
            <div style={{ fontSize: 13, lineHeight: 1.8, whiteSpace: 'pre-wrap', color: '#333', background: '#f8fafc', borderRadius: 8, padding: 12 }}>
              {current.content}
            </div>

            {current.suggestions.length > 0 && (
              <>
                <div style={{ fontSize: 13, fontWeight: 500, margin: '12px 0 8px' }}>改进建议(点击标记是否采纳)</div>
                {current.suggestions.map((sg, i) => (
                  <div key={i} style={{ display: 'flex', gap: 10, alignItems: 'center', padding: '8px 0', borderBottom: '1px solid #f0f1f3', fontSize: 13 }}>
                    <span style={{ flex: 1 }}>{sg.text}</span>
                    {sg.status === 'pending' && (
                      <span style={{ display: 'flex', gap: 6 }}>
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
        </>
      )}

      {/* LLM 配置 */}
      <Card title="LLM 配置(兼容 OpenAI 协议: DeepSeek / 通义 / Kimi / Ollama)" style={{ marginTop: 12 }}>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
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
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, paddingTop: 8 }}>
              <input type="checkbox" checked={cfgForm.enabled} onChange={(e) => setCfgForm({ ...cfgForm, enabled: e.target.checked })} />
              启用深度复盘(不启用则只做规则诊断)
            </label>
          </Field>
        </div>
        <div style={{ display: 'flex', gap: 12, alignItems: 'center', marginTop: 8 }}>
          <Button onClick={saveConfig}>保存配置</Button>
          {cfgSaved && <span style={{ color: '#16a34a', fontSize: 12 }}>{cfgSaved}</span>}
        </div>
        <div style={{ fontSize: 11, color: '#999', marginTop: 8 }}>
          默认指向 DeepSeek(base_url 留空 + 填入 Key 即可)。Key 仅存本机数据库, 接口回传时脱敏。
        </div>
      </Card>

      {/* 历史复盘 */}
      <Card title={`复盘历史(${history.length})`} style={{ marginTop: 12 }}>
        {history.length === 0 ? (
          <div style={{ color: '#999', fontSize: 13 }}>暂无复盘记录, 点击「开始复盘」生成第一份。</div>
        ) : (
          history.map((r) => (
            <div key={r.id} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '10px 0', borderBottom: '1px solid #f0f1f3', fontSize: 13 }}>
              <span style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
                <span style={{ fontWeight: 600 }}>#{r.id}</span>
                <span style={{ color: '#888' }}>{r.time}</span>
                <Tag color="#64748b">范围: {r.range}</Tag>
                {r.rule_result?.issues?.length ? <Tag color="#ea580c">规则问题 {r.rule_result.issues.length}</Tag> : null}
              </span>
              <Button kind="ghost" onClick={() => setCurrent(r)} style={{ padding: '4px 10px', fontSize: 12 }}>查看</Button>
            </div>
          ))
        )}
      </Card>
    </div>
  )
}

function Metric({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div style={{ fontSize: 13 }}>
      <div style={{ color: '#888', fontSize: 12 }}>{label}</div>
      <div style={{ fontWeight: 700, fontSize: 16, color: color || '#333' }}>{value}</div>
    </div>
  )
}
