import type { HealthData } from '../api/client'

export default function Dashboard({ health }: { health: HealthData | null }) {
  return (
    <div>
      <h1 style={{ fontSize: 20 }}>仪表盘</h1>
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginTop: 16 }}>
        <div style={{ border: '1px solid #e5e6eb', borderRadius: 8, padding: '12px 20px', minWidth: 160 }}>
          <div style={{ color: '#888', fontSize: 12 }}>系统状态</div>
          <div style={{ fontSize: 22, fontWeight: 700, color: health?.status === 'up' ? '#22a55b' : '#c00' }}>
            {health ? (health.status === 'up' ? '运行中' : health.status) : '未知'}
          </div>
        </div>
        <div style={{ border: '1px solid #e5e6eb', borderRadius: 8, padding: '12px 20px', minWidth: 160 }}>
          <div style={{ color: '#888', fontSize: 12 }}>当前日期(Asia/Shanghai)</div>
          <div style={{ fontSize: 18, fontWeight: 700 }}>{health?.date ?? '-'}</div>
        </div>
        <div style={{ border: '1px solid #e5e6eb', borderRadius: 8, padding: '12px 20px', minWidth: 160 }}>
          <div style={{ color: '#888', fontSize: 12 }}>数据源</div>
          <div style={{ fontSize: 18, fontWeight: 700 }}>
            {health ? health.data_sources.filter((s) => !s.circuit_open).length + '/' + health.data_sources.length : '-'}
            <span style={{ fontSize: 12, color: '#888', marginLeft: 6 }}>个可用</span>
          </div>
        </div>
        <div style={{ border: '1px solid #e5e6eb', borderRadius: 8, padding: '12px 20px', minWidth: 160 }}>
          <div style={{ color: '#888', fontSize: 12 }}>今日信号</div>
          <div style={{ fontSize: 22, fontWeight: 700 }}>-</div>
        </div>
      </div>
      <div style={{ marginTop: 24, color: '#888', fontSize: 13 }}>
        一期(地基)骨架:数据源自动切换 + 指标库 + 行情/K线 API 已就绪。信号/持仓/复盘等功能按二期~四期交付。
      </div>
    </div>
  )
}
