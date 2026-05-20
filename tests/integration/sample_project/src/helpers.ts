// Helper utilities for the TypeScript client

import { ApiError, ErrorCode } from './types';

export function formatError(message: string, statusCode: number): string {
  return `[${statusCode}] ${message}`;
}

export async function retryWithBackoff<T>(
  fn: () => Promise<T>,
  maxRetries: number = 3,
  baseDelay: number = 1000
): Promise<T> {
  let lastError: Error | null = null;

  for (let attempt = 0; attempt < maxRetries; attempt++) {
    try {
      return await fn();
    } catch (error) {
      lastError = error as Error;
      if (attempt < maxRetries - 1) {
        const delay = baseDelay * Math.pow(2, attempt);
        await sleep(delay);
      }
    }
  }

  throw lastError;
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function parseApiError(response: unknown): ApiError {
  if (typeof response === 'object' && response !== null) {
    const obj = response as Record<string, unknown>;
    return {
      code: (obj.code as ErrorCode) || 'SERVER_ERROR',
      message: (obj.message as string) || 'Unknown error',
      details: obj.details as Record<string, unknown>,
    };
  }
  return { code: 'SERVER_ERROR', message: 'Unknown error' };
}

export function isTokenExpired(expiresAt: number): boolean {
  return Date.now() > expiresAt * 1000;
}

export function buildQueryString(params: Record<string, string | number | boolean>): string {
  const entries = Object.entries(params)
    .filter(([_, v]) => v !== undefined && v !== null)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  return entries.length > 0 ? `?${entries.join('&')}` : '';
}
