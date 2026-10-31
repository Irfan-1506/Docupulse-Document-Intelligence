# DocuPulse Enterprise v4.3 — Phase 2 Security Implementation Plan

**Author:** Antigravity Engineering (Principal Security Architect & Lead Backend Engineer)  
**Date:** October 31, 2026  
**Phase:** Phase 2 — Enterprise Security Engineering  
**Objective:** Remediate all 12 identified vulnerabilities (`SEC-01` through `SEC-12`) with zero regressions on Phase 1 functionality.

---

## 1. Architectural Strategy & Phased Roadmap

To maintain application stability and prevent breaking changes across the working financial pipeline, Phase 2 is structured into 8 modular execution phases:

```
[Phase 2.1] Baseline & Threat Models (Complete)
     │
     ▼
[Phase 2.2] Multi-Tenant & User Relational Models + Alembic Migration
     │
     ▼
[Phase 2.3] Enterprise Authentication Engine (Argon2id, JWT, Cookies, Lockout)
     │
     ▼
[Phase 2.4] Centralized Role-Based Access Control (RBAC) & Permission Guards
     │
     ▼
[Phase 2.5] Multi-Tenant Data Isolation (Repository Scoping & Worker Context)
     │
     ▼
[Phase 2.6] Frontend Stored XSS Remediation & Content Security Policy (CSP)
     │
     ▼
[Phase 2.7] Secure File Upload Pipeline (ZIP Bomb, Pixel Flood, Size Limits)
     │
     ▼
[Phase 2.8] Audit Log Hash Chaining & Identity Spoofing Elimination
     │
     ▼
[Phase 2.9] API Hardening (CORS, Rate Limiting, Error Sanitization)
     │
     ▼
[Phase 2.10] Comprehensive Automated Security Test Suite (`tests/security/`)
```

---

## 2. Phase-by-Phase Technical Specifications

### Phase 2.2: Multi-Tenant & User Relational Models
1. **New Relational Entities (`src/db/models.py`):**
   - **`TenantModel`**: `id`, `name`, `slug`, `status` (`ACTIVE`, `SUSPENDED`), `created_at`, `updated_at`.
   - **`UserModel`**: `id`, `email`, `password_hash` (Argon2id), `full_name`, `is_active`, `is_verified`, `failed_login_attempts`, `locked_until`, `last_login_at`, `created_at`, `updated_at`.
   - **`TenantMembershipModel`**: `id`, `tenant_id`, `user_id`, `role` (`SUPER_ADMIN`, `TENANT_ADMIN`, `FINANCE_MANAGER`, `DOCUMENT_REVIEWER`, `VIEWER`), `created_at`.
   - **`RefreshTokenModel`**: `id`, `user_id`, `token_hash`, `expires_at`, `revoked_at`, `created_at`.
2. **Entity Tenant Ownership:**
   - Add `tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)` to:
     - `documents`
     - `processing_jobs`
     - `extracted_records`
     - `validation_results`
     - `audit_logs`
     - `document_embeddings`
3. **Alembic Migration & Legacy Data Strategy:**
   - Create a dedicated migration that creates a default legacy tenant (`"default-enterprise-tenant"`), assigns all existing legacy documents and jobs to this tenant, and sets up the primary admin user.

---

### Phase 2.3: Enterprise Authentication Engine (`src/auth/`)
1. **Password Security (`src/auth/password.py`):**
   - Use Argon2id hashing algorithm via `argon2-cffi` (or secure PBKDF2/bcrypt fallback with constant-time verification).
   - Enforce password policy: minimum 8 characters, requiring mixed characters.
2. **Tokens & Sessions (`src/auth/jwt.py`):**
   - Access tokens: Short-lived (15 minutes), signed with `HS256` or `RS256`, containing `sub` (user_id), `tenant_id`, `role`, `type="access"`.
   - Refresh tokens: Long-lived (7 days), stored hashed in database, rotated on every refresh.
   - Dual-mode client support:
     - Browser clients: Delivered via `HttpOnly`, `SameSite=Lax`, `Secure` cookies.
     - CLI / API clients: Accepted via `Authorization: Bearer <token>` header.
3. **Lockout & Rate Limiting (`src/auth/service.py`):**
   - Track `failed_login_attempts`.
   - After 5 consecutive failures, set `locked_until = now() + 15 minutes`.
4. **Auth Routes (`src/api/routes/auth.py`):**
   - `POST /api/v1/auth/register` (Initial onboarding)
   - `POST /api/v1/auth/login` (Returns session cookie + JWT)
   - `POST /api/v1/auth/refresh` (Rotates refresh token)
   - `POST /api/v1/auth/logout` (Revokes session)
   - `GET /api/v1/auth/me` (Current user profile & tenant roles)

---

### Phase 2.4: Centralized RBAC Framework (`src/auth/rbac.py`)
1. **Role & Permission Definitions:**
   - **Roles:** `SUPER_ADMIN`, `TENANT_ADMIN`, `FINANCE_MANAGER`, `DOCUMENT_REVIEWER`, `VIEWER`.
   - **Permissions:**
     - `document:create` (Upload new files)
     - `document:read` (View documents and extractions)
     - `document:review` (Correct extracted fields in HITL)
     - `document:approve` (Final approval / verification of invoices)
     - `document:reprocess` (Trigger reprocessing)
     - `document:delete` (Purge documents)
     - `audit:read` (View audit trails)
     - `tenant:manage` (Administer tenant users and settings)
2. **FastAPI Dependency Guards:**
   ```python
   def require_permission(perm: str):
       async def dependency(current_user: UserContext = Depends(get_current_user)):
           if perm not in current_user.permissions:
               raise HTTPException(status_code=403, detail="Insufficient permissions")
           return current_user
       return dependency
   ```

---

### Phase 2.5: Multi-Tenant Data Isolation (`src/db/repository.py`)
1. **Tenant-Scoped Repository Methods:**
   - Every read and write method receives `tenant_id: str`.
   - Relational queries add mandatory filter:
     ```python
     stmt = select(DocumentModel).where(
         DocumentModel.id == document_id,
         DocumentModel.tenant_id == tenant_id,
     )
     ```
2. **Worker Tenant Context Preservation:**
   - `JobPayload` includes `tenant_id`.
   - Worker validates tenant ownership before processing and persists results under the identical `tenant_id`.

---

### Phase 2.6: Frontend Stored XSS Remediation & CSP (`static/index.html`)
1. **Safe DOM API Replacement:**
   - Create a global `escapeHTML()` and `safeText()` helper.
   - Replace all 32 `innerHTML` string interpolations with safe DOM nodes (`element.textContent`) or encoded fragments.
2. **Content Security Policy (CSP):**
   - Inject restrictive CSP header via FastAPI middleware:
     ```
     default-src 'self';
     script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com;
     style-src 'self' 'unsafe-inline' https://fonts.googleapis.com;
     font-src 'self' https://fonts.gstatic.com;
     img-src 'self' data: blob:;
     connect-src 'self';
     frame-ancestors 'none';
     base-uri 'self';
     form-action 'self';
     ```

---

### Phase 2.7: Secure File Upload Pipeline (`src/ingestion/validators.py`)
1. **Upload Size Limits:**
   - Check stream size: Reject uploads exceeding `MAX_UPLOAD_SIZE_MB` (default 50MB) with HTTP 413.
2. **ZIP Bomb Detection in XLSX (`_validate_xlsx_deep`):**
   - Inspect `zf.infolist()`:
     - Max cumulative uncompressed size: $100\text{ MB}$.
     - Max compression ratio: $100:1$.
     - Max file count: $1,000$.
3. **Image Pixel Flood Guard (`_validate_image_deep`):**
   - Enforce `Image.MAX_IMAGE_PIXELS = 50_000_000` (50MP).
   - Reject images where `width * height > 50,000,000`.
4. **PDF Page Count Limit (`_validate_pdf_deep`):**
   - Reject PDFs with $> 2,000\text{ pages}$ to prevent resource exhaustion.
5. **CSV Formula Injection Sanitization (`src/ingestion/spreadsheet_processor.py`):**
   - Strip or prefix leading `=`, `+`, `-`, `@`, `\t` with `'`.

---

### Phase 2.8: Audit Log Integrity & Hash Chaining
1. **Server-Derived Actor Attribution:**
   - In `correct_record()`, replace `payload.corrected_by` with `current_user.email`.
2. **Cryptographic Hash Chaining (`src/db/models.py`, `src/db/repository.py`):**
   - Add columns to `audit_logs`:
     - `prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)`
     - `record_hash: Mapped[str] = mapped_column(String(64), nullable=False)`
   - Each entry computes:
     `record_hash = sha256(prev_hash + actor + field_changed + str(old_value) + str(new_value) + str(timestamp) + tenant_id)`
3. **Integrity Verifier:**
   - Implement `verify_audit_trail_integrity(record_id: str)` to verify uninterrupted hash chains and detect tampering.

---

### Phase 2.9: API Hardening & Middleware
1. **CORS Hardening:**
   - Replace `allow_origins=["*"]` with explicit configured `ALLOWED_ORIGINS` from environment.
2. **Rate Limiting:**
   - Implement token-bucket / sliding-window rate limiting on sensitive routes (auth, upload, search).
3. **Standardized Error Sanitization:**
   - Catch unhandled exceptions and return consistent error JSON without exposing internal stack traces.

---

### Phase 2.10: Dedicated Security Test Suite (`tests/security/`)
Implement tests for:
1. `test_authentication.py`: Unauthenticated rejections, invalid passwords, expired tokens, account lockout.
2. `test_rbac.py`: Role enforcement, unauthorized role attempts rejected with HTTP 403.
3. `test_tenant_isolation.py`: Cross-tenant reads, updates, searches, and reprocessing rejected.
4. `test_xss_protection.py`: Harmful payloads in filename, vendor, and line item rendered as text.
5. `test_upload_security.py`: Oversized files, ZIP bombs, pixel flood images, corrupted files rejected.
6. `test_audit_integrity.py`: Spoofed actor rejected; hash chain tampering detected.
7. `test_api_security.py`: CORS origin validation, security headers present.
