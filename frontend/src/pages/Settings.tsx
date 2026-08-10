import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { api } from '../api/client'
import { CONFIG_GROUPS, DATA_SOURCE_LABELS } from '../const/configSchema'
import type { FieldMeta } from '../const/configSchema'
import { Button, Card, ErrorBox, Field, Loading, PageHeader, Tag, cn, inputStyle, toast } from '../components/ui'

// 配置树为任意 JSON(no-explicit-any 已在 eslint 配置中关闭), 此处放宽类型
// 注意: 注释勿以 "eslint" 开头, 会被当作 inline directive 解析报错
type AnyRec = Record<string, any>

const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v)) as T

/** 浮点显示: 消除 0.00005×10000=0.5000000000000001 这类二进制误差 */
function fmtNum(v: unknown, scale: number): string {
  const n = Number(v)
  if (v === null || v === undefined || Number.isNaN(n)) return ''
  return String(Number((n * scale).toPrecision(12)))
}

/**
 * api_key 归一化: 后端 GET 回传脱敏占位符 ``******``(已配置) 或 ``''``(未配置),
 * 两者都代表"不修改", 归一到同一哨兵值后再比对, 避免产生幽灵脏值。
 */
const KEEP = '\u0000KEEP'
function groupSnapshot(groupKey: string, g: unknown): string {
  if (g === undefined) return ''
  if (groupKey === 'llm') {
    const c = { ...(g as AnyRec) }
    if (c.api_key === '' || c.api_key === '******') c.api_key = KEEP
    return JSON.stringify(c)
  }
  return JSON.stringify(g)
}

/* ------------------------------------------------------------------ 输入控件 */

/** 数字输入: 维护本地文本缓冲, 允许 "0." / "-" 等中间态, 失焦时钳位并归一 */
function NumInput({ value, meta, onChange }: {
  value: unknown
  meta: FieldMeta
  onChange: (v: number) => void
}) {
  const scale = meta.scale ?? 1
  const [text, setText] = useState(() => fmtNum(value, scale))
  const [focused, setFocused] = useState(false)

  useEffect(() => {
    if (!focused) setText(fmtNum(value, scale))
  }, [value, scale, focused])

  const onType = (raw: string) => {
    setText(raw)
    const t = raw.trim()
    if (t === '' || t === '-' || t.endsWith('.')) return // 中间态不回写
    const n = Number(t)
    if (!Number.isNaN(n)) onChange(n / scale)
  }

  const onBlur = () => {
    setFocused(false)
    let n = Number(text)
    if (text.trim() === '' || Number.isNaN(n)) {
      setText(fmtNum(value, scale))
      return
    }
    if (meta.type === 'int') n = Math.round(n)
    if (meta.min !== undefined && n < meta.min) n = meta.min
    if (meta.max !== undefined && n > meta.max) n = meta.max
    onChange(n / scale)
    setText(String(Number(n.toPrecision(12))))
  }

  return (
    <input
      style={inputStyle}
      value={text}
      inputMode="decimal"
      onFocus={() => setFocused(true)}
      onBlur={onBlur}
      onChange={(e) => onType(e.target.value)}
    />
  )
}

/** 数字数组输入: 逗号分隔 */
function ListInput({ value, onChange }: { value: unknown; onChange: (v: number[]) => void }) {
  const arr = Array.isArray(value) ? (value as number[]) : []
  const [text, setText] = useState(() => arr.join(', '))
  const [focused, setFocused] = useState(false)

  useEffect(() => {
    if (!focused) setText((Array.isArray(value) ? (value as number[]) : []).join(', '))
  }, [value, focused])

  const onType = (raw: string) => {
    setText(raw)
    const parts = raw.split(',').map((s) => s.trim()).filter((s) => s !== '')
    if (parts.length === 0 || parts.some((p) => Number.isNaN(Number(p)))) return
    onChange(parts.map(Number))
  }

  return (
    <input
      style={inputStyle}
      value={text}
      placeholder="用英文逗号分隔, 如 0.5, 0.3, 0.2"
      onFocus={() => setFocused(true)}
      onBlur={() => setFocused(false)}
      onChange={(e) => onType(e.target.value)}
    />
  )
}

/** 多行文本 -> 字符串数组(每行一条) */
function LinesInput({ value, onChange, placeholder }: {
  value: unknown
  onChange: (v: string[]) => void
  placeholder?: string
}) {
  const [text, setText] = useState(() => (Array.isArray(value) ? (value as string[]).join('\n') : ''))
  const [focused, setFocused] = useState(false)

  useEffect(() => {
    if (!focused) setText(Array.isArray(value) ? (value as string[]).join('\n') : '')
  }, [value, focused])

  const onType = (raw: string) => {
    setText(raw)
    onChange(raw.split('\n').map((s) => s.trim()).filter(Boolean))
  }

  return (
    <textarea
      style={{ ...inputStyle, minHeight: 72, fontFamily: 'ui-monospace, Menlo, Consolas, monospace' }}
      value={text}
      placeholder={placeholder}
      onFocus={() => setFocused(true)}
      onBlur={() => setFocused(false)}
      onChange={(e) => onType(e.target.value)}
    />
  )
}

/* ------------------------------------------------------------------ 校验 */

const sum = (a: unknown): number =>
  (Array.isArray(a) ? (a as number[]) : []).reduce((s, x) => s + (Number(x) || 0), 0)

function validate(cfg: AnyRec | null): string[] {
  if (!cfg) return []
  const errs: string[] = []
  const G = (k: string): AnyRec => (cfg[k] ?? {}) as AnyRec

  const t = G('趋势')
  if (!(Number(t.ma_short) < Number(t.ma_mid) && Number(t.ma_mid) < Number(t.ma_long))) {
    errs.push('趋势: 均线周期需满足 短期 < 中期 < 长期')
  }

  const m = G('动量')
  if (Number(m.macd_fast) >= Number(m.macd_slow)) errs.push('动量: MACD 快线周期必须小于慢线周期')
  if (Number(m.rsi_oversold) >= Number(m.rsi_overbought)) errs.push('动量: RSI 超卖线必须小于超买线')

  const r = G('风控')
  if (Number(r.single_position_pct) > Number(r.total_position_pct)) {
    errs.push('风控: 单票仓位上限不应大于总仓位上限')
  }

  const p = G('仓位')
  if (p.strategy === 'pyramid') {
    const ps = sum(p.pyramid_ratios)
    if (Math.abs(ps - 1) > 0.001) errs.push(`仓位: 金字塔分批比例之和须为 1.00, 当前 ${ps.toFixed(3)}`)
  }
  const tps = sum(p.take_profit_ratios)
  if (tps > 1.001) errs.push(`仓位: 各档减仓比例之和不能超过 1.00, 当前 ${tps.toFixed(3)}`)
  const lv = Array.isArray(p.take_profit_levels) ? (p.take_profit_levels as number[]) : []
  if (p.take_profit_mode === 'fixed' && lv.some((v, i) => i > 0 && v <= lv[i - 1])) {
    errs.push('仓位: 固定止盈档位必须递增')
  }
  const am = Array.isArray(p.atr_multipliers) ? (p.atr_multipliers as number[]) : []
  if (p.take_profit_mode === 'atr' && am.some((v, i) => i > 0 && v <= am[i - 1])) {
    errs.push('仓位: ATR 止盈倍数必须递增')
  }

  const w = G('评分权重')
  const ws = ['timing', 'position', 'stop', 'profit', 'discipline']
    .reduce((s, k) => s + (Number(w[k]) || 0), 0)
  if (Math.abs(ws - 1) > 0.001) errs.push(`评分权重: 五项之和须为 1.00, 当前 ${ws.toFixed(3)}`)

  const se = G('趋势阶段')
  if (Number(se.rsi_exhaust) <= Number(se.rsi_overheat)) errs.push('趋势阶段: RSI 衰竭线必须大于过热线')

  const ds = G('数据源')
  const en = (ds.enabled ?? {}) as Record<string, boolean>
  if (!Object.values(en).some(Boolean)) errs.push('数据源: 至少需要启用一个数据源')
  const pr = Array.isArray(ds.priority) ? (ds.priority as string[]) : []
  if (pr.length && !pr.some((n) => en[n])) errs.push('数据源: 优先级列表中没有任何已启用的数据源')

  const llm = G('llm')
  if (llm.enabled && !String(llm.base_url || '').trim()) errs.push('AI 复盘: 启用后必须填写 API 地址')
  if (llm.enabled && !String(llm.model || '').trim()) errs.push('AI 复盘: 启用后必须填写模型名')

  const f = G('手续费')
  if (Number(f.commission_min) < 0) errs.push('手续费: 单笔最低佣金不能为负')

  return errs
}

/* ------------------------------------------------------------------ 数据源面板 */

function DataSourcePanel({ ds, onChange }: {
  ds: AnyRec
  onChange: (path: string[], v: unknown) => void
}) {
  const [testing, setTesting] = useState('')
  const priority: string[] = Array.isArray(ds.priority) ? ds.priority : []
  const enabled: Record<string, boolean> = (ds.enabled ?? {}) as Record<string, boolean>
  const em: AnyRec = (ds.eastmoney ?? {}) as AnyRec

  const move = (i: number, dir: -1 | 1) => {
    const j = i + dir
    if (j < 0 || j >= priority.length) return
    const next = [...priority]
    const tmp = next[i]
    next[i] = next[j]
    next[j] = tmp
    onChange(['priority'], next)
  }

  const test = async (name: string) => {
    setTesting(name)
    try {
      await api.testSource(name)
      toast.success(`${name} 连通正常`)
    } catch (e) {
      toast.error(`${name} 测试失败: ${(e as Error).message}`)
    } finally {
      setTesting('')
    }
  }

  return (
    <div>
      <div className="mb-1.5 text-[13px] font-medium">优先级与开关</div>
      <div className="mb-1 text-[11px] text-ink-faint">
        自上而下依次尝试，前一个失败或被熔断时自动降级到下一个。
      </div>
      <div className="mb-4 overflow-hidden rounded-lg border border-line">
        {priority.map((name, i) => (
          <div
            key={name}
            className={cn('flex flex-wrap items-center gap-2 px-3 py-2 text-[13px]', i > 0 && 'border-t border-divider')}
          >
            <span className="w-5 shrink-0 text-center text-xs text-ink-faint">{i + 1}</span>
            <label className="flex flex-1 items-center gap-2">
              <input
                type="checkbox"
                checked={!!enabled[name]}
                onChange={(e) => onChange(['enabled', name], e.target.checked)}
              />
              <span className={cn(!enabled[name] && 'text-ink-faint line-through')}>
                {DATA_SOURCE_LABELS[name] ?? name}
              </span>
            </label>
            {i === 0 && enabled[name] && <Tag color="#16a34a">主用</Tag>}
            <span className="flex shrink-0 gap-1">
              <Button kind="ghost" onClick={() => move(i, -1)} disabled={i === 0} style={{ padding: '2px 8px', fontSize: 12 }} className="h-6">↑</Button>
              <Button kind="ghost" onClick={() => move(i, 1)} disabled={i === priority.length - 1} style={{ padding: '2px 8px', fontSize: 12 }} className="h-6">↓</Button>
              <Button kind="ghost" onClick={() => test(name)} disabled={testing === name} style={{ padding: '2px 8px', fontSize: 12 }} className="h-6">
                {testing === name ? '测试中' : '测试'}
              </Button>
            </span>
          </div>
        ))}
      </div>

      <div className="mb-1.5 text-[13px] font-medium">东方财富专项限流</div>
      <div className="mb-2 text-[11px] text-ink-faint">
        东财有连接级风控，需降频 + 串行访问，否则易被临时封禁。
      </div>
      <div className="grid grid-cols-1 gap-x-5 md:grid-cols-2 xl:grid-cols-4">
        <Field label="请求间隔 (秒)">
          <NumInput
            value={em.interval_sec}
            meta={{ key: 'interval_sec', label: '', type: 'float', min: 0, max: 30, scale: 1 }}
            onChange={(v) => onChange(['eastmoney', 'interval_sec'], v)}
          />
          <div className="mt-1 text-[11px] leading-snug text-ink-faint">两次请求最小间隔，建议 ≥ 2</div>
        </Field>
        <Field label="并发数">
          <NumInput
            value={em.max_workers}
            meta={{ key: 'max_workers', label: '', type: 'int', min: 1, max: 16 }}
            onChange={(v) => onChange(['eastmoney', 'max_workers'], v)}
          />
          <div className="mt-1 text-[11px] leading-snug text-ink-faint">建议保持 1（串行）</div>
        </Field>
        <Field label="重试次数">
          <NumInput
            value={em.retry}
            meta={{ key: 'retry', label: '', type: 'int', min: 0, max: 10 }}
            onChange={(v) => onChange(['eastmoney', 'retry'], v)}
          />
        </Field>
        <Field label="启用反风控补丁">
          <label className="flex items-center gap-2 pt-1 text-[13px]">
            <input
              type="checkbox"
              checked={!!em.enable_patch}
              onChange={(e) => onChange(['eastmoney', 'enable_patch'], e.target.checked)}
            />
            UA + NID 伪装
          </label>
        </Field>
      </div>

      <div className="mb-1.5 mt-2 text-[13px] font-medium">代理池</div>
      <LinesInput
        value={ds.proxy_pool}
        onChange={(v) => onChange(['proxy_pool'], v)}
        placeholder={'每行一个代理地址，留空表示不使用\nhttp://127.0.0.1:7890'}
      />
      <div className="mt-1 text-[11px] text-ink-faint">被数据源限流时轮换出口 IP，一般本机自用可留空。</div>
    </div>
  )
}

/* ------------------------------------------------------------------ 主页面 */

export default function Settings() {
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [original, setOriginal] = useState<AnyRec | null>(null)
  const [draft, setDraft] = useState<AnyRec | null>(null)
  const [active, setActive] = useState(CONFIG_GROUPS[0].key)

  // 配置加载: useQuery 管理; draft/original 为本地编辑态, 仅首次填充
  const { data: cfgData, isLoading, error: cfgQueryError } = useQuery({
    queryKey: ['config'],
    queryFn: api.config,
  })
  const { data: defaults } = useQuery({
    queryKey: ['config-defaults'],
    queryFn: api.configDefaults,
    retry: false,
  })
  const initializedRef = useRef(false)
  useEffect(() => {
    if (cfgData && !initializedRef.current) {
      initializedRef.current = true
      setOriginal(cfgData as AnyRec)
      setDraft(clone(cfgData) as AnyRec)
    }
  }, [cfgData])
  useEffect(() => {
    if (cfgQueryError) setError(String((cfgQueryError as Error).message || cfgQueryError))
  }, [cfgQueryError])

  const dirty = useMemo(() => {
    const s = new Set<string>()
    if (!draft || !original) return s
    for (const g of CONFIG_GROUPS) {
      if (groupSnapshot(g.key, draft[g.key]) !== groupSnapshot(g.key, original[g.key])) s.add(g.key)
    }
    return s
  }, [draft, original])

  const errors = useMemo(() => validate(draft), [draft])

  const setValue = (group: string, path: string[], value: unknown) => {
    setDraft((d) => {
      if (!d) return d
      const next: AnyRec = { ...d }
      let node: AnyRec = { ...((next[group] ?? {}) as AnyRec) }
      next[group] = node
      for (let i = 0; i < path.length - 1; i++) {
        const child: AnyRec = { ...((node[path[i]] ?? {}) as AnyRec) }
        node[path[i]] = child
        node = child
      }
      node[path[path.length - 1]] = value
      return next
    })
  }

  const save = async () => {
    if (!draft || dirty.size === 0) return
    if (errors.length > 0) {
      toast.error('配置校验未通过，请先修正下方问题')
      return
    }
    const payload: AnyRec = {}
    dirty.forEach((g) => { payload[g] = clone(draft[g]) })
    // 脱敏占位符不回传, 避免覆盖真实 Key(后端亦有兜底)
    if (payload.llm && (payload.llm.api_key === '******' || payload.llm.api_key === '')) {
      delete payload.llm.api_key
    }
    setSaving(true)
    setError('')
    try {
      const fresh = (await api.updateConfig(payload)) as AnyRec
      setOriginal(fresh)
      setDraft(clone(fresh))
      toast.success(`已保存 ${dirty.size} 组配置，热生效`)
    } catch (e) {
      const msg = String((e as Error).message || e)
      setError(msg)
      toast.error(`保存失败: ${msg}`)
    } finally {
      setSaving(false)
    }
  }

  const discard = () => {
    if (!original) return
    setDraft(clone(original))
    toast.info('已放弃未保存的修改')
  }

  const restoreGroupDefault = () => {
    if (!defaults || !defaults[active]) {
      toast.error('未获取到默认配置')
      return
    }
    const label = CONFIG_GROUPS.find((g) => g.key === active)?.label ?? active
    setDraft((d) => (d ? { ...d, [active]: clone(defaults[active]) } : d))
    toast.info(`已载入「${label}」默认值，保存后生效`)
  }

  if (isLoading) return <Loading />
  if (!draft) return <ErrorBox message={error || '配置加载失败'} />

  const group = CONFIG_GROUPS.find((g) => g.key === active) ?? CONFIG_GROUPS[0]
  const gv: AnyRec = (draft[group.key] ?? {}) as AnyRec
  const weightSum = ['timing', 'position', 'stop', 'profit', 'discipline']
    .reduce((s, k) => s + (Number(((draft['评分权重'] ?? {}) as AnyRec)[k]) || 0), 0)

  const renderField = (meta: FieldMeta) => {
    const val = gv[meta.key]
    const set = (v: unknown) => setValue(group.key, [meta.key], v)
    const label = meta.unit ? `${meta.label} (${meta.unit})` : meta.label

    let control: ReactNode
    switch (meta.type) {
      case 'bool':
        control = (
          <label className="flex items-center gap-2 pt-1 text-[13px]">
            <input type="checkbox" checked={!!val} onChange={(e) => set(e.target.checked)} />
            {val ? '已启用' : '已关闭'}
          </label>
        )
        break
      case 'select':
        control = (
          <select style={inputStyle} value={String(val ?? '')} onChange={(e) => set(e.target.value)}>
            {meta.options?.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        )
        break
      case 'numlist':
        control = <ListInput value={val} onChange={set} />
        break
      case 'text':
        control = <input style={inputStyle} value={String(val ?? '')} onChange={(e) => set(e.target.value)} />
        break
      case 'password':
        control = (
          <input
            style={inputStyle}
            type="password"
            value={val === '******' ? '' : String(val ?? '')}
            placeholder={original?.llm?.api_key === '******' ? '已配置 · 留空则不修改' : '未配置，请填入 Key'}
            onChange={(e) => set(e.target.value)}
          />
        )
        break
      default:
        control = <NumInput value={val} meta={meta} onChange={set} />
    }

    return (
      <Field key={meta.key} label={label}>
        {control}
        {(meta.hint || meta.listHint) && (
          <div className="mt-1 text-[11px] leading-snug text-ink-faint">{meta.hint || meta.listHint}</div>
        )}
      </Field>
    )
  }

  return (
    <div>
      <PageHeader
        title="设置"
        subtitle="所有参数保存后热生效，无需重启；直接影响下一轮选股、信号与计划。"
      />
      {error && <ErrorBox message={error} />}

      {/* 分组切换 */}
      <div className="mb-3 flex flex-wrap gap-1.5">
        {CONFIG_GROUPS.map((g) => (
          <button
            key={g.key}
            onClick={() => setActive(g.key)}
            className={cn(
              'rounded-full px-3 py-1.5 text-[13px] transition-colors',
              active === g.key
                ? 'bg-link text-white'
                : 'border border-line bg-white text-ink hover:border-link hover:text-link',
            )}
          >
            {g.label}
            {dirty.has(g.key) && (
              <span className={cn('ml-1.5 inline-block h-1.5 w-1.5 rounded-full align-middle',
                active === g.key ? 'bg-white' : 'bg-primary')}
              />
            )}
          </button>
        ))}
      </div>

      <Card
        title={group.label}
        extra={<Button kind="ghost" onClick={restoreGroupDefault} style={{ padding: '4px 10px', fontSize: 12 }} className="h-7">恢复默认</Button>}
      >
        <div className="mb-3 text-xs leading-relaxed text-ink-muted">{group.desc}</div>

        {group.custom === 'datasource' ? (
          <DataSourcePanel ds={gv} onChange={(path, v) => setValue(group.key, path, v)} />
        ) : (
          <>
            <div className="grid grid-cols-1 gap-x-5 md:grid-cols-2 xl:grid-cols-3">
              {group.fields.map(renderField)}
            </div>
            {group.key === '评分权重' && (
              <div className="mt-1 text-[13px]">
                权重合计{' '}
                <span className={cn('font-semibold tabular-nums', Math.abs(weightSum - 1) > 0.001 ? 'text-rise' : 'text-fall')}>
                  {weightSum.toFixed(3)}
                </span>
                <span className="ml-2 text-[11px] text-ink-faint">须等于 1.000 才能保存</span>
              </div>
            )}
          </>
        )}
      </Card>

      {errors.length > 0 && (
        <Card title={`校验未通过 (${errors.length})`} className="mt-3">
          <ul className="list-inside list-disc text-[13px] leading-relaxed text-rise">
            {errors.map((e) => <li key={e}>{e}</li>)}
          </ul>
        </Card>
      )}

      {/* 粘性操作栏 */}
      <div className="sticky bottom-0 z-10 -mx-4 mt-3 flex flex-wrap items-center gap-3 border-t border-line bg-white/95 px-4 py-3 backdrop-blur md:-mx-6 md:px-6">
        <Button onClick={save} disabled={saving || dirty.size === 0 || errors.length > 0}>
          {saving ? '保存中...' : '保存修改'}
        </Button>
        <Button kind="ghost" onClick={discard} disabled={saving || dirty.size === 0}>放弃修改</Button>
        <span className="text-xs text-ink-muted">
          {dirty.size === 0
            ? '当前无未保存修改'
            : `${dirty.size} 组待保存: ${CONFIG_GROUPS.filter((g) => dirty.has(g.key)).map((g) => g.label).join('、')}`}
        </span>
      </div>
    </div>
  )
}
