'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { ArrowLeft } from 'lucide-react';
import { isLoggedIn } from '@/lib/auth';
import UploadForm from '@/components/UploadForm';

export default function UploadPage() {
  const router = useRouter();
  useEffect(() => { if (!isLoggedIn()) router.push('/'); }, [router]);

  return (
    <div className="mx-auto max-w-3xl px-6 py-10 lg:px-8">
      <div className="mb-8">
        <a href="/dashboard" className="mb-4 inline-flex items-center gap-1.5 text-sm text-gray-500 transition-colors hover:text-white">
          <ArrowLeft size={16} /> Back to Dashboard
        </a>
        <h1 className="text-2xl font-bold text-white">Upload File</h1>
        <p className="mt-1 text-sm text-gray-500">Drag and drop or click to select a video or audio file</p>
      </div>
      <UploadForm />
    </div>
  );
}
