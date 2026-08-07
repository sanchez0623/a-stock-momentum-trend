import { AlertCircle } from 'lucide-react'

export function ErrorBox({ message }: { message: string }) {
  return (
    <div className="mb-3 flex items-start gap-2 rounded-lg border border-rise/30 bg-rise/5 px-3 py-2.5 text-[13px] text-rise">
      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
      <span>{message}</span>
    </div>
  )
}
