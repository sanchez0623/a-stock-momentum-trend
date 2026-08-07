// 占位组件: 未实现模块的通用提示(供后续阶段页面复用)
export function Placeholder({ title }: { title: string }) {
  return (
    <div className="px-12 py-12 text-center text-ink-secondary">
      <h2 className="mb-2 text-lg">{title}</h2>
      <p className="text-[13px]">该模块将在后续阶段实现(二期/三期/四期)。</p>
    </div>
  )
}
