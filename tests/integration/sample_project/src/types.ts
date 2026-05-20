// Type definitions for the application

export interface ApiConfig {
  baseUrl: string;
  timeout?: number;
  retries?: number;
}

export interface UserResponse {
  id: string;
  name: string;
  email: string;
  role: UserRole;
  createdAt: string;
}

export enum UserRole {
  Admin = 'admin',
  User = 'user',
  Guest = 'guest',
}

export interface Session {
  token: string;
  userId: string;
  expiresAt: number;
}

export interface PaginatedResponse<T> {
  data: T[];
  total: number;
  page: number;
  pageSize: number;
}

export type ErrorCode = 'AUTH_FAILED' | 'NOT_FOUND' | 'RATE_LIMITED' | 'SERVER_ERROR';

export interface ApiError {
  code: ErrorCode;
  message: string;
  details?: Record<string, unknown>;
}
