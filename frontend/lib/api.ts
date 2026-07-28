import axios, { AxiosInstance } from 'axios';

const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

class ApiClient {
  private client: AxiosInstance;
  private token: string | null = null;

  constructor() {
    this.client = axios.create({
      baseURL: API_BASE,
      headers: {
        'Content-Type': 'application/json',
      },
    });

    // Request interceptor to add auth header
    this.client.interceptors.request.use((config) => {
      if (this.token) {
        config.headers.Authorization = `Bearer ${this.token}`;
      }
      return config;
    });

    // Response interceptor for error handling
    this.client.interceptors.response.use(
      (response) => response,
      (error) => {
        if (error.response?.status === 401 && typeof window !== 'undefined') {
          localStorage.removeItem('token');
          window.location.href = '/';
        }
        return Promise.reject(error);
      }
    );
  }

  setToken(token: string | null) {
    this.token = token;
    if (token) {
      localStorage.setItem('token', token);
    } else {
      localStorage.removeItem('token');
    }
  }

  getToken(): string | null {
    if (!this.token) {
      this.token = localStorage.getItem('token');
    }
    return this.token;
  }

  isAuthenticated(): boolean {
    return !!this.getToken();
  }

  // Auth
  async register(email: string, password: string) {
    const res = await this.client.post('/auth/register', { email, password });
    this.setToken(res.data.access_token);
    return res.data;
  }

  async login(email: string, password: string) {
    const res = await this.client.post('/auth/login', { email, password });
    this.setToken(res.data.access_token);
    return res.data;
  }

  async getMe() {
    return this.client.get('/auth/me');
  }

  // Files
  async listFiles() {
    return this.client.get('/files');
  }

  async getFile(fileId: string) {
    return this.client.get(`/files/${fileId}`);
  }

  async getPublicFile(fileId: string) {
    return this.client.get(`/files/public/${fileId}`);
  }

  async deleteFile(fileId: string) {
    return this.client.delete(`/files/${fileId}`);
  }

  async uploadFile(file: File, onProgress?: (percent: number) => void) {
    const formData = new FormData();
    formData.append('file', file);

    const res = await this.client.post('/upload', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      onUploadProgress: (progressEvent) => {
        if (onProgress && progressEvent.total) {
          const percent = Math.round((progressEvent.loaded * 100) / progressEvent.total);
          onProgress(percent);
        }
      },
    });

    return res;
  }

  // Analytics
  async trackEvent(fileId: string, eventType: string, bytesP2p = 0, bytesOrigin = 0) {
    return this.client.post('/analytics/event', {
      file_id: fileId,
      event_type: eventType,
      bytes_p2p: bytesP2p,
      bytes_origin: bytesOrigin,
    });
  }

  async getAnalytics(fileId: string) {
    return this.client.get(`/analytics/${fileId}`);
  }

  // Billing
  async getBillingStatus() {
    return this.client.get('/billing/status');
  }

  async createCheckout(plan: string) {
    return this.client.post('/billing/create-checkout', { plan });
  }
}

export const api = new ApiClient();
export default api;
