export default function UsageBar({ used, limit }: { used: number; limit: number }) {
  const isUnlimited = limit === Infinity
  const pct = isUnlimited ? 0 : Math.min((used / limit) * 100, 100)

  return (
    <div>
      <div className="mb-2 flex items-center justify-between text-sm">
        <span style={{ color: '#666' }}>Bandwidth usage</span>
        <span style={{ color: '#666' }}>
          {used.toFixed(2)} GB{isUnlimited ? '' : ` / ${limit} GB`}
        </span>
      </div>
      <div className="h-1 w-full" style={{ backgroundColor: '#222' }}>
        <div
          className="h-full transition-all duration-500"
          style={{
            width: isUnlimited ? '100%' : `${pct}%`,
            backgroundColor: pct > 80 ? '#ff2d6b' : pct > 50 ? '#f5a623' : 'var(--accent)',
          }}
        />
      </div>
    </div>
  )
}
