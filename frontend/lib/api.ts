import axios from 'axios'
import { getToken, removeToken } from './auth'

const api = axios.create({
  baseURL: process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000',
  timeout: 15000,
})

api.interceptors.request.use((config) => {
  const token = getToken()
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      removeToken()
      if (typeof window !== 'undefined') {
        window.location.href = '/'
      }
    }
    return Promise.reject(error)
  }
)

export default {
  register: (email: string, password: string) =>
    api.post('/auth/register', { email, password }).then((r) => r.data),

  login: (email: string, password: string) =>
    api.post('/auth/login', { email, password }).then((r) => r.data),

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

  createCheckout: async (plan: string) => {
    try {
      const res = await api.post('/billing/create-checkout', { plan })
      return res.data
    } catch {
      throw new Error('Checkout failed. Please try again.')
    }
  },
}
