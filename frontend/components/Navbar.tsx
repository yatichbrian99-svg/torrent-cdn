'use client';

import { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import {
  LogOut,
  LayoutDashboard,
  CreditCard,
  Menu,
  X,
  Loader2,
} from 'lucide-react';
import { isLoggedIn, getToken, saveToken, removeToken } from '@/lib/auth';
import api from '@/lib/api';

interface User {
  id: string;
  email: string;
  plan: string;
}

export default function Navbar() {
  const router = useRouter();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [showAuthModal, setShowAuthModal] = useState(false);
  const [isLogin, setIsLogin] = useState(true);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [user, setUser] = useState<User | null>(null);
  const loggedIn = isLoggedIn();

  useEffect(() => {
    if (loggedIn && !user) {
      api
        .getMe()
        .then(setUser)
        .catch(() => removeToken());
    }
  }, [loggedIn, user]);

  const handleAuth = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError('');
    try {
      const data = isLogin
        ? await api.login(email, password)
        : await api.register(email, password);
      saveToken(data.access_token);
      setShowAuthModal(false);
      setEmail('');
      setPassword('');
      setUser(null);
      router.refresh();
      window.location.reload();
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Authentication failed');
    } finally {
      setLoading(false);
    }
  };

  const handleLogout = () => {
    removeToken();
    setUser(null);
    router.push('/');
    window.location.reload();
  };

  return (
    <>
      <nav className="fixed top-0 z-50 w-full border-b border-[#1f1f1f] bg-[#0a0a0a]/90 backdrop-blur-xl">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-4 py-3 sm:px-6 lg:px-8">
          {/* Logo */}
          <a
            href="/"
            className="flex items-center gap-2.5 group"
          >
            <div className="flex h-8 w-8 items-center justify-center bg-accent-500/10 border border-accent-500/30">
              <svg
                className="h-4 w-4 text-accent-500"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={2}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M13 10V3L4 14h7v7l9-11h-7z"
                />
              </svg>
            </div>
            <span className="text-base font-bold tracking-tight text-white">
              Torrent<span className="text-accent-500">CDN</span>
            </span>
          </a>

          {/* Desktop Nav */}
          <div className="hidden items-center gap-6 md:flex">
            {loggedIn && (
              <>
                <a
                  href="/dashboard"
                  className="flex items-center gap-1.5 text-sm text-gray-400 transition-colors hover:text-white"
                >
                  <LayoutDashboard size={15} />
                  Dashboard
                </a>
                <a
                  href="/billing"
                  className="flex items-center gap-1.5 text-sm text-gray-400 transition-colors hover:text-white"
                >
                  <CreditCard size={15} />
                  Billing
                </a>
              </>
            )}

            {loggedIn && user ? (
              <div className="flex items-center gap-3">
                <span className="text-xs text-gray-500">{user.email}</span>
                <span className="rounded border border-[#1f1f1f] px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-gray-500">
                  {user.plan}
                </span>
                <button
                  onClick={handleLogout}
                  className="flex items-center gap-1.5 rounded border border-[#1f1f1f] px-3 py-1.5 text-xs text-gray-400 transition-colors hover:border-gray-700 hover:text-white"
                >
                  <LogOut size={13} />
                  Logout
                </button>
              </div>
            ) : (
              <button
                onClick={() => {
                  setIsLogin(true);
                  setShowAuthModal(true);
                }}
                className="rounded bg-accent-500 px-4 py-1.5 text-xs font-medium text-black transition-all hover:bg-accent-400"
              >
                Sign In
              </button>
            )}
          </div>

          {/* Mobile hamburger */}
          <button
            onClick={() => setMobileOpen(!mobileOpen)}
            className="text-gray-400 md:hidden"
          >
            {mobileOpen ? <X size={20} /> : <Menu size={20} />}
          </button>
        </div>

        {/* Mobile nav */}
        {mobileOpen && (
          <div className="border-t border-[#1f1f1f] bg-[#0a0a0a] px-4 py-4 md:hidden">
            <div className="flex flex-col gap-3">
              {loggedIn && (
                <>
                  <a
                    href="/dashboard"
                    className="text-sm text-gray-400"
                    onClick={() => setMobileOpen(false)}
                  >
                    Dashboard
                  </a>
                  <a
                    href="/billing"
                    className="text-sm text-gray-400"
                    onClick={() => setMobileOpen(false)}
                  >
                    Billing
                  </a>
                </>
              )}
              {loggedIn ? (
                <button
                  onClick={() => {
                    handleLogout();
                    setMobileOpen(false);
                  }}
                  className="flex items-center gap-1.5 text-sm text-gray-400"
                >
                  <LogOut size={14} /> Logout
                </button>
              ) : (
                <button
                  onClick={() => {
                    setIsLogin(true);
                    setShowAuthModal(true);
                    setMobileOpen(false);
                  }}
                  className="rounded bg-accent-500 px-4 py-1.5 text-xs font-medium text-black"
                >
                  Sign In
                </button>
              )}
            </div>
          </div>
        )}
      </nav>

      {/* Auth Modal */}
      {showAuthModal && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <div className="w-full max-w-sm border border-[#1f1f1f] bg-[#111] p-6">
            <div className="mb-6 flex items-center justify-between">
              <h2 className="text-sm font-semibold text-white">
                {isLogin ? 'Sign In' : 'Create Account'}
              </h2>
              <button
                onClick={() => setShowAuthModal(false)}
                className="text-gray-500 hover:text-white"
              >
                <X size={16} />
              </button>
            </div>

            <form onSubmit={handleAuth} className="flex flex-col gap-4">
              <div>
                <label className="mb-1 block text-[11px] font-medium uppercase tracking-wider text-gray-500">
                  Email
                </label>
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  required
                  className="w-full border border-[#1f1f1f] bg-[#0a0a0a] px-3 py-2 text-sm text-white placeholder-gray-600 focus:border-accent-500 focus:outline-none"
                  placeholder="you@example.com"
                />
              </div>
              <div>
                <label className="mb-1 block text-[11px] font-medium uppercase tracking-wider text-gray-500">
                  Password
                </label>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                  minLength={6}
                  className="w-full border border-[#1f1f1f] bg-[#0a0a0a] px-3 py-2 text-sm text-white placeholder-gray-600 focus:border-accent-500 focus:outline-none"
                  placeholder="••••••••"
                />
              </div>

              {error && (
                <p className="text-xs text-red-500">{error}</p>
              )}

              <button
                type="submit"
                disabled={loading}
                className="flex items-center justify-center gap-2 border border-accent-500 bg-accent-500/10 px-4 py-2 text-sm font-medium text-accent-500 transition-colors hover:bg-accent-500/20 disabled:opacity-50"
              >
                {loading && <Loader2 size={14} className="animate-spin" />}
                {isLogin ? 'Sign In' : 'Create Account'}
              </button>

              <p className="text-center text-xs text-gray-600">
                {isLogin ? "Don't have an account?" : 'Already have an account?'}{' '}
                <button
                  type="button"
                  onClick={() => {
                    setIsLogin(!isLogin);
                    setError('');
                  }}
                  className="text-accent-500 hover:underline"
                >
                  {isLogin ? 'Register' : 'Sign In'}
                </button>
              </p>
            </form>
          </div>
        </div>
      )}
    </>
  );
}
