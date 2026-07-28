export default function UsageBar({ used, limit }: { used: number; limit: number }) {
  const percentage = limit === Infinity ? 0 : Math.min((used / limit) * 100, 100);
  const isUnlimited = limit === Infinity;

  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <span className="text-sm text-gray-400">Bandwidth Usage</span>
        <span className="text-sm text-gray-500">
          {used.toFixed(2)} GB{isUnlimited ? '' : ` / ${limit} GB`}
        </span>
      </div>
      <div className="h-2.5 rounded-full bg-surface-border">
        <div
          className={`h-full rounded-full transition-all duration-500 ${
            isUnlimited
              ? 'bg-gradient-to-r from-brand-500 to-brand-400 opacity-60'
              : percentage > 80
              ? 'bg-red-500'
              : percentage > 50
              ? 'bg-yellow-500'
              : 'bg-brand-500'
          }`}
          style={{ width: isUnlimited ? '100%' : `${Math.min(percentage, 100)}%` }}
        />
      </div>
    </div>
  );
}
