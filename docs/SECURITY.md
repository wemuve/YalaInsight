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
| Authentication | bcrypt (cost 12) password hashes. HS256 JWT with expiry, checked against an active user on every request. Password policy: ≥ 10 characters with letters and digits. |
| API keys | Random 256-bit secret, only the SHA-256 is stored, revocable, `last_used_at` tracked, role-scoped, and limited to plans that include the `api` module. |
| Rate limiting | Per user and per API key (plan limit), plus login attempts per IP. The limiter is in-process (see Outstanding). |
| Input validation | Pydantic schemas with ranges and patterns; CSV range and unit validation; upload size limit (`YALA_MAX_UPLOAD_MB`); UTF-8 only. |
| Audit | Logins (including failures), user, key, plant, contract, event, import, model-run, alert-review, work-order and licence changes are written to `audit_logs` in the same transaction as the change. |
| Transport | TLS is terminated at the reverse proxy (`deploy/Caddyfile`, HSTS). The app sets `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, and `Cache-Control: no-store` on API responses. |
| Secrets | Read from the environment only. Production start-up refuses a default or short JWT secret. Webhook secrets are shown once. |
| Telemetry exposure | No unauthenticated data endpoints. `/metrics` exposes only counters and should be restricted at the proxy. |
| Frontend | All dynamic values are HTML-escaped. Libraries are vendored, with no third-party script CDN. The token is kept in `sessionStorage`, not cookies, so no CSRF surface. |

## Encryption at rest

Encryption at rest is provided by the storage layer: an encrypted managed PostgreSQL volume (for
example RDS, Cloud SQL or Azure with KMS) or LUKS on self-hosted disks. Backups are encrypted with
GPG (`deploy/backup/pg_backup.sh`) or by the managed service. Application-level field encryption is
not implemented.

## Outstanding before production

- **Shared rate limiter.** Needed for multiple instances (e.g. Redis).
- **Identity.** SSO/OIDC, MFA, password reset flow, refresh tokens and session revocation list.
- **Plant-level access scoping.** Today, `plant_owner` sees all plants in the organisation.
- **Secret management.** Integration with Vault or a cloud secret manager; key rotation runbook.
- **Assurance.** External penetration test, dependency scanning in CI, CSP header, and a formal data
  retention and deletion policy (GDPR / Kenya DPA / POPIA).
