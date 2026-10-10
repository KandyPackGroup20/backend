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

## Member: Phase 2 — Rail Capacity Allocation and Spillover (Feature 4.2) — 2026-10-10

Scope: existing rail APIs, allocation transactions, actual destination/time/space contracts, and necessary station-manifest handoff. `fullworkflow` was initially clean with Phase 1 in the baseline. Prior entries were preserved; no README/prompt/main edits, pushes or merges.

### Existing behavior and defects fixed

Trip CRUD, pending orders, suitable-trip lookup, stored-procedure auto-allocation, breakdowns, reversal and Logistics Manager/SuperAdmin authorization already existed. FastAPI, PyMySQL and parameterized SQL remain the implementation. Existing `/api/v1/rail/trips`, `/orders/pending`, `/allocate`, `/orders/{order_id}/allocations` contracts are retained rather than duplicating the prompt's unversioned/renamed routes.

`app/api/v1/rail.py` changes:

- Manual allocation and suitable-trip lookup used the customer's mutable profile route instead of the order's committed delivery route. Both now use `customer_order.delivery_route_id` consistently with the stored procedure.
- Selected trips now require active supported hubs, a future scheduled departure, matching order destination, arrival before the delivery-date cutoff and an unreceived manifest. Empty/nonpositive item quantities and invalid rates are rejected.
- Manual allocation locks order before trip, uses exact Decimal/rounded function results and current capacity reads, writes missing order-status history, and shares bounded deadlock/timeout retries with the automatic path. Duplicate allocations fail on the locked order state.
- Schedule inputs normalize offset-aware timestamps to Asia/Colombo before writing existing MySQL DATETIME fields. Capacities use two-decimal Decimal validation instead of float. Past trip creation/edits, inactive trip edits and edits missing existing order deadlines are rejected; departed/arrived trips cannot be reactivated.
- Supported destination validation and `/api/v1/rail/stations` supply real active IDs to the existing form, replacing its erroneous seed-ID assumptions. This endpoint is a missing data connection, not an alias of an existing API.
- Allocation replies include the existing order-item identity; breakdowns sort by departure, trip and allocation ID. Unknown-order breakdowns return 404.

Other changed files: `tests/test_phase2_rail.py` (real DB regression suite) and this appended `workflow-test.md` section. No backend ORM or business-feature rewrite was introduced.

### SQL initialization / transaction ownership

Apply sibling database `15_phase2_rail_workflow.sql` to the explicitly selected initialized database, with rail writes paused. It installs the capacity/quantity locking fixes, active-hub guards, received-cargo protection and atomic pending-manifest handoff, and repairs only unambiguous future missing manifests. See the database workflow section for exact sources and migration precautions. No existing DB was migrated in this session.

The stored procedure owns its existing START TRANSACTION/COMMIT/ROLLBACK; the backend does not pretend it can wrap that commit in a larger transaction. The selected-trip path owns its connection transaction. Failed allocation rolls back allocation/manifest/order/history writes; rejection audit records from the existing automatic procedure may intentionally persist after rollback. Capacity remains derived rather than duplicated in a new field.

### Tests and results

Run from backend: `.venv/Scripts/python.exe -m unittest discover -s tests -p test_phase2_rail.py -v`.

The suite runs 28 tests: 15 rail tests plus the 13 Phase 1 regressions. It uses actual FastAPI handlers and real MySQL, creates a unique disposable schema, loads the actual feature SQL, and deletes only that schema. It never starts the application's automatic migration/seed hooks. Existing databases and running services were not changed. Final pass/fail result is recorded in the completion entry below.

Rail coverage: single allocation and duplicate retry; three-trip fractional-space spillover; complete shortage rollback across multiple products; two concurrent requests for one order; two orders competing for the same capacity; stale repeatable-read capacity and quantity guards; order-route versus customer-profile-route conflict; selected-trip empty/invalid/cutoff failures; exact time/decimal validation; allowed hubs; Logistics Manager success/customer mutation denial; capacity shrink/cancel rejection; received-manifest exclusion; reversal releases capacity; generated migration applied twice without allocation loss and with one missing future manifest restored.

The real receipt integration simulates arrival, receives every spillover leg through the existing inventory API/procedure, checks stock totals and rejection of duplicate receipt, and confirms order status advances only after the final leg. No external notifications were sent. Python compilation passed. Preliminary test-harness assertions, the real stale-read/manifest defects, and one generated migration SQL ambiguity were fixed before final verification.

### Remaining dependencies / approval

Browser interaction remains blocked (no connected browser). Production-domain/cookie/deployment checks from Phase 1 still apply. Historical allocations missing manifests require explicit inventory reconciliation; the migration does not synthesize historical receipts. Empty or premature receipt policy belongs to the station member and remains to be verified. The rail tests do not certify truck dispatch, delivery completion or business reporting; those members' features were not redesigned.

Ready for Phase 2 code review after the final passing suite, with the SQL/backend/frontend changes reviewed together. This is not full-lifecycle or deployment approval.

Phase 2 completion result: **28/28 passed** in the final complete run (15 rail tests + 13 identity regressions), including migration repeatability/backfill and allocation-to-inventory receipt. The generated disposable schema was cleaned up. Migration consistency and all repository diff checks passed. No existing database, main branch, push or merge was changed.

## Phase 3 member — start delivery, fatigue information, eligible crew (2026-10-10)

Resumed the interrupted work without resetting it. All three repositories were on `fullworkflow`; backend and frontend contained the preserved 14-file change set, and database was clean. No applicable AGENTS.md exists in these repositories or their parent paths; the instructions in the archived option3 checkout do not apply. No README, commit, push, merge, or main change was made.

### Scope, defects, and files

- `app/api/v1/roster.py`: adds authenticated GET `/api/v1/roster/availability` (route_id, truck_id, from/to with explicit offsets, optional selected driver_id/assistant_id) and POST `/api/v1/roster/schedules/{roster_id}/start`. Reuses existing roster read/write guards and no-store/error handling. Existing writers include DISPATCHER, SUPERADMIN and LOGISTICS_MGR; their permissions were preserved.
- `app/roster/cargo.py`: missing explicit departure transition implemented in one transaction. Locks route/truck/roster, attached orders/items/products, rail receipt evidence and deliveries; validates membership, scheduled/assigned state, route, full receipt, and station order status. Roster and deliveries become IN_TRANSIT; orders become OUT_FOR_DELIVERY, with status history and one ACCEPTED START_DELIVERY audit. Concurrent/repeated starts have no duplicate effect. Failures roll back all writes. The extra membership recheck rejects a loading list changed while locks were acquired.
- `app/roster/availability.py`: previously the catalog was unfiltered. The read-only snapshot now calls the existing assignment policy for candidate pairs, using selected counterpart, route and truck. Excludes inactive/wrong-type staff, overlaps, existing consecutive-duty/rest violations and weekly overages. Computes existing/proposed/projected seconds and remaining seconds for every affected Colombo week using existing splitting and limit constants.
- `tests/test_phase3_roster.py`: disposable real-MySQL/FastAPI verification, extending the existing identity/rail harness.
- `tests/test_roster_availability.py`: candidate filtering, exact cap, cancellation, pair, route, rest and cross-week regressions.
- `tests/test_roster_hours.py`: repaired legacy unit fixtures after Phase 1 added current-account database authentication. Only the account lookup is mocked; signed JWT decoding, actual authentication dependency, role checks and reset checks still execute. Production authentication was not changed or bypassed.
- `workflow-test.md`: this appended member record.

### Preserved behavior and schema decisions

Create roster → attach whole orders → start delivery remains three separate requests. Neither creation nor attachment marks orders OUT_FOR_DELIVERY. Existing assignment locking and DemoV1RosterPolicy validation still run at submission, independently of advisory availability reads; rejection handling and accepted-only roster audit remain unchanged. Rest rules mean the existing touching-duty chain rules, not a newly invented minimum-rest interval. Equality with the 40-hour driver / 60-hour assistant limit remains valid.

Uses existing singular tables and supported IN_TRANSIT status, not new tables or aliases. The unique audit_log.roster_id link is reserved by the schema for ASSIGN_ROSTER: START_DELIVERY uses entity_name/entity_id with a NULL roster_id, matching the existing unrelated-action convention. No rejected audit is written. Rail audit outcomes are a separate earlier feature and were not changed.

No schema migration is needed. Existing roster SQL (`09_roster_assignment.sql`, `10_roster_reporting.sql`) and Phase 1/2 schema/triggers are prerequisites. Do not rerun destructive initialization on an existing database. The test harness creates a generated `kandypack_phase1_test_<uuid>` database, loads the actual schema/rail/receipt/roster SQL after stripping original database selection/reset statements, and removes only that generated schema. Application startup migration/seed hooks are not run. `ROSTER_DATA_MODE=mysql` is required for these features.

### Actual verification after resume

From backend, `.venv/Scripts/python.exe -m unittest discover -s tests -p <filename> -v`:

| Filename | Result |
| --- | --- |
| test_phase3_roster.py | 34/34 passed in 50.884 seconds: 6 Phase 3 + 28 inherited identity/rail tests |
| test_roster_hours.py | 11/11 passed |
| test_roster_availability.py | 5/5 passed |
| test_roster_policy.py | 17/17 passed |

Integration evidence includes actual rail allocation and station receipt, unchanged order state after schedule creation/attachment, successful dispatcher start, repeated and concurrent starts, cancelled/completed/empty rejection, station-status recheck, customer/anonymous denial, and unchanged inventory. A deliberately failing MySQL audit trigger proved rollback of roster, delivery, order and status history; its expected logged exception is part of the passing test. Availability is refreshed for overlap/touching/later intervals; inactive crew are removed and final submission rejects them. All roster audit outcomes remain ACCEPTED. Last disposable schema: `kandypack_phase1_test_b634a0ceb62041709eb5791dc16d85a2`, cleaned up by the harness.

Earlier development runs exposed fixture errors (staff email domain/reset trigger expectations, an overly broad audit assertion, and legacy hours authentication setup); these were corrected, not hidden. Above counts are the fresh resumed passing runs. Sibling frontend: 47/47 tests, TypeScript, and changed-source lint passed. `git diff --check` passed in all three repositories.

### Dependencies and remaining verification

Deploy with the matching frontend changes. Existing Next rewrite forwards both new paths; API middleware exclusion leaves authorization to FastAPI. Config/client contracts are tested, but live browser-to-Next-to-backend proxy traffic and interactive UI behavior are not certified: the Browser skill returned no browser and an empty browser list. Manual UI approval should still exercise input changes/selection clearing, failure/retry, and loading-list status refresh on narrow screens. Inventory deduction and delivery completion remain deliberately outside Phase 3. No database repository file was changed. Ready for code review with that explicit UI verification limitation; this is not merge or deployment approval.

## Phase 4 member — Station inventory and warehouse operations (2026-10-10)

Inspected clean `fullworkflow` branches and reused the prior implementation. No applicable AGENTS.md in these repositories; archived instructions do not apply. Existing manual parameterized SQL, FastAPI and MySQL remain. No README, main, commit, push or merge changes.

### Scope, defects and changed files

- `app/api/v1/inventory.py`: enforce current database station assignment on inventory, manifest lists/items, bins, adjustment writes/history and station reports. Managers use existing manager_id; warehouse staff use the new nullable user.station_id. Removed silent station-1 fallback. Added `/inventory/stations` for actual authorized choices. Warehouse staff can inspect/receive their assigned station's shipments and read its summary. Existing privileged access remains. Manifest list filters actual Kandy-to-destination trips and adds cargo_units.
- Same file: validate positive IDs, integer adjustment quantities and nonblank bounded reasons; DAMAGED/LOST/EXPIRED must deduct. Existing free-text recount corrections remain supported. Lock inventory for adjustment, retain trigger-driven updates, and support optional Idempotency-Key with conflict detection. Underflow rolls back the adjustment row. Response quantity is read before commit under the same lock. Bin no-op/retry now succeeds while nonexistent inventory/cross-station bins are rejected.
- Same file: preserve existing receipt procedure argument order and transaction ownership. End authorization read transaction before calling it; set Colombo session timezone. Explicit receipt guards return actionable conflict codes. Already-received remains the existing 409 response without duplicate stock. Passwordless legacy session endpoint remains disabled.
- `app/api/v1/auth.py`: minimal integration only—optional station_id during SuperAdmin warehouse-staff provisioning; verify role and active station, store assignment atomically. No authentication/role/reset redesign.
- `tests/test_phase4_inventory.py`: disposable MySQL/FastAPI verification plus inherited lifecycle regressions.
- `workflow-test.md`: this member's evidence.

### Reconciliation and dependencies

Retained `/api/v1/inventory/...`, singular baseline tables and the four-argument receipt procedure instead of duplicating prompt aliases. The plan explicitly requires station-level staff scoping; no warehouse assignment existed. With no clarification reply, selected explicit nullable warehouse assignment rather than broad all-station permissions. Unassigned staff fail closed; managers retain manager_id scoping. `reason` stays VARCHAR for existing corrections/history rather than a destructive ENUM conversion. Station inventory remains product-level; cargo quantities are units, not invented physical package records.

Apply sibling `database/16_phase4_station_workflow.sql` before deploying the backend. It adds user.station_id and stock_adjustment.request_key and updates receipt. Do not rerun destructive initialization on an existing DB. New warehouse staff use the existing provisioning API/UI selector; explicitly assign legacy staff using approved user/station IDs. Matching frontend uses backend station choices and stable request keys. External callers should supply Idempotency-Key for adjustment retries; keyless historical callers remain supported and each submission represents a distinct adjustment.

### Tests and results

`.venv/Scripts/python.exe -m unittest discover -s tests -p test_phase4_inventory.py -v`: **43/43 passed** in the full run (9 Phase 4 + 34 earlier-phase regressions), 69.267 seconds. Real MySQL/FastAPI, generated disposable schema only; startup migrations and existing databases are untouched. Generated schema was cleaned up. After final null-status guard refinement, the affected `-k receipt` subset was rerun; completion recorded below.

Verified own-station access and cross-station denials for both target roles; unassigned denial; real warehouse provisioning; empty/future/wrong-destination receipt rejection; duplicate/concurrent receipt exactly once; injected order-history failure rolls back stock/manifest/order; closed-order rejection; sequential and concurrent damage handling, duplicate key replay/conflict, underflow rollback; integer/reason validation; bin retry and foreign-bin denial. Scoped report totals after damage match stock and loss value. Inherited tests verify spillover receipt completes only after all legs, and receipt → whole-order roster attachment → explicit delivery start remains working without dispatch stock deduction. No failed roster audit behavior changed.

Frontend: 56 existing regressions plus 2 warehouse render checks passed; TypeScript passed. Scoped lint has zero errors and three existing warehouse warnings (unused import and two navigation recommendations). Migration consistency and diff checks passed. One migration-check command was mistakenly invoked with Node and failed before execution; rerun with Python passed. Earlier 40/42-test runs also passed; full result above includes later receipt/report assertions.

### Remaining issues / readiness

Ready for code review, not deployment approval. Migration and explicit legacy warehouse assignments remain deployment prerequisites. Browser runtime returned an empty list; interactive intake/damage dialog, switching stations, retry behavior and responsive UI/live Next proxy checks are not claimed. Delivery completion and stock deduction at dispatch remain outside this change. Existing bin/search/report UI was reused; no new package-level tracking.

Phase 4 completion: final `-k receipt` subset **4/4 passed** in 9.531 seconds after the null-status refinement (spillover, concurrent/rollback, cancelled/null rejection, roster handoff). Test schema cleaned up. This reruns four of the 43 passing integration tests above. Frontend total is 58 passing tests; final branch and diff checks passed across all three repositories.
