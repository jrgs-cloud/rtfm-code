// TypeScript API client for the application

import { UserResponse, ApiConfig } from './types';
import { formatError, retryWithBackoff } from './helpers';

export class ApiClient {
  private baseUrl: string;
  private token: string | null = null;

  constructor(config: ApiConfig) {
    this.baseUrl = config.baseUrl;
  }

  async login(email: string, password: string): Promise<string> {
    const response = await retryWithBackoff(() =>
      fetch(`${this.baseUrl}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
      })
    );

    if (!response.ok) {
      throw new Error(formatError('Login failed', response.status));
    }

    const data = await response.json();
    this.token = data.token;
    return data.token;
  }

  async getUser(userId: string): Promise<UserResponse> {
    const response = await fetch(`${this.baseUrl}/users/${userId}`, {
      headers: this.getHeaders(),
    });

    if (!response.ok) {
      throw new Error(formatError('User fetch failed', response.status));
    }

    return response.json();
  }

  async listUsers(page: number = 1): Promise<UserResponse[]> {
    const response = await fetch(`${this.baseUrl}/users?page=${page}`, {
      headers: this.getHeaders(),
    });
    return response.json();
  }

  private getHeaders(): Record<string, string> {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
    };
    if (this.token) {
      headers['Authorization'] = `Bearer ${this.token}`;
    }
    return headers;
  }
}
