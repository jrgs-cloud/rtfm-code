# Sample Project

A sample project used for integration testing of the rtfm plugin.

## Structure

- `src/app.py` — Main application entry point
- `src/models.py` — Domain models (User, AdminUser, Session)
- `src/utils.py` — Utility functions (hashing, validation, tokens)
- `src/services/auth_service.py` — Authentication service
- `src/client.ts` — TypeScript API client
- `src/helpers.ts` — TypeScript helper utilities
- `src/types.ts` — TypeScript type definitions
- `src/middleware.ts` — Request middleware

## Getting Started

```bash
pip install -e .
python -m src.app
```
