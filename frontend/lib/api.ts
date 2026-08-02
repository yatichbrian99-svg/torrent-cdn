import axios from 'axios'
import { getToken, removeToken } from './auth'

// Production ALWAYS routes through the same-origin Vercel proxy: vercel.json
// rewrites /api/*, /auth/*, /upload, /files/*, /analytics/*, /billing/*,
// /admin/*, /notifications/* to the Render backend, so relative URLs are
// reliable AND avoid the page CSP + CORS entirely.
//
// A cross-origin NEXT_PUBLIC_API_URL (e.g. someone setting it to a direct
// https://torrent-cdn.onrender.com URL) breaks login in production: the browser
// either blocks the fetch via connect-src or the direct cross-origin POST fails
// at the network layer ('Could not reach the server'). So in production we
// IGNORE the env var and stay same-origin. The env override is only honored in
// local dev, where vercel.json rewrites don't exist.
const isProduction = process.env.NODE_ENV === 'production'
const API_BASE = isProduction ? '' : (process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000')

const api = axios.create({
  baseURL: API_BASE,
  // 45s: Render's free tier cold-starts after ~15min of inactivity (can take
  // 20-45s). The old 15s timeout made the FIRST request after idle fail with a
  // generic 'Something went wrong' even though the backend eventually responds.
  timeout: 45000,
})

api.interceptors.request.use((config) => {
  const token = getToken()
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

api.interceptors.response.use(
  (response) => response,
  (error) => {
    // A 401 from the login/register endpoints means WRONG CREDENTIALS, not an
    // expired session — let the form show the real error ('Invalid email or
    // password') instead of nuking the page with a redirect. Everywhere else
    // (auth/me, files, billing, ...) a 401 still means the session died, so
    // clear the token and bounce home.
    const url = error.config?.url || ''
    const isCredentialCheck = url === '/auth/login' || url === '/auth/register'
    if (error.response?.status === 401 && !isCredentialCheck) {
      removeToken()
      if (typeof window !== 'undefined') {
        window.location.href = '/'
      }
    }
    return Promise.reject(error)
  }
)

export default {
  register: (email: string, password: string, termsAccepted: boolean = false, refCode?: string) =>
    api.post('/auth/register', { email, password, terms_accepted: termsAccepted, ref_code: refCode || undefined }).then((r) => r.data),

  login: async (email: string, password: string) => {
    // Render's free tier cold-starts after ~15min idle — the first request after
    // idle can hit a transient 502/503/504 at the Vercel proxy while the
    // instance wakes. Retry ONCE after a short pause so users don't have to
    // click Login twice. Login is safe to retry (idempotent: only mints a
    // token). Non-5xx errors (wrong password 401, lockout 429) pass through.
    try {
      return (await api.post('/auth/login', { email, password })).data
    } catch (err: any) {
      const status = err.response?.status
      // Retry once on a cold start: either a transient 502/503/504 from the
      // proxy OR no response at all (the 45s timeout fires while Render boots).
      // Login is idempotent (only mints a token) so retrying is safe — it can't
      // hit the 401/429 lockout paths, which pass through untouched.
      const isTransient = status === 502 || status === 503 || status === 504 || !err.response
      if (!isTransient) throw err
      await new Promise((resolve) => setTimeout(resolve, 2000))
      return (await api.post('/auth/login', { email, password })).data
    }
  },

  getMe: () => api.get('/auth/me').then((r) => r.data),

  uploadFile: (file: File, onProgress?: (pct: number) => void) => {
    const form = new FormData()
    form.append('file', file)
    return api
      .post('/upload', form, {
        headers: { 'Content-Type': 'multipart/form-data' },
        onUploadProgress: (e) => {
          if (e.total && onProgress) onProgress(Math.round((e.loaded / e.total) * 100))
        },
      })
      .then((r) => r.data)
  },

  listFiles: () => api.get('/files').then((r) => r.data),

  getFile: (file_id: string) => api.get(`/files/${file_id}`).then((r) => r.data),

  deleteFile: (file_id: string) => api.delete(`/files/${file_id}`).then((r) => r.data),

  getPublicFile: (file_id: string) => api.get(`/files/public/${file_id}`).then((r) => r.data),

  trackEvent: (file_id: string, event_type: string, bytes_p2p?: number, bytes_origin?: number) =>
    api.post('/analytics/event', { file_id, event_type, bytes_p2p: bytes_p2p || 0, bytes_origin: bytes_origin || 0 }).then((r) => r.data),

  getAnalytics: (file_id: string) => api.get(`/analytics/${file_id}`).then((r) => r.data),

  getBillingStatus: () => api.get('/billing/status').then((r) => r.data),

  verifyEmail: (token: string) =>
    api.post('/auth/verify-email', { token }).then((r) => r.data),

  resendVerification: () =>
    api.post('/auth/resend-verification').then((r) => r.data),

  getVerificationStatus: () =>
    api.get('/auth/verification-status').then((r) => r.data),

  forgotPassword: (email: string) =>
    api.post('/auth/forgot-password', { email }).then((r) => r.data),

  resetPassword: (token: string, password: string) =>
    api.post('/auth/reset-password', { token, password }).then((r) => r.data),

  deleteAccount: () => api.delete('/auth/me').then((r) => r.data),

  createCheckout: async (plan: string) => {
    try {
      const res = await api.post('/billing/create-checkout', { plan })
      return res.data
    } catch {
      throw new Error('Checkout failed. Please try again.')
    }
  },

  getNotifications: () => api.get('/notifications').then((r) => r.data),

  markNotificationRead: (id: string) => api.post(`/notifications/mark-read/${id}`).then((r) => r.data),

  getReferral: () => api.get('/auth/referral').then((r) => r.data),

  getBandwidthHistory: () => api.get('/analytics/bandwidth-history').then((r) => r.data),

  getAdminStats: () => api.get('/admin/stats').then((r) => r.data),

  getAdminUsers: (page: number) => api.get(`/admin/users?page=${page}&limit=20`).then((r) => r.data),

  updateUserPlan: (userId: string, plan: string) =>
    api.post(`/admin/users/${userId}/plan`, { plan }).then((r) => r.data),

  toggleAdmin: (userId: string) => api.post(`/admin/users/${userId}/toggle-admin`).then((r) => r.data),

  verifyUser: (userId: string) => api.post(`/admin/users/${userId}/verify`).then((r) => r.data),

  deleteUser: (userId: string) => api.delete(`/admin/users/${userId}`).then((r) => r.data),
}

