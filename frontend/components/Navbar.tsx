'use client'

import { useState, useEffect, useRef, useCallback } from 'react'
import { useRouter } from 'next/navigation'
import { Bell, Menu, X } from 'lucide-react'
import { isLoggedIn, removeToken } from '@/lib/auth'
import { aiFeatureEnabled } from '@/lib/aiConsole'
import api from '@/lib/api'
import AuthModal from './AuthModal'
import ThemeToggle from './ThemeToggle'

interface Notification {
  id: string
  type: string
  message: string
  read: boolean
  created_at: string
}

export default function Navbar() {
  const router = useRouter()
  const [showAuth, setShowAuth] = useState(false)
  const [user, setUser] = useState<{ email: string; plan: string; is_admin?: boolean } | null>(null)
  const [loggedIn, setLoggedIn] = useState(false)
  const [aiEnabled, setAiEnabled] = useState(false)
  const [notifications, setNotifications] = useState<Notification[]>([])
  const [showNotifs, setShowNotifs] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const notifRef = useRef<HTMLDivElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const burgerRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    setLoggedIn(isLoggedIn())
  }, [])

  // The AI link appears only when this deployment actually serves the AI
  // surface: the probe is a single status call (404 == flag off), memoised for
  // a minute in lib/aiConsole. Signed-out visitors never probe — the console is
  // account-scoped, so there is nothing for them to reach.
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
        // ONLY a 401 (expired/revoked session) ends the session. A 429 from
        // rate limiting, or any transient network/5xx failure, must NOT log
        // the user out — the page refresh burst of concurrent requests used to
        // trip the rate limiter, which made getMe fail, which nuked the token
        // and bounced the user to the landing page.
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

  // Mobile menu: scroll lock while open, Escape closes, Tab is trapped inside
  // the panel, and focus returns to the trigger on close.
  useEffect(() => {
    if (!menuOpen) return
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        setMenuOpen(false)
        burgerRef.current?.focus()
        return
      }
      if (event.key !== 'Tab' || !menuRef.current) return
      const focusables = menuRef.current.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )
      if (focusables.length === 0) return
      const first = focusables[0]
      const last = focusables[focusables.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', onKeyDown)
    // Move focus into the panel for keyboard users.
    const timer = window.setTimeout(() => {
      menuRef.current?.querySelector<HTMLElement>('a[href], button')?.focus()
    }, 20)

    return () => {
      document.body.style.overflow = previousOverflow
      document.removeEventListener('keydown', onKeyDown)
      window.clearTimeout(timer)
    }
  }, [menuOpen])

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

  const closeMenu = useCallback(() => setMenuOpen(false), [])

  const openAuth = () => {
    setMenuOpen(false)
    setShowAuth(true)
  }

  const primaryLinks = [
    { href: '/docs', label: 'Docs' },
    { href: '/architecture', label: 'Architecture' },
    { href: '/demo', label: 'Demo' },
    { href: '/tcdn-ai', label: 'TCDN-AI', desktopOnly: true },
  ]

  const accountLinks = loggedIn
    ? [
        { href: '/dashboard', label: 'Dashboard' },
        { href: '/live', label: 'Live' },
        { href: '/billing', label: 'Billing' },
        ...(aiEnabled ? [{ href: '/ai', label: 'AI' }] : []),
        ...(user?.is_admin ? [{ href: '/admin', label: 'Admin' }] : []),
      ]
    : []

  return (
    <>
      <header className="nav-shell">
        <nav className="nav-inner" aria-label="Main">
          <a href="/" className="nav-logo" aria-label="TorrentCDN home">
            <span className="nav-logo-mark" aria-hidden="true" />
            TorrentCDN
          </a>

          <div className="nav-links">
            {primaryLinks.map((link) => (
              <a
                key={link.href}
                href={link.href}
                className={`nav-link${link.desktopOnly ? ' nav-link-desktop' : ''}`}
              >
                {link.label}
              </a>
            ))}
            {accountLinks.map((link) => (
              <a key={link.href} href={link.href} className="nav-link">
                {link.label}
              </a>
            ))}
          </div>

          <div className="nav-actions">
            <ThemeToggle />
            {loggedIn ? (
              <>
                <div className="relative" ref={notifRef}>
                  <button
                    onClick={() => setShowNotifs((s) => !s)}
                    className="nav-icon-btn"
                    aria-label="Notifications"
                    aria-expanded={showNotifs}
                  >
                    <Bell size={16} aria-hidden="true" />
                    {unreadCount > 0 && <span className="nav-dot" aria-hidden="true" />}
                  </button>
                  {showNotifs && (
                    <div className="nav-notifs">
                      <div className="nav-notifs-head">
                        <p className="label">Notifications</p>
                      </div>
                      {notifications.length === 0 ? (
                        <p className="nav-notifs-empty">No notifications</p>
                      ) : (
                        notifications.map((n) => (
                          <div key={n.id} className="nav-notif">
                            <p className={n.read ? 'nav-notif-read' : 'nav-notif-unread'}>
                              {n.message}
                            </p>
                            <div className="nav-notif-actions">
                              {!n.read && (
                                <button
                                  onClick={() => handleMarkRead(n.id)}
                                  className="nav-notif-action"
                                >
                                  Mark read
                                </button>
                              )}
                              <a href="/billing" className="nav-notif-action nav-notif-upgrade">
                                Upgrade →
                              </a>
                            </div>
                          </div>
                        ))
                      )}
                    </div>
                  )}
                </div>
                {user?.email && (
                  <span className="nav-email" title={user.email}>{user.email}</span>
                )}
                <button onClick={handleLogout} className="nav-cta nav-cta-ghost">
                  Logout
                </button>
              </>
            ) : (
              <>
                <button onClick={() => setShowAuth(true)} className="nav-link nav-login">
                  Login
                </button>
                <button onClick={() => setShowAuth(true)} className="nav-cta">
                  Sign up
                </button>
              </>
            )}
            <button
              ref={burgerRef}
              type="button"
              className="nav-burger"
              aria-label={menuOpen ? 'Close menu' : 'Open menu'}
              aria-expanded={menuOpen}
              aria-controls="nav-mobile-panel"
              onClick={() => setMenuOpen((open) => !open)}
            >
              {menuOpen ? <X size={18} aria-hidden="true" /> : <Menu size={18} aria-hidden="true" />}
            </button>
          </div>
        </nav>
      </header>

      {menuOpen && (
        <div className="nav-scrim" onClick={closeMenu} aria-hidden="true" />
      )}
      <div
        id="nav-mobile-panel"
        ref={menuRef}
        className={`nav-mobile${menuOpen ? ' is-open' : ''}`}
        hidden={!menuOpen}
        aria-label="Mobile navigation"
        role="dialog"
        aria-modal="true"
      >
        <div className="nav-mobile-head">
          <span className="nav-logo">
            <span className="nav-logo-mark" aria-hidden="true" />
            TorrentCDN
          </span>
          <button type="button" className="nav-icon-btn" onClick={closeMenu} aria-label="Close menu">
            <X size={18} aria-hidden="true" />
          </button>
        </div>
        <div className="nav-mobile-links">
          {primaryLinks.map((link) => (
            <a key={link.href} href={link.href} className="nav-mobile-link" onClick={closeMenu}>
              {link.label}
            </a>
          ))}
          {accountLinks.map((link) => (
            <a key={link.href} href={link.href} className="nav-mobile-link" onClick={closeMenu}>
              {link.label}
            </a>
          ))}
        </div>
        <div className="nav-mobile-foot">
          {loggedIn ? (
            <>
              {user?.email && (
                <p className="nav-mobile-email" title={user.email}>
                  {user.email}
                </p>
              )}
              <button onClick={handleLogout} className="btn btn-secondary w-full">
                Logout
              </button>
            </>
          ) : (
            <>
              <button onClick={() => { closeMenu(); setShowAuth(true) }} className="btn btn-secondary w-full">
                Login
              </button>
              <button onClick={openAuth} className="btn btn-primary w-full">
                Sign up
              </button>
            </>
          )}
        </div>
      </div>

      {showAuth && (
        <AuthModal onClose={() => { setShowAuth(false); api.getMe().then(setUser).catch(() => {}) }} />
      )}
    </>
  )
}
