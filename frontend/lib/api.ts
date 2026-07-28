import axios, { AxiosInstance } from 'axios';
import { getToken, removeToken } from './auth';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

class ApiClient {
  private client: AxiosInstance;

  constructor() {
    this.client = axios.create({
      baseURL: API_BASE,
      headers: { 'Content-Type': 'application/json' },
    });

    this.client.interceptors.request.use((config) => {
      const token = getToken();
      if (token) {
        config.headers.Authorization = `Bearer ${token}`;
      }
      return config;
    });

    this.client.interceptors.response.use(
      (response) => response,
      (error) => {
        if (error.response?.status === 401 && typeof window !== 'undefined') {
          removeToken();
          window.location.href = '/';
        }
        return Promise.reject(error);
      }
    );
  }

  // Auth
  async register(email: string, password: string) {
    const res = await this.client.post('/auth/register', { email, password });
    return res.data;
  }

  async login(email: string, password: string) {
    const res = await this.client.post('/auth/login', { email, password });
    return res.data;
  }

  async getMe() {
    const res = await this.client.get('/auth/me');
    return res.data;
  }

  // Files
  async listFiles() {
    const res = await this.client.get('/files');
    return res.data;
  }

  async getFile(fileId: string) {
    const res = await this.client.get(`/files/${fileId}`);
    return res.data;
  }

  async getPublicFile(fileId: string) {
    const res = await this.client.get(`/files/public/${fileId}`);
    return res.data;
  }

  async deleteFile(fileId: string) {
    const res = await this.client.delete(`/files/${fileId}`);
    return res.data;
  }

  async uploadFile(file: File, onProgress?: (percent: number) => void) {
    const formData = new FormData();
    formData.append('file', file);

    const res = await this.client.post('/upload', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      onUploadProgress: (progressEvent) => {
        if (onProgress && progressEvent.total) {
          const percent = Math.round(
            (progressEvent.loaded * 100) / progressEvent.total
          );
          onProgress(percent);
        }
      },
    });

    return res.data;
  }

  // Analytics
  async trackEvent(
    fileId: string,
    eventType: string,
    bytesP2p = 0,
    bytesOrigin = 0
  ) {
    const res = await this.client.post('/analytics/event', {
      file_id: fileId,
      event_type: eventType,
      bytes_p2p: bytesP2p,
      bytes_origin: bytesOrigin,
    });
    return res.data;
  }

  async getAnalytics(fileId: string) {
    const res = await this.client.get(`/analytics/${fileId}`);
    return res.data;
  }

  // Billing
  async getBillingStatus() {
    const res = await this.client.get('/billing/status');
    return res.data;
  }

  async createCheckout(plan: string) {
    const res = await this.client.post('/billing/create-checkout', { plan });
    return res.data;
  }
}

export const api = new ApiClient();
export default api;
