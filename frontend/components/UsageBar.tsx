export default function UsageBar({ used, limit }: { used: number; limit: number }) {
  const percentage = limit === Infinity ? 0 : Math.min((used / limit) * 100, 100);
  const isUnlimited = limit === Infinity;

  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between">
        <span className="text-xs text-gray-400">Bandwidth Usage</span>
        <span className="text-xs text-gray-500">
          {used.toFixed(2)} GB{isUnlimited ? '' : ` / ${limit} GB`}
        </span>
      </div>
      <div className="h-2 bg-[#1f1f1f]">
        <div
          className={`h-full transition-all duration-500 ${
            isUnlimited ? 'bg-gray-600' : percentage > 80 ? 'bg-red-500' : percentage > 50 ? 'bg-yellow-500' : 'bg-gray-500'
          }`}
          style={{ width: isUnlimited ? '100%' : `${Math.min(percentage, 100)}%` }}
        />
      </div>
    </div>
  );
}
