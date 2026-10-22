# Security model

## Roles and permissions

Permissions are defined in `backend/app/core/rbac.py`. A role change takes effect on the next request.

| Permission | platform_admin | client_admin | plant_owner | analyst | technician | read_only |
|---|:-:|:-:|:-:|:-:|:-:|:-:|
| org:read, plants:read, alerts:read, workorders:read, reports:read | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| finance:read, carbon:read | ✓ | ✓ | ✓ | ✓ | | ✓ |
| plants:write, carbon:write | ✓ | ✓ | ✓ | | | |
| data:import, data:export, finance:simulate, alerts:run, workorders:write | ✓ | ✓ | ✓ | ✓ | | |
| alerts:review | ✓ | ✓ | ✓ | ✓ | ✓ | |
| workorders:execute | ✓ | ✓ | | | ✓ | |
| users:manage, apikeys:manage, webhooks:manage, audit:read, org:manage | ✓ | ✓ | | | | |
| platform:* (tenants, plans, module overrides) | ✓ | | | | | |

Two rules are enforced in code beyond the matrix:

- Technicians cannot move a work order to `verified`.
- Only platform admins can grant `platform_admin`.

## Controls implemented

| Area | Control |
|---|---|
| Tenant isolation | Organisation-scoped queries plus explicit ownership checks plus PostgreSQL RLS (`FORCE ROW LEVEL SECURITY`) on every tenant table. Tests prove that raw SQL in one tenant's context cannot read or write another tenant's rows (`tests/test_tenant_isolation.py`). |
| Authentication | bcrypt (cost 12) password hashes. HS256 JWT with expiry and a per-user token version: a password change, admin reset, role change, deactivation, 2FA reset or "sign out everywhere" revokes every existing session. Password policy: ≥ 10 characters with letters and digits. Unknown emails take the same time as wrong passwords. |
| Two-factor authentication | Optional per user. TOTP (RFC 6238) works with any authenticator app. Secrets are encrypted at rest with `YALA_DATA_KEY` (Fernet). Admins can reset a user's 2FA. |
| Account lockout | 5 failed attempts (configurable) locks the account for 15 minutes. Admins can unlock. Every lockout is audited. |
| Temporary passwords | Passwords set by an admin (new users, resets, new organisations, bootstrap) must be changed at first sign-in. Until then, the API refuses every other call. |
| Plant-level access | Each user can be limited to specific plants (`plant_scope`). This applies to lists, the dashboard, GIS, alerts, work orders, reports and direct links; out-of-scope plants return 404. |
| API keys | Random 256-bit secret, only the SHA-256 is stored, revocable, `last_used_at` tracked, role-scoped, and limited to plans that include the `api` module. |
| Rate limiting | Per user and per API key (plan limit), plus login attempts per IP. Counters are shared in Redis when `YALA_REDIS_URL` is set (in-process otherwise). The limiter fails open if Redis is down. |
| Input validation | Pydantic schemas with ranges and patterns; CSV range and unit validation; upload size limit (`YALA_MAX_UPLOAD_MB`); UTF-8 only. |
| Audit | Logins (including failures), user, key, plant, contract, event, import, model-run, alert-review, work-order and licence changes are written to `audit_logs` in the same transaction as the change. |
| Transport and headers | TLS is terminated by the host (Render) or the reverse proxy (`deploy/Caddyfile`). HSTS is set in production. A strict Content-Security-Policy applies (`script-src 'self'`, `frame-ancestors 'none'`, image hosts limited to the configured map tiles), along with `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`, and `Cache-Control: no-store` on API responses. |
| Database role | Production start-up refuses a DB role that is a superuser or has `BYPASSRLS`, since RLS would be bypassed. `/ready` reports `tenant_isolation_enforced`. |
| Secrets | Read from the environment only. Production start-up refuses a default or short JWT secret. Webhook secrets are shown once. |
| Telemetry exposure | No unauthenticated data endpoints. `/metrics` exposes only counters and should be restricted at the proxy. |
| Frontend | All dynamic values are HTML-escaped. Libraries are vendored, with no third-party script CDN. The token is kept in `sessionStorage`, not cookies, so no CSRF surface. |

## Encryption at rest

Encryption at rest is provided by the storage layer: an encrypted managed PostgreSQL volume (for
example RDS, Cloud SQL or Azure with KMS) or LUKS on self-hosted disks. Backups are encrypted with
GPG (`deploy/backup/pg_backup.sh`) or by the managed service. At the application level, 2FA secrets
are encrypted with `YALA_DATA_KEY`; other fields rely on storage encryption.

## Outstanding

- **Identity:** SSO/OIDC (Azure AD, Google Workspace) for enterprise customers, and self-service
  password reset by email. Self-service reset needs an email provider to be chosen; today an admin
  resets the password, and the user must change it at next sign-in.
- **Secrets:** a key-rotation runbook for `YALA_JWT_SECRET` (rotating it signs everyone out) and
  `YALA_DATA_KEY` (rotating it requires re-enrolment of 2FA).
- **Assurance:** an external penetration test before onboarding the first paying customer.
  Dependency and container scanning in CI (e.g. `pip-audit`, Trivy).
- **Data protection:** a formal retention and deletion policy and a DPA template (GDPR, Kenya DPA
  2019, POPIA). Tenant data export and deletion are currently operator tasks.
- **Abuse protection:** WAF / DDoS protection is provided by the host or CDN, not by the app.
