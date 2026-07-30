'use client'

import { useState } from 'react'
import { Loader2 } from 'lucide-react'
import { saveToken } from '@/lib/auth'
import api from '@/lib/api'

export default function AuthModal({ onClose }: { onClose: () => void }) {
  const [isLogin, setIsLogin] = useState(true)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    setError('')
    try {
      const data = isLogin ? await api.login(email, password) : await api.register(email, password)
      saveToken(data.access_token)
      onClose()
      window.location.reload()
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Authentication failed')
    } finally { setLoading(false) }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" style={{ backgroundColor: 'rgba(0,0,0,0.8)' }}>
      <div className="w-full max-w-sm border p-8" style={{ backgroundColor: '#111', borderColor: '#333' }}>
        <div className="mb-6 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-white">{isLogin ? 'Login' : 'Create account'}</h2>
          <button onClick={onClose} className="text-sm" style={{ color: '#555' }}>&times;</button>
        </div>

        <form onSubmit={handleSubmit} className="flex flex-col gap-5">
          <div>
            <label className="mb-1 block text-xs uppercase tracking-widest" style={{ color: '#555' }}>Email</label>
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required
              className="w-full border px-3 py-2 text-sm text-white" style={{ backgroundColor: '#0c0c0c', borderColor: '#333' }}
              placeholder="you@example.com" />
          </div>
          <div>
            <label className="mb-1 block text-xs uppercase tracking-widest" style={{ color: '#555' }}>Password</label>
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required minLength={8}
              className="w-full border px-3 py-2 text-sm text-white" style={{ backgroundColor: '#0c0c0c', borderColor: '#333' }}
              placeholder="password" />
            {isLogin && (
              <a
                href="/auth/forgot-password"
                className="mt-1 inline-block text-[11px] transition-colors hover:text-white"
                style={{ color: '#555' }}
              >
                Forgot password?
              </a>
            )}
          </div>

          {error && <p className="text-xs text-white">{error}</p>}

          <button type="submit" disabled={loading}
            className="w-full border py-2 text-sm font-medium disabled:opacity-50"
            style={{ backgroundColor: '#fff', borderColor: '#fff', color: '#000' }}>
            {loading ? <Loader2 size={14} className="mx-auto animate-spin" /> : isLogin ? 'Login' : 'Create account'}
          </button>

          <p className="text-center text-xs" style={{ color: '#555' }}>
            {isLogin ? "Don't have an account?" : 'Already have an account?'}{' '}
            <button type="button" onClick={() => { setIsLogin(!isLogin); setError('') }} className="font-medium text-white hover:underline">
              {isLogin ? 'Sign up' : 'Login'}
            </button>
          </p>
        </form>
      </div>
    </div>
  )
}
