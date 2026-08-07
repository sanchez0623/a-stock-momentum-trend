import type { CSSProperties, ReactNode } from 'react'
import { Card } from './Card'

// 图表容器: 统一图表卡片外壳(标题 + 图表内容), 未来图表统一放此
export function ChartContainer({ title, children, style }: {
  title: string
  children: ReactNode
  style?: CSSProperties
}) {
  return <Card title={title} style={style}>{children}</Card>
}
