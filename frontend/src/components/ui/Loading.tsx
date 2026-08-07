export function Loading({ text = '加载中...' }: { text?: string }) {
  return (
    <div className="py-12 text-center text-ink-faint">
      <div className="mx-auto mb-2.5 h-6 w-6 animate-spin rounded-full border-2 border-divider border-t-link" />
      <div className="text-[13px]">{text}</div>
    </div>
  )
}
