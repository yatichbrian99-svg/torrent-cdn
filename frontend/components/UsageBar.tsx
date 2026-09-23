export default function UsageBar({ used, limit }: { used: number; limit: number }) {
  const isUnlimited = limit === Infinity
  const pct = isUnlimited ? 0 : Math.min((used / limit) * 100, 100)

  return (
    <div>
      <div className="mb-2 flex items-center justify-between text-sm">
        <span style={{ color: 'var(--text-muted)' }}>Bandwidth usage</span>
        <span style={{ color: 'var(--text-muted)' }}>{used.toFixed(2)} GB{isUnlimited ? '' : ` / ${limit} GB`}</span>
      </div>
      <div className="h-1 w-full" style={{ backgroundColor: 'var(--border)' }}>
        <div className="h-full transition-all duration-500" style={{ width: isUnlimited ? '100%' : `${pct}%`, backgroundColor: 'var(--inverse-bg)' }} />
      </div>
    </div>
  )
}
