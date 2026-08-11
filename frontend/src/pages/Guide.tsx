import { Card, PageHeader } from '../components/ui'

// 交易说明书: 系统化规则体系(风控/信号/模式/仓位/T+1/申报规则/计划字段)
// 与代码规则同源, 供人工核对; 字段级即时提示见各页 hover 提示
const GATES = [
  { name: '日亏损熔断', rule: '单日账户浮亏 ≥ 3%', effect: '当天禁止一切新开仓(已有持仓的止损/减仓照常)', config: '风控.daily_loss_limit_pct' },
  { name: '连亏降仓', rule: '连续亏损 ≥ 3 笔', effect: '单票仓位建议减半', config: '风控.consecutive_loss_limit' },
  { name: '回撤防守', rule: '净值从高点回撤 ≥ 10%', effect: '进入防守模式: 只减不加, 买入类信号全部拦截', config: '风控.max_drawdown_pct' },
]

const SIGNALS = [
  { type: 'SELL_STOP', name: '止损', pri: 1, desc: 'MA短穿中且ADX掉头等破位形态, 立即止损清仓' },
  { type: 'SELL_REDUCE', name: '减仓', pri: 2, desc: '冲高回落/跌破关键均线, 建议减 1/3~1/2 仓位' },
  { type: 'BUY_ADD', name: '加仓', pri: 3, desc: '回踩企稳/沿均线强势, 按金字塔档位顺向加仓' },
  { type: 'BUY_FIRST', name: '首仓', pri: 4, desc: '三共振入场(趋势/动量/量能), 金字塔第一档建仓' },
  { type: 'T_BUY / T_SELL', name: '做T', pri: 5, desc: '当日低吸高抛降成本, 不留隔夜增量' },
]

const MODES = [
  { key: 'trend_strong', name: '趋势强攻', cond: 'ADX≥30 + 距20日高≤3% + 放量', style: '重仓追强, 止损6%' },
  { key: 'trend_pullback', name: '趋势回踩', cond: 'ADX≥18 + 多头排列 + 距高3~8%', style: '回踩加仓, 止损5%' },
  { key: 'range', name: '震荡', cond: 'ADX<18 无明确趋势', style: '轻仓小加, 止损4%' },
  { key: 'defense', name: '防守', cond: '-DI>+DI 空头占优', style: '仅执行止损, 不加仓不新建' },
  { key: 'unknown', name: '市况不明', cond: '均线/位置无明确模式特征', style: '买入观望, 止损/减仓照常' },
]

const PYRAMID = [
  { stage: '第 1 档', pct: '50%', desc: '首仓建仓(趋势强攻/回踩模式)' },
  { stage: '第 2 档', pct: '30%', desc: '回踩企稳后第一次加仓' },
  { stage: '第 3 档', pct: '20%', desc: '趋势确认后最后一次加仓' },
]

const PLAN_FIELDS = [
  { name: '建议操作', desc: '本计划核心: 具体做什么(建仓/加仓/减仓/止损/观望), 以行动优先' },
  { name: '触发价位', desc: '现价 ±0.5% 内的参考成交区' },
  { name: '止损价位', desc: '跌破即离场(成本下移 N% 或模式止损线), 执行不犹豫' },
  { name: '止盈计划', desc: '分批止盈档位: ATR 动态档或固定档, 早期少减让利润奔跑' },
  { name: '风控检查', desc: '三道闸门状态: 日亏损熔断/防守模式/总仓位上限' },
  { name: '一致性', desc: '信号建议动作与计划动作的对应关系; 不一致会注明原因' },
  { name: '纪律提醒', desc: '信号与计划一致时才动手; 加仓后总仓位不超上限' },
]

const LOT_RULES = [
  { board: '科创板(688/689)', buy: '≥200 股, 1 股递增', sell: '剩余不足 200 股须一次性清仓' },
  { board: '北交所(43/83/87/88/92)', buy: '≥100 股, 1 股递增', sell: '剩余不足 100 股须一次性清仓' },
  { board: '主板/创业板', buy: '100 股整数倍', sell: '剩余不足 100 股须一次性清仓' },
]

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Card className="mb-3">
      <div className="mb-2 text-[14px] font-semibold">{title}</div>
      {children}
    </Card>
  )
}

function Th({ children }: { children: React.ReactNode }) {
  return <th className="px-2 py-1.5 text-left font-medium text-ink-muted">{children}</th>
}

function Td({ children, className }: { children: React.ReactNode; className?: string }) {
  return <td className={`px-2 py-1.5 align-top text-[12.5px] leading-snug ${className ?? ''}`}>{children}</td>
}

export default function Guide() {
  return (
    <div>
      <PageHeader title="交易说明书" />
      <div className="mb-3 text-xs text-ink-muted">
        系统化交易规则体系(与代码规则同源)。页面字段上的悬停提示可即时查看单个术语含义。
      </div>

      <Section title="① 风控三道闸门(交易前自动拦截)">
        <table className="w-full border-collapse">
          <thead><tr><Th>闸门</Th><Th>触发条件</Th><Th>后果</Th><Th>配置项</Th></tr></thead>
          <tbody>
            {GATES.map((g) => (
              <tr key={g.name} className="border-b border-divider last:border-b-0">
                <Td><b>{g.name}</b></Td><Td>{g.rule}</Td><Td>{g.effect}</Td><Td className="text-ink-faint">{g.config}</Td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="mt-2 text-[11px] text-ink-faint">
          拦截发生在信号产生前; 已持仓的止损/减仓永远放行(风控不拦风险出清)。
        </div>
      </Section>

      <Section title="② 六类信号(命中即返回, 保命优先)">
        <table className="w-full border-collapse">
          <thead><tr><Th>信号</Th><Th>含义</Th><Th>优先级</Th><Th>触发形态</Th></tr></thead>
          <tbody>
            {SIGNALS.map((s) => (
              <tr key={s.type} className="border-b border-divider last:border-b-0">
                <Td><b>{s.type}</b></Td><Td>{s.name}</Td><Td className="text-rise">{s.pri}</Td><Td>{s.desc}</Td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section title="③ 交易模式(市况分类器按规则选出)">
        <table className="w-full border-collapse">
          <thead><tr><Th>模式</Th><Th>判定条件</Th><Th>风格</Th></tr></thead>
          <tbody>
            {MODES.map((m) => (
              <tr key={m.key} className="border-b border-divider last:border-b-0">
                <Td><b>{m.name}</b></Td><Td>{m.cond}</Td><Td>{m.style}</Td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section title="④ 金字塔仓位(分批建仓/加仓)">
        <table className="w-full border-collapse">
          <thead><tr><Th>档位</Th><Th>目标仓位</Th><Th>说明</Th></tr></thead>
          <tbody>
            {PYRAMID.map((p) => (
              <tr key={p.stage} className="border-b border-divider last:border-b-0">
                <Td><b>{p.stage}</b></Td><Td>{p.pct}</Td><Td>{p.desc}</Td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="mt-2 text-[11px] text-ink-faint">
          加仓必须顺向(价格 ≥ 成本); 低于成本加仓需强制录入确认(成交原因标注)。各模式比率可不同(见③)。
        </div>
      </Section>

      <Section title="⑤ T+1 规则">
        <div className="text-[12.5px] leading-relaxed text-ink-secondary">
          当日买入的持仓当日不可减仓/卖出, 计划自动顺延至下一交易日; 当日仅可持有或做T买入。
          T+1 只约束卖出类信号, 买入类不受影响。
        </div>
      </Section>

      <Section title="⑥ A股申报数量规则">
        <table className="w-full border-collapse">
          <thead><tr><Th>板块</Th><Th>买入申报</Th><Th>卖出</Th></tr></thead>
          <tbody>
            {LOT_RULES.map((r) => (
              <tr key={r.board} className="border-b border-divider last:border-b-0">
                <Td><b>{r.board}</b></Td><Td>{r.buy}</Td><Td>{r.sell}</Td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section title="⑦ 交易计划字段解读">
        <table className="w-full border-collapse">
          <thead><tr><Th>字段</Th><Th>含义</Th></tr></thead>
          <tbody>
            {PLAN_FIELDS.map((f) => (
              <tr key={f.name} className="border-b border-divider last:border-b-0">
                <Td><b>{f.name}</b></Td><Td>{f.desc}</Td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>
    </div>
  )
}
