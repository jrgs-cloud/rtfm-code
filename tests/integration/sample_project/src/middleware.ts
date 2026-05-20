// Middleware for request processing

import { ApiConfig, Session } from './types';
import { isTokenExpired } from './helpers';
import { ApiClient } from './client';

export interface Middleware {
  name: string;
  process(request: Request, next: () => Promise<Response>): Promise<Response>;
}

export class AuthMiddleware implements Middleware {
  name = 'auth';
  private session: Session | null = null;

  constructor(private client: ApiClient) {}

  async process(request: Request, next: () => Promise<Response>): Promise<Response> {
    if (this.session && isTokenExpired(this.session.expiresAt)) {
      this.session = null;
    }
    return next();
  }

  setSession(session: Session): void {
    this.session = session;
  }
}

export class LoggingMiddleware implements Middleware {
  name = 'logging';
  private logs: string[] = [];

  async process(request: Request, next: () => Promise<Response>): Promise<Response> {
    const start = Date.now();
    const response = await next();
    const duration = Date.now() - start;
    this.logs.push(`${request.method} ${request.url} - ${response.status} (${duration}ms)`);
    return response;
  }

  getLogs(): string[] {
    return [...this.logs];
  }

  clearLogs(): void {
    this.logs = [];
  }
}

export function createMiddlewareChain(middlewares: Middleware[]): Middleware[] {
  return middlewares.sort((a, b) => a.name.localeCompare(b.name));
}
