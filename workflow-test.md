# Workflow verification log

## Member: Phase 1 — Identity, RBAC and subdomain security — 2026-10-10

Scope: authentication, provisioning, current-account authorization, customer ownership and necessary lifecycle security connections. Only the supplied Phase 1 prompt was handled. `fullworkflow` was initially clean; existing features were inspected before editing. No push, merge, main change, README edit or prompt edit.

### Working baseline / conflicts resolved

FastAPI, PyMySQL and manual parameterized SQL were already in use and remain in use. The prompt's SQLAlchemy/ORM suggestion conflicts with the session rules and was not adopted. Existing `/api/v1/auth/login`, `/register`, `/users`, `/change-password`, `/me` and `/logout` contracts remain authoritative; no duplicate `/api/auth` or `/api/admin/users` APIs were added. `require_roles` is the existing equivalent of the proposed renamed role dependency. Existing schema names, roles and statuses are preserved. Bcrypt is already used directly; no hashing-library replacement was needed.

### Verified defects and changed files

- `app/core/security.py`: removed plaintext password acceptance; checks UTF-8 bcrypt byte limits; requires JWT expiry/subject; loads current active account and role from MySQL instead of trusting stale claims; enforces forced reset before business APIs; checks admin-domain customer exclusion. Removed suppression of weak-key warnings.
- `app/api/v1/auth.py`: removed universal `password123` bypasses from login/password change; secure HttpOnly SameSite=Lax cookies with JWT-aligned lifetime; domain context comes from allowlisted Origin or actual Host instead of submitted portal choice alone; admin registration rejected; explicit staff role allowlist and required temporary password; delivery staff identity created in the same transaction; provisioning sets SQL guard context; concurrent database conflicts return controlled errors; database internals removed from public auth errors. Updating staff profiles no longer fabricates customer records or default routing. New passwords must differ from the old password.
- `app/core/cache.py`: process-local, locked sliding failure window; five failures in 60 seconds lock the normalized email for 900 seconds. Replaces the incorrect one-minute lock. Redis remains available for unrelated cache use.
- `app/core/config.py`, `.env.example`: JWT secret is required and at least 32 characters; secure cookies default on; explicit allowed browser origins.
- `app/main.py`: credentialed CORS origin allowlist and mutating-request Origin checks replace wildcard credentialed CORS.
- `app/api/v1/orders.py`: anonymous order-history/tracking disclosure fixed, customer without a customer row fails closed, tracking checks customer ownership. Existing staff read-role access and business queries are retained.
- `app/api/v1/inventory.py`: legacy `/inventory/session` allowed passwordless impersonation by role/email. It now returns 410 and directs callers to password login; no token is minted. No warehouse business logic was redesigned.
- `app/api/v1/notifications.py`: existing notification reads use current account status/role; unauthenticated notification sending is closed with SUPERADMIN authorization. Tests never send messages.
- `tests/test_phase1_workflow.py`: repeatable isolated real-MySQL regression suite using FastAPI TestClient, not a mock database. Application startup migration/seed hooks are deliberately not invoked.
- `workflow-test.md`: this record.

### SQL / configuration / deployment

1. Apply sibling database `14_phase1_identity_guard.sql` to the explicitly selected target database; never run the resetting base schema on an existing database. No existing database was migrated during this work.
2. Configure a random `SECRET_KEY` of at least 32 characters. The known demo/default key must be rotated; rotation invalidates old JWTs. Real environment files were not edited or printed.
3. Production HTTPS uses `SESSION_COOKIE_SECURE=true`. Local HTTP development may explicitly set it to false. Cookies are host-only and use the frontend same-origin API proxy; no broad parent-domain cookie is introduced.
4. Set `AUTH_ALLOWED_ORIGINS` as a JSON array if deployment origins differ. Defaults include the two production HTTPS domains and localhost/admin.localhost port 3000. Do not trust arbitrary forwarded host headers.
5. The requested in-memory lockout is per process and resets on restart. Use one authentication worker/instance for this policy; distributed deployment requires a shared atomic limiter and is not verified here.
6. Existing plaintext/demo password records will no longer authenticate. An authorized password reset/bootstrap process must supply real bcrypt hashes; this task did not rewrite existing users or passwords.

### Tests and results

From this repository: `.venv/Scripts/python.exe -m unittest discover -s tests -p test_phase1_workflow.py -v`.

Final result: **13 passed**, against real MySQL and the actual FastAPI ASGI app. The harness generates its own signing key and unique disposable schema; creates only test accounts; executes the actual schema/triggers and selected-database migration; and drops only its created database. Covered:

- Bcrypt-only verification and both universal-password bypass regressions.
- HttpOnly/Secure/SameSite flags, password reset completion and active-account checks.
- Atomic registration, invalid-FK rollback, duplicate and concurrent submissions.
- Staff allowlist, forced reset, SQL provisioning guard and delivery_staff linkage.
- Fifteen-minute lock after five failed attempts, with deterministic clock advancement; concurrent login requests cannot skip the threshold.
- Forged-token rejection and current DB role overriding stale signed role claims.
- Anonymous/customer denials for staff, rail, inventory, roster and report APIs.
- Actual owner-only order list/tracking versus a different customer.
- Untrusted Origin, admin customer access, retired passwordless session and unauthenticated notification sending.
- Staff profile update does not create a customer.

Existing `test_phase2_auth.py` passed. Its parameter tuple assertion is only a utility/static check, not proof of SQL injection resistance. Python compilation passed. Backend tests had preliminary path and legacy-session regression failures that were fixed before the final passing run.

Legacy suites that write to a configured existing database were not run wholesale. No successful order-to-delivery lifecycle is claimed by these identity tests.

### Lifecycle dependencies / remaining issues

Registration establishes customer ownership for ordering. Delivery provisioning establishes the staff IDs consumed by roster assignment. Rail allocation/spillover, station receipt/inventory, truck roster/cargo assignment and reporting use the shared current-account role/reset checks; their business behavior was not rebuilt. Existing reporting role policy (SUPERADMIN) is preserved. The inspected roster router provides assignments and cargo loading but no dispatch/completion transition endpoint; later workflow members must resolve/verify those lifecycle transitions.

The schema has customer ownership and global staff roles, not independent organization tenants or universal station-staff membership. No new tenant or station-assignment model was invented. SQL database roles were inspected statically, but live server-global grant isolation was not tested. Production TLS, reverse proxy origins/cookies, and browser interaction remain deployment/UI checks. Stateless bearer tokens still expire by JWT lifetime; logout clears the browser cookie rather than maintaining a token revocation registry.

Approval: ready for Phase 1 code review with the above explicit deployment requirements; not a claim that the full logistics lifecycle or production deployment is approved.
