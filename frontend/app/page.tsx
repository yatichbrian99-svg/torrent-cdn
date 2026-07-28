'use client';

import { useState, useEffect } from 'react';
import { Zap, Shield, Download, BarChart3, Globe, Users, Check } from 'lucide-react';

export default function Home() {
  const [mounted, setMounted] = useState(false);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [isLogin, setIsLogin] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  if (!mounted) return null;

  const handleAuth = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      const { default: api } = await import('@/lib/api');
      if (isLogin) {
        await api.login(email, password);
      } else {
        await api.register(email, password);
      }
      window.location.href = '/dashboard';
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Authentication failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="relative overflow-hidden">
      {/* Hero Section */}
      <section className="relative px-4 pb-20 pt-20 sm:px-6 lg:px-8">
        <div className="absolute inset-0 bg-gradient-to-b from-primary-500/10 via-transparent to-transparent" />
        <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_top,_var(--tw-gradient-stops))] from-primary-900/20 via-transparent to-transparent" />

        <div className="relative mx-auto max-w-7xl">
          <div className="grid gap-12 lg:grid-cols-2 lg:gap-16">
            {/* Left Column - Hero Content */}
            <div className="flex flex-col justify-center">
              <div className="inline-flex items-center rounded-full border border-primary-500/20 bg-primary-500/10 px-3 py-1 text-sm text-primary-400">
                <Zap className="mr-1 h-3 w-3" />
                Now in Public Beta
              </div>
              <h1 className="mt-6 text-4xl font-bold tracking-tight text-white sm:text-5xl lg:text-6xl">
                P2P CDN —
                <span className="block bg-gradient-to-r from-primary-400 to-purple-400 bg-clip-text text-transparent">
                  Deliver content cheaper with WebTorrent
                </span>
              </h1>
              <p className="mt-6 text-lg leading-8 text-slate-400">
                Slash your bandwidth costs by up to 90%. Torrent CDN uses WebTorrent to
                distribute your video and audio content peer-to-peer, automatically
                offloading traffic from your origin servers.
              </p>

              <div className="mt-8 grid grid-cols-2 gap-4">
                {[
                  { icon: Zap, text: 'Up to 90% bandwidth savings' },
                  { icon: Shield, text: 'Encrypted P2P connections' },
                  { icon: Download, text: 'Auto-fallback to origin' },
                  { icon: BarChart3, text: 'Real-time analytics' },
                ].map((feature) => (
                  <div key={feature.text} className="flex items-center gap-2 text-sm text-slate-300">
                    <feature.icon className="h-4 w-4 text-primary-400" />
                    {feature.text}
                  </div>
                ))}
              </div>
            </div>

            {/* Right Column - Auth Form */}
            <div className="flex items-center justify-center">
              <div className="w-full max-w-md rounded-2xl border border-slate-700/50 bg-slate-800/50 p-8 backdrop-blur-xl">
                <h2 className="text-2xl font-bold text-white">
                  {isLogin ? 'Welcome back' : 'Get started free'}
                </h2>
                <p className="mt-2 text-sm text-slate-400">
                  {isLogin ? 'Sign in to your account' : 'No credit card required. 100GB/month free.'}
                </p>

                <form onSubmit={handleAuth} className="mt-8 space-y-4">
                  {error && (
                    <div className="rounded-lg bg-red-500/10 p-3 text-sm text-red-400">
                      {error}
                    </div>
                  )}

                  <div>
                    <label htmlFor="email" className="block text-sm font-medium text-slate-300">
                      Email
                    </label>
                    <input
                      id="email"
                      type="email"
                      required
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      className="mt-1 block w-full rounded-lg border border-slate-600 bg-slate-700/50 px-4 py-2.5 text-white placeholder-slate-400 transition-colors focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500/20"
                      placeholder="you@example.com"
                    />
                  </div>

                  <div>
                    <label htmlFor="password" className="block text-sm font-medium text-slate-300">
                      Password
                    </label>
                    <input
                      id="password"
                      type="password"
                      required
                      minLength={8}
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      className="mt-1 block w-full rounded-lg border border-slate-600 bg-slate-700/50 px-4 py-2.5 text-white placeholder-slate-400 transition-colors focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500/20"
                      placeholder="Min. 8 characters"
                    />
                  </div>

                  <button
                    type="submit"
                    disabled={loading}
                    className="w-full rounded-lg bg-gradient-to-r from-primary-600 to-purple-600 px-4 py-2.5 text-sm font-semibold text-white shadow-lg transition-all hover:from-primary-500 hover:to-purple-500 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {loading ? (
                      <span className="flex items-center justify-center gap-2">
                        <span className="spinner h-4 w-4" />
                        {isLogin ? 'Signing in...' : 'Creating account...'}
                      </span>
                    ) : (
                      isLogin ? 'Sign in' : 'Create free account'
                    )}
                  </button>
                </form>

                <div className="mt-6 text-center">
                  <button
                    onClick={() => { setIsLogin(!isLogin); setError(''); }}
                    className="text-sm text-primary-400 transition-colors hover:text-primary-300"
                  >
                    {isLogin ? "Don't have an account? Sign up" : 'Already have an account? Sign in'}
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* Stats Section */}
      <section className="relative border-y border-slate-800/50 py-16">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
          <div className="grid grid-cols-2 gap-8 md:grid-cols-4">
            {[
              { value: '10M+', label: 'Files served', icon: Globe },
              { value: '90%', label: 'Max P2P offload', icon: Zap },
              { value: '50K+', label: 'Active users', icon: Users },
              { value: '99.9%', label: 'Uptime SLA', icon: Shield },
            ].map((stat) => (
              <div key={stat.label} className="text-center">
                <stat.icon className="mx-auto h-6 w-6 text-primary-400" />
                <div className="mt-2 text-3xl font-bold text-white">{stat.value}</div>
                <div className="mt-1 text-sm text-slate-400">{stat.label}</div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Pricing Section */}
      <section className="px-4 py-20 sm:px-6 lg:px-8" id="pricing">
        <div className="mx-auto max-w-7xl">
          <div className="text-center">
            <h2 className="text-3xl font-bold text-white sm:text-4xl">
              Simple, transparent pricing
            </h2>
            <p className="mt-4 text-lg text-slate-400">
              Start free, scale as you grow. No hidden fees.
            </p>
          </div>

          <div className="mt-12 grid gap-8 md:grid-cols-3">
            {/* Free Plan */}
            <div className="relative rounded-2xl border border-slate-700/50 bg-slate-800/30 p-8 backdrop-blur-xl transition-all hover:border-slate-600/50">
              <h3 className="text-xl font-semibold text-white">Free</h3>
              <div className="mt-4">
                <span className="text-4xl font-bold text-white">$0</span>
                <span className="text-slate-400">/month</span>
              </div>
              <p className="mt-2 text-sm text-slate-400">Perfect for getting started</p>
              <ul className="mt-6 space-y-3">
                {[
                  '100GB bandwidth / month',
                  'Basic analytics',
                  'Community support',
                  '1 concurrent upload',
                ].map((feature) => (
                  <li key={feature} className="flex items-center gap-2 text-sm text-slate-300">
                    <Check className="h-4 w-4 text-green-400" />
                    {feature}
                  </li>
                ))}
              </ul>
              <button
                onClick={() => document.getElementById('auth-form')?.scrollIntoView({ behavior: 'smooth' })}
                className="mt-8 w-full rounded-lg border border-slate-600 px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-slate-700"
              >
                Get started
              </button>
            </div>

            {/* Pro Plan */}
            <div className="relative rounded-2xl border-2 border-primary-500/50 bg-slate-800/50 p-8 shadow-xl shadow-primary-500/10 backdrop-blur-xl transition-all hover:border-primary-500">
              <div className="absolute -top-3 left-1/2 -translate-x-1/2">
                <span className="rounded-full bg-gradient-to-r from-primary-600 to-purple-600 px-4 py-1 text-xs font-semibold text-white">
                  Most popular
                </span>
              </div>
              <h3 className="text-xl font-semibold text-white">Pro</h3>
              <div className="mt-4">
                <span className="text-4xl font-bold text-white">$29</span>
                <span className="text-slate-400">/month</span>
              </div>
              <p className="mt-2 text-sm text-slate-400">For growing projects</p>
              <ul className="mt-6 space-y-3">
                {[
                  '1TB bandwidth / month',
                  'Advanced analytics',
                  'Priority support',
                  'Unlimited concurrent uploads',
                  'Custom embed domain',
                ].map((feature) => (
                  <li key={feature} className="flex items-center gap-2 text-sm text-slate-300">
                    <Check className="h-4 w-4 text-green-400" />
                    {feature}
                  </li>
                ))}
              </ul>
              <button
                onClick={() => document.getElementById('auth-form')?.scrollIntoView({ behavior: 'smooth' })}
                className="mt-8 w-full rounded-lg bg-gradient-to-r from-primary-600 to-purple-600 px-4 py-2.5 text-sm font-medium text-white shadow-lg transition-all hover:from-primary-500 hover:to-purple-500"
              >
                Start free trial
              </button>
            </div>

            {/* Business Plan */}
            <div className="relative rounded-2xl border border-slate-700/50 bg-slate-800/30 p-8 backdrop-blur-xl transition-all hover:border-slate-600/50">
              <h3 className="text-xl font-semibold text-white">Business</h3>
              <div className="mt-4">
                <span className="text-4xl font-bold text-white">$99</span>
                <span className="text-slate-400">/month</span>
              </div>
              <p className="mt-2 text-sm text-slate-400">For high-volume platforms</p>
              <ul className="mt-6 space-y-3">
                {[
                  'Unlimited bandwidth',
                  'Real-time analytics & insights',
                  '24/7 dedicated support',
                  'Custom embed domain',
                  'SSO & team accounts',
                  'SLA guarantee',
                ].map((feature) => (
                  <li key={feature} className="flex items-center gap-2 text-sm text-slate-300">
                    <Check className="h-4 w-4 text-green-400" />
                    {feature}
                  </li>
                ))}
              </ul>
              <button
                onClick={() => document.getElementById('auth-form')?.scrollIntoView({ behavior: 'smooth' })}
                className="mt-8 w-full rounded-lg border border-slate-600 px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-slate-700"
              >
                Contact sales
              </button>
            </div>
          </div>
        </div>
      </section>

      {/* How it Works */}
      <section className="border-t border-slate-800/50 px-4 py-20 sm:px-6 lg:px-8">
        <div className="mx-auto max-w-7xl">
          <h2 className="text-center text-3xl font-bold text-white sm:text-4xl">
            How it works
          </h2>
          <p className="mx-auto mt-4 max-w-2xl text-center text-lg text-slate-400">
            Upload once, deliver everywhere. Our P2P network does the heavy lifting.
          </p>

          <div className="mt-12 grid gap-8 md:grid-cols-3">
            {[
              {
                step: '01',
                title: 'Upload your content',
                description: 'Upload video or audio files through our dashboard or API. We store them securely in our distributed storage.',
              },
              {
                step: '02',
                title: 'Get your embed code',
                description: 'We generate a magnet link and embed code. Drop our SDK snippet into any website to start serving content.',
              },
              {
                step: '03',
                title: 'Deliver via P2P',
                description: 'Viewers stream from each other in real-time. Our origin servers only kick in when no peers are available.',
              },
            ].map((step) => (
              <div key={step.step} className="relative rounded-2xl border border-slate-700/50 bg-slate-800/30 p-8 backdrop-blur-xl transition-all hover:border-slate-600/50">
                <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-primary-500/10 text-lg font-bold text-primary-400">
                  {step.step}
                </div>
                <h3 className="mt-4 text-lg font-semibold text-white">{step.title}</h3>
                <p className="mt-2 text-sm text-slate-400">{step.description}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* CTA Section */}
      <section className="border-t border-slate-800/50 px-4 py-20 sm:px-6 lg:px-8">
        <div className="mx-auto max-w-4xl text-center">
          <h2 className="text-3xl font-bold text-white sm:text-4xl">
            Ready to slash your bandwidth costs?
          </h2>
          <p className="mt-4 text-lg text-slate-400">
            Join thousands of creators and businesses already using Torrent CDN to deliver content efficiently.
          </p>
          <button
            onClick={() => document.getElementById('auth-form')?.scrollIntoView({ behavior: 'smooth' })}
            className="mt-8 inline-flex items-center gap-2 rounded-lg bg-gradient-to-r from-primary-600 to-purple-600 px-8 py-3 text-base font-semibold text-white shadow-lg transition-all hover:from-primary-500 hover:to-purple-500"
          >
            <Zap className="h-5 w-5" />
            Get started for free
          </button>
        </div>
      </section>

      {/* Footer */}
      <footer className="border-t border-slate-800/50 px-4 py-8 sm:px-6 lg:px-8">
        <div className="mx-auto max-w-7xl">
          <div className="flex flex-col items-center justify-between gap-4 sm:flex-row">
            <div className="flex items-center gap-2">
              <div className="flex h-6 w-6 items-center justify-center rounded-md bg-gradient-to-br from-primary-500 to-purple-600">
                <svg className="h-3 w-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
                </svg>
              </div>
              <span className="text-sm font-medium text-slate-400">Torrent CDN</span>
            </div>
            <p className="text-sm text-slate-500">&copy; 2024 Torrent CDN. All rights reserved.</p>
          </div>
        </div>
      </footer>
    </div>
  );
}
