'use client'

import { useState, useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { isLoggedIn, removeToken } from '@/lib/auth'
import api from '@/lib/api'
import AuthModal from './AuthModal'

export default function Navbar() {
  const router = useRouter()
  const [showAuth, setShowAuth] = useState(false)
  const [user, setUser] = useState<{ email: string; plan: string } | null>(null)
  const loggedIn = isLoggedIn()

  useEffect(() => {
    if (loggedIn && !user) api.getMe().then(setUser).catch(() => removeToken())
  }, [loggedIn, user])

  const handleLogout = () => {
    removeToken()
    setUser(null)
    router.push('/')
    window.location.reload()
  }

  return (
    <>
      <nav className="fixed top-0 z-50 flex h-12 w-full items-center border-b" style={{ borderColor: '#222', backgroundColor: '#000' }}>
        <div className="mx-auto flex w-full max-w-6xl items-center justify-between px-4">
          <a href="/" className="font-mono text-sm font-medium text-white">
            TorrentCDN
          </a>

          <div className="flex items-center gap-4">
            {loggedIn && (
              <>
                <a href="/dashboard" className="text-sm" style={{ color: '#666' }}>
                  Dashboard
                </a>
                <a href="/billing" className="text-sm" style={{ color: '#666' }}>
                  Billing
                </a>
                <span className="text-sm" style={{ color: '#666' }}>
                  {user?.email}
                </span>
                <button
                  onClick={handleLogout}
                  className="border px-3 py-1 text-sm"
                  style={{ borderColor: '#222', color: '#666' }}
                >
                  Logout
                </button>
              </>
            )}
            {!loggedIn && (
              <>
                <button
                  onClick={() => setShowAuth(true)}
                  className="text-sm"
                  style={{ color: '#666' }}
                >
                  Login
                </button>
                <button
                  onClick={() => setShowAuth(true)}
                  className="border px-3 py-1 text-sm"
                  style={{ borderColor: 'var(--accent)', color: '#fff', backgroundColor: 'var(--accent)' }}
                >
                  Sign up
                </button>
              </>
            )}
          </div>
        </div>
      </nav>

      {showAuth && (
        <AuthModal
          onClose={() => {
            setShowAuth(false)
            api.getMe().then(setUser).catch(() => {})
          }}
        />
      )}
    </>
  )
}
