// 配置中心元数据: 把后端 DEFAULT_CONFIG 的英文 key 映射为中文标签/说明/输入类型/取值范围。
// 后端分组键(趋势/动量/…)必须与 backend/app/core/config.py 的 DEFAULT_CONFIG 完全一致。
// 新增后端配置项时, 在此补一条 FieldMeta 即可自动出现在设置页。

export type FieldType = 'int' | 'float' | 'bool' | 'select' | 'numlist' | 'text' | 'password'

export interface FieldMeta {
  key: string
  label: string
  type: FieldType
  /** 字段下方的灰色说明文字 */
  hint?: string
  /** select 选项 */
  options?: { value: string; label: string }[]
  min?: number
  max?: number
  step?: number
  /** 标签后缀单位, 如 % / 日 / 元 */
  unit?: string
  /** 显示值 = 存储值 × scale。手续费存 0.00005, 按「万分之」展示为 0.5 */
  scale?: number
  /** numlist 建议长度提示 */
  listHint?: string
}

export interface GroupMeta {
  key: string
  label: string
  desc: string
  fields: FieldMeta[]
  /** 结构复杂需专门渲染的分组 */
  custom?: 'datasource'
}

export const CONFIG_GROUPS: GroupMeta[] = [
  {
    key: '趋势',
    label: '趋势',
    desc: '决定「什么算上升趋势」，直接影响选股趋势分(0-40)与首仓信号是否放行。',
    fields: [
      { key: 'ma_short', label: '短期均线', type: 'int', unit: '日', min: 2, max: 120, hint: '判定短线方向，常用 5 / 10' },
      { key: 'ma_mid', label: '中期均线', type: 'int', unit: '日', min: 3, max: 250, hint: '趋势主轴，常用 20' },
      { key: 'ma_long', label: '长期均线', type: 'int', unit: '日', min: 5, max: 500, hint: '多空分界，常用 60 / 120' },
      {
        key: 'trend_filter', label: '趋势过滤方式', type: 'select',
        options: [
          { value: 'ADX', label: 'ADX（趋势强度，推荐）' },
          { value: 'MA', label: 'MA（纯均线多头排列）' },
        ],
        hint: 'ADX 能过滤掉「均线粘合的假趋势」，震荡市更稳',
      },
      { key: 'adx_threshold', label: 'ADX 阈值', type: 'float', min: 0, max: 100, step: 1, hint: '高于此值才算有效趋势。25 为常用门槛，调低出票多但噪声大' },
    ],
  },
  {
    key: '动量',
    label: '动量',
    desc: '衡量「涨得有多急」，对应选股动量分(0-40)与超买超卖判定。',
    fields: [
      { key: 'roc_period', label: 'ROC 周期', type: 'int', unit: '日', min: 2, max: 120, hint: '价格变动率回看天数，越短越敏感' },
      { key: 'rsi_period', label: 'RSI 周期', type: 'int', unit: '日', min: 2, max: 60, hint: '经典取 14' },
      { key: 'rsi_overbought', label: 'RSI 超买线', type: 'float', min: 50, max: 100, step: 1, hint: '高于此值提示过热，触发减仓倾向' },
      { key: 'rsi_oversold', label: 'RSI 超卖线', type: 'float', min: 0, max: 50, step: 1, hint: '低于此值提示超跌，做T买点参考' },
      { key: 'macd_fast', label: 'MACD 快线', type: 'int', min: 2, max: 100, hint: '必须小于慢线周期' },
      { key: 'macd_slow', label: 'MACD 慢线', type: 'int', min: 3, max: 200 },
      { key: 'macd_signal', label: 'MACD 信号线', type: 'int', min: 2, max: 100, hint: 'DEA 平滑周期，经典 9' },
    ],
  },
  {
    key: '量能',
    label: '量能',
    desc: '判断资金是否真进场，对应选股量能分(0-20)。',
    fields: [
      { key: 'volume_ma', label: '成交量均线', type: 'int', unit: '日', min: 2, max: 120, hint: '量比的基准区间' },
      { key: 'volume_ratio_threshold', label: '放量阈值(量比)', type: 'float', min: 0.1, max: 10, step: 0.1, hint: '当日量 ÷ 均量 高于此值判定为放量。1.5 表示放量五成' },
    ],
  },
  {
    key: '风控',
    label: '风控',
    desc: '三道闸门在「信号产生前」拦截。不通过则不会给出任何买入计划。',
    fields: [
      { key: 'daily_loss_limit_pct', label: '单日亏损熔断', type: 'float', unit: '%', min: 0.5, max: 50, step: 0.5, hint: '当日账户浮亏达此比例，当天停止一切新开仓' },
      { key: 'consecutive_loss_limit', label: '连亏降仓阈值', type: 'int', unit: '笔', min: 1, max: 20, hint: '连续亏损达此笔数后，自动按半仓建议' },
      { key: 'max_drawdown_pct', label: '最大回撤防守线', type: 'float', unit: '%', min: 1, max: 80, step: 1, hint: '账户从高点回撤超过此值，进入防守模式（只减不加）' },
      { key: 'single_position_pct', label: '单票仓位上限', type: 'float', unit: '%', min: 1, max: 100, step: 1, hint: '单只股票占总资金的上限，不应大于总仓位上限' },
      { key: 'total_position_pct', label: '总仓位上限', type: 'float', unit: '%', min: 1, max: 100, step: 1, hint: '所有持仓合计占比上限，留出现金应对极端行情' },
      { key: 'stop_loss_pct', label: '固定止损', type: 'float', unit: '%', min: 0.5, max: 50, step: 0.5, hint: '相对成本价的硬止损线，触发即建议清仓' },
      { key: 'trailing_stop_pct', label: '移动止损回撤', type: 'float', unit: '%', min: 0.5, max: 50, step: 0.5, hint: '盈利后从最高点回撤此比例即止盈离场' },
    ],
  },
  {
    key: '仓位',
    label: '仓位与止盈',
    desc: '决定每次买多少、分几批止盈。数组类字段用英文逗号分隔。',
    fields: [
      {
        key: 'strategy', label: '建仓策略', type: 'select',
        options: [
          { value: 'pyramid', label: '金字塔分批（推荐）' },
          { value: 'kelly', label: '凯利公式（按胜率动态）' },
          { value: 'fixed', label: '固定仓位' },
        ],
      },
      { key: 'pyramid_ratios', label: '金字塔分批比例', type: 'numlist', listHint: '各批占计划仓位的比例，总和须为 1。如 0.5, 0.3, 0.2 表示首仓五成、加仓三成、末仓两成' },
      { key: 'kelly_fraction', label: '凯利系数', type: 'float', min: 0.05, max: 1, step: 0.05, hint: '仅凯利策略生效。0.5 = 半凯利，实盘建议不超过 0.5' },
      {
        key: 'take_profit_mode', label: '止盈模式', type: 'select',
        options: [
          { value: 'atr', label: 'ATR 自适应（跟随波动率，推荐）' },
          { value: 'fixed', label: '固定百分比' },
        ],
        hint: 'ATR 模式下高波动票止盈档自动放宽，避免被正常震荡洗出',
      },
      { key: 'atr_multipliers', label: 'ATR 止盈倍数', type: 'numlist', listHint: 'ATR 模式档位：止盈价 = 成本 × (1 + 倍数 × ATR%)。如 1.5, 3, 5' },
      { key: 'atr_period', label: 'ATR 周期', type: 'int', unit: '日', min: 2, max: 60 },
      { key: 'min_tp_pct', label: '首档止盈下限', type: 'float', unit: '%', min: 0.5, max: 30, step: 0.5, hint: 'ATR 模式的保护：第一档至少要有这么多利润才减仓' },
      { key: 'take_profit_levels', label: '固定止盈档位', type: 'numlist', listHint: '固定模式档位（成本的倍数）。如 1.03, 1.06, 1.10 表示 +3% / +6% / +10%' },
      { key: 'take_profit_ratios', label: '各档减仓比例', type: 'numlist', listHint: '每档卖出持仓的比例，总和不超过 1。如 0.2, 0.3, 0.5 表示早期少减、让利润奔跑' },
    ],
  },
  {
    key: '做T',
    label: '做T',
    desc: '持仓期内的日内高抛低吸，仅对已有底仓的股票生效。',
    fields: [
      { key: 'enable', label: '启用做T信号', type: 'bool', hint: '关闭后不再产生做T买/做T卖信号' },
      { key: 'min_swing_pct', label: '最小波幅', type: 'float', unit: '%', min: 0.1, max: 20, step: 0.1, hint: '日内振幅不足此值不做T，避免手续费吃掉利润' },
      { key: 'support_lookback', label: '支撑位回看', type: 'int', unit: '日', min: 2, max: 60, hint: '计算日内支撑/压力的历史区间' },
      { key: 't_position_ratio', label: '做T仓位比例', type: 'float', min: 0.05, max: 1, step: 0.05, hint: '用底仓的多大比例做T。0.3 = 三成' },
    ],
  },
  {
    key: '评分权重',
    label: '评分权重',
    desc: '每笔交易的五维打分权重，五项之和必须等于 1.00。',
    fields: [
      { key: 'timing', label: '时机', type: 'float', min: 0, max: 1, step: 0.05, hint: '买卖点是否踩在信号上' },
      { key: 'position', label: '仓位', type: 'float', min: 0, max: 1, step: 0.05, hint: '下手轻重是否符合仓位纪律' },
      { key: 'stop', label: '止损', type: 'float', min: 0, max: 1, step: 0.05, hint: '是否按时止损、有没有扛单' },
      { key: 'profit', label: '止盈', type: 'float', min: 0, max: 1, step: 0.05, hint: '是否按计划分批兑现' },
      { key: 'discipline', label: '纪律', type: 'float', min: 0, max: 1, step: 0.05, hint: '有无追高、频繁交易、逆势操作' },
    ],
  },
  {
    key: '趋势阶段',
    label: '趋势阶段',
    desc: '识别趋势生命周期(启动/加速/过热/衰竭)并自动加减分：刚起趋势的票加分浮出，乖离过大/动能衰竭的票扣分压下去。数值参数可被 AI 复盘调参(±20%/次、7天冷却、可回滚)。',
    fields: [
      { key: 'enabled', label: '启用阶段识别', type: 'bool', hint: '关闭后选股不输出阶段加减分(阶段列显示 -)' },
      { key: 'launch_macd_golden', label: '启动加分·MACD金叉', type: 'float', min: 0, max: 10, step: 0.5, hint: '近4根内 MACD 柱由负转正' },
      { key: 'launch_roc_turn', label: '启动加分·ROC转正', type: 'float', min: 0, max: 10, step: 0.5, hint: '近4根内 ROC 由负转正' },
      { key: 'launch_ma_cross', label: '启动加分·短均线刚上穿', type: 'float', min: 0, max: 10, step: 0.5, hint: '近4根内短均线上穿中均线' },
      { key: 'launch_adx_first', label: '启动加分·ADX首次达标', type: 'float', min: 0, max: 10, step: 0.5, hint: 'ADX 刚上穿阈值且持续走高' },
      { key: 'launch_bonus_max', label: '启动加分封顶', type: 'float', min: 0, max: 15, step: 0.5, hint: '多个启动事件叠加时的加分上限，防止分数虚高' },
      { key: 'overheat_bias', label: '过热乖离阈值', type: 'float', unit: '%', min: 3, max: 30, step: 0.5, hint: '偏离短期均线超过此值判为过热期' },
      { key: 'overheat_bias_penalty', label: '过热乖离扣分', type: 'float', min: 0, max: 10, step: 0.5 },
      { key: 'overheat_rsi_penalty', label: '过热RSI扣分', type: 'float', min: 0, max: 10, step: 0.5, hint: 'RSI 超过过热线时扣分（动量分本身已衰减，此为补充）' },
      { key: 'overheat_volume', label: '过热量比阈值', type: 'float', min: 1, max: 10, step: 0.1, hint: '量比超过此值判为过热期' },
      { key: 'overheat_volume_penalty', label: '过热量比扣分', type: 'float', min: 0, max: 10, step: 0.5 },
      { key: 'exhaust_penalty', label: '衰竭扣分', type: 'float', min: 0, max: 15, step: 0.5, hint: 'RSI 超买且 MACD 红柱缩短时扣分' },
      { key: 'rsi_overheat', label: '阶段判定RSI过热线', type: 'float', min: 60, max: 90, step: 1, hint: 'RSI ≥ 此值判为过热期' },
      { key: 'rsi_exhaust', label: '阶段判定RSI衰竭线', type: 'float', min: 65, max: 95, step: 1, hint: 'RSI ≥ 此值且红柱缩短判为衰竭期，须大于过热线' },
    ],
  },
  {
    key: '数据源',
    label: '数据源',
    desc: '行情来源的优先级与降级策略。上层业务只调用管理器，切换数据源对策略无影响。',
    custom: 'datasource',
    fields: [],
  },
  {
    key: 'llm',
    label: 'AI 复盘',
    desc: '兼容 OpenAI 协议的大模型，用于复盘深度归因。不启用则只做规则诊断。',
    fields: [
      { key: 'enabled', label: '启用 LLM 深度复盘', type: 'bool' },
      { key: 'base_url', label: 'API 地址', type: 'text', hint: '如 https://api.deepseek.com/v1 · 本地 Ollama 填 http://localhost:11434/v1' },
      { key: 'api_key', label: 'API Key', type: 'password', hint: '留空则不修改已保存的 Key。仅存本机数据库，接口回传时脱敏' },
      { key: 'model', label: '模型', type: 'text', hint: '如 deepseek-chat / qwen-plus / gpt-4o-mini' },
      { key: 'temperature', label: '温度', type: 'float', min: 0, max: 2, step: 0.1, hint: '越低越稳定保守，复盘建议 0.2 ~ 0.5' },
      { key: 'max_tokens', label: '最大输出长度', type: 'int', min: 256, max: 32768, step: 256 },
      { key: 'timeout_sec', label: '超时', type: 'int', unit: '秒', min: 5, max: 600 },
    ],
  },
  {
    key: '手续费',
    label: '手续费',
    desc: '影响每笔成交的费用与净盈亏，进而影响统计、评分与复盘口径。费率按「万分之」填写。',
    fields: [
      { key: 'commission_rate', label: '佣金费率', type: 'float', unit: '万分之', scale: 10000, min: 0, max: 100, step: 0.01, hint: '单边收取。券商万 0.5 即填 0.5' },
      { key: 'commission_min', label: '单笔最低佣金', type: 'float', unit: '元', min: 0, max: 100, step: 0.5, hint: '佣金不足此金额时按此收取，常见为 5 元' },
      { key: 'stamp_tax_rate', label: '印花税', type: 'float', unit: '万分之', scale: 10000, min: 0, max: 100, step: 0.1, hint: '仅卖出收取。现行万 5 即填 5' },
      { key: 'exchange_fee_rate', label: '经手费', type: 'float', unit: '万分之', scale: 10000, min: 0, max: 100, step: 0.001, hint: '买卖双边收取，现行万 0.341' },
      { key: 'regulatory_fee_rate', label: '证管费', type: 'float', unit: '万分之', scale: 10000, min: 0, max: 100, step: 0.01, hint: '买卖双边收取，现行万 0.2' },
      { key: 'transfer_fee_rate', label: '过户费', type: 'float', unit: '万分之', scale: 10000, min: 0, max: 100, step: 0.01, hint: '买卖双边收取，现行万 0.1' },
    ],
  },
]

export const DATA_SOURCE_LABELS: Record<string, string> = {
  mootdx: 'mootdx（通达信本地协议，最快）',
  tencent: '腾讯行情（稳定，实时快照）',
  eastmoney: '东方财富（数据全，有连接级风控）',
  akshare: 'AKShare（兜底，较慢）',
  lixinger: '理杏仁（申万2021行业分级，分类刷新专用）',
}
