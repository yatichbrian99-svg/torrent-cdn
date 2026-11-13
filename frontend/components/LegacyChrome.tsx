'use client'

/**
 * The PRE-REDESIGN navbar, rendered verbatim on the frozen surfaces
 * (`/embed`, `/share`).
 *
 * Those pages are customer-facing player surfaces; their appearance must not
 * change with the redesign. layout.tsx therefore swaps this component in for
 * the new Navbar on those routes and pins the old token palette through
 * `[data-frozen-surface]` in globals.css.
 *
 * This is a deliberate copy of the old Navbar, including its inline styles and
 * its own small toggle (the redesign's toggle has a different look). Keep it
 * literally in sync with the frozen markup — a Playwright pixel diff
 * (scripts/diff_screenshots.mjs) fails if anything here changes appearance.
 */

import { useState, useEffect, useRef } from 'react'
import { useRouter } from 'next/navigation'
import { Bell, Moon, Sun } from 'lucide-react'
import { isLoggedIn, removeToken } from '@/lib/auth'
import { aiFeatureEnabled } from '@/lib/aiConsole'
import api from '@/lib/api'
import AuthModal from './AuthModal'
import { applyTheme, persistTheme, readStoredTheme, resolveTheme, toggleTheme, type Theme } from '@/lib/theme'

interface Notification {
  id: string
  type: string
  message: string
  read: boolean
  created_at: string
}

/** Old toggle, pixel-identical to the pre-redesign one. */
function LegacyThemeToggle() {
  const [theme, setTheme] = useState<Theme>('dark')

  useEffect(() => {
    const prefersDark =
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-color-scheme: dark)').matches
    setTheme(resolveTheme(window.localStorage, prefersDark))
  }, [])

  const switchTheme = () => {
    const next = toggleTheme(theme)
    setTheme(next)
    applyTheme(next)
    persistTheme(next, window.localStorage)
  }

  return (
    <button
      onClick={switchTheme}
      className="p-1 transition-opacity hover:opacity-80"
      style={{ color: 'var(--text-muted)' }}
      aria-label={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
      title={theme === 'dark' ? 'Light mode' : 'Dark mode'}
    >
      {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
    </button>
  )
}

export default function LegacyChrome() {
  const router = useRouter()
  const [showAuth, setShowAuth] = useState(false)
  const [user, setUser] = useState<{ email: string; plan: string; is_admin?: boolean } | null>(null)
  const [loggedIn, setLoggedIn] = useState(false)
  const [aiEnabled, setAiEnabled] = useState(false)
  const [notifications, setNotifications] = useState<Notification[]>([])
  const [showNotifs, setShowNotifs] = useState(false)
  const notifRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    setLoggedIn(isLoggedIn())
  }, [])

  useEffect(() => {
    if (!loggedIn) {
      setAiEnabled(false)
      return
    }
    let cancelled = false
    aiFeatureEnabled().then((enabled) => {
      if (!cancelled) setAiEnabled(enabled)
    })
    return () => {
      cancelled = true
    }
  }, [loggedIn])

  useEffect(() => {
    if (loggedIn && !user) {
      api.getMe().then(setUser).catch((err: any) => {
        if (err?.response?.status === 401) removeToken()
      })
    }
  }, [loggedIn, user])

  useEffect(() => {
    if (loggedIn) {
      api.getNotifications().then(setNotifications).catch(() => {})
    }
  }, [loggedIn])

  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (notifRef.current && !notifRef.current.contains(e.target as Node)) {
        setShowNotifs(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  const unreadCount = notifications.filter((n) => !n.read).length

  const handleLogout = () => {
    removeToken()
    setUser(null)
    router.push('/')
    window.location.reload()
  }

  const handleMarkRead = async (id: string) => {
    try {
      await api.markNotificationRead(id)
      setNotifications((prev) => prev.map((n) => (n.id === id ? { ...n, read: true } : n)))
    } catch {
      // ignore — dropdown refresh will reconcile
    }
  }

  return (
    <>
      <nav className="fixed top-0 z-50 flex h-12 w-full items-center border-b" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--bg-primary)' }}>
        <div className="mx-auto flex w-full max-w-6xl items-center justify-between px-4">
          <a href="/" className="font-mono text-sm font-medium text-[color:var(--text-primary)]">TorrentCDN</a>

          <div className="flex items-center gap-4">
            <LegacyThemeToggle />
            <a href="/docs" className="text-sm transition-colors hover:text-[color:var(--text-primary)]" style={{ color: 'var(--text-muted)' }}>Docs</a>
            <a href="/architecture" className="text-sm transition-colors hover:text-[color:var(--text-primary)]" style={{ color: 'var(--text-muted)' }}>Architecture</a>
            <a href="/demo" className="text-sm transition-colors hover:text-[color:var(--text-primary)]" style={{ color: 'var(--text-muted)' }}>Demo</a>
            <a href="/tcdn-ai" className="hidden text-sm transition-colors hover:text-[color:var(--text-primary)] sm:inline" style={{ color: 'var(--text-muted)' }}>TCDN-AI</a>
            {loggedIn && (
              <>
                <a href="/dashboard" className="text-sm" style={{ color: 'var(--text-muted)' }}>Dashboard</a>
                <a href="/live" className="text-sm" style={{ color: 'var(--text-muted)' }}>Live</a>
                <a href="/billing" className="text-sm" style={{ color: 'var(--text-muted)' }}>Billing</a>
                {aiEnabled && (
                  <a href="/ai" className="text-sm" style={{ color: 'var(--text-muted)' }}>AI</a>
                )}
                {user?.is_admin && (
                  <a href="/admin" className="text-sm" style={{ color: 'var(--text-muted)' }}>Admin</a>
                )}
                <div className="relative" ref={notifRef}>
                  <button onClick={() => setShowNotifs((s) => !s)} className="relative p-1 transition-opacity hover:opacity-80" aria-label="Notifications">
                    <Bell size={16} style={{ color: 'var(--text-muted)' }} />
                    {unreadCount > 0 && (
                      <span className="absolute -right-0.5 -top-0.5 h-2 w-2 rounded-full" style={{ backgroundColor: 'var(--err)' }} />
                    )}
                  </button>
                  {showNotifs && (
                    <div className="absolute right-0 top-8 z-50 max-h-96 w-80 overflow-y-auto border" style={{ backgroundColor: 'var(--bg-secondary)', borderColor: 'var(--border)' }}>
                      <div className="border-b px-4 py-2" style={{ borderColor: 'var(--border)' }}>
                        <p className="text-xs font-bold uppercase tracking-widest" style={{ color: 'var(--text-muted)', fontFamily: 'monospace' }}>Notifications</p>
                      </div>
                      {notifications.length === 0 ? (
                        <p className="px-4 py-6 text-center text-xs" style={{ color: 'var(--text-muted)' }}>No notifications</p>
                      ) : (
                        notifications.map((n) => (
                          <div key={n.id} className="border-b px-4 py-3" style={{ borderColor: 'var(--border)' }}>
                            <p className={`text-xs leading-relaxed ${n.read ? '' : 'font-medium text-[color:var(--text-primary)]'}`} style={n.read ? { color: 'var(--text-dim)' } : {}}>
                              {n.message}
                            </p>
                            <div className="mt-2 flex items-center gap-3">
                              {!n.read && (
                                <button onClick={() => handleMarkRead(n.id)} className="text-[10px] uppercase tracking-widest transition-colors hover:text-[color:var(--text-primary)]" style={{ color: 'var(--text-soft)', fontFamily: 'monospace' }}>
                                  Mark read
                                </button>
                              )}
                              <a href="/billing" className="text-[10px] uppercase tracking-widest transition-colors hover:text-[color:var(--text-primary)]" style={{ color: 'var(--ok)', fontFamily: 'monospace' }}>
                                Upgrade →
                              </a>
                            </div>
                          </div>
                        ))
                      )}
                    </div>
                  )}
                </div>
                <span className="text-sm" style={{ color: 'var(--text-muted)' }}>{user?.email}</span>
                <button onClick={handleLogout} className="border px-3 py-1 text-sm" style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}>Logout</button>
              </>
            )}
            {!loggedIn && (
              <>
                <button onClick={() => setShowAuth(true)} className="text-sm" style={{ color: 'var(--text-muted)' }}>Login</button>
                <button onClick={() => setShowAuth(true)} className="border px-3 py-1 text-sm font-medium" style={{ backgroundColor: 'var(--inverse-bg)', borderColor: 'var(--inverse-bg)', color: 'var(--bg-primary)' }}>Sign up</button>
              </>
            )}
          </div>
        </div>
      </nav>

      {showAuth && (
        <AuthModal onClose={() => { setShowAuth(false); api.getMe().then(setUser).catch(() => {}) }} />
      )}
    </>
  )
}
