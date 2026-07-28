'use client';

import { Suspense } from 'react';
import { Loader2 } from 'lucide-react';
import VideoPlayer from '@/components/VideoPlayer';

export default function EmbedPage({
  params,
}: {
  params: { file_id: string };
}) {
  return (
    <div className="mx-auto max-w-4xl px-4 py-8 sm:px-6 lg:px-8">
      <Suspense
        fallback={
          <div className="flex aspect-video items-center justify-center border border-[#1f1f1f] bg-[#111]">
            <Loader2 size={24} className="animate-spin text-gray-500" />
          </div>
        }
      >
        <VideoPlayer fileId={params.file_id} />
      </Suspense>

      {params.file_id !== 'demo' && (
        <div className="mt-4 text-center">
          <p className="text-xs text-gray-600">
            Powered by{' '}
            <a
              href="/"
              className="text-accent-500 hover:underline"
            >
              TorrentCDN
            </a>{' '}
            &mdash; P2P Video Delivery
          </p>
        </div>
      )}
    </div>
  );
}
