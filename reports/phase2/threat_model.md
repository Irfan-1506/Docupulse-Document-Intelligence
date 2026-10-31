# DocuPulse Enterprise v4.3 — Threat Model Report

**Author:** Antigravity Engineering (Principal Security Architect & OWASP Specialist)  
**Date:** October 31, 2026  
**Methodology:** STRIDE Modeling & Data Flow Analysis (DFA)  
**System:** DocuPulse Enterprise v4.3 Multi-Tenant Document Intelligence Platform  

---

## 1. System Overview & Architecture Decomposition

DocuPulse ingests, parses, extracts, and stores sensitive enterprise accounting data. The system operates across five primary security trust boundaries:

```
+---------------------------------------------------------------------------------------------------+
| [TRUST BOUNDARY 1: External Untrusted Clients / Internet]                                         |
|                                                                                                   |
|   Tenant A Users        Tenant B Users        Anonymous Attackers       Compromised Browsers      |
|        │                     │                         │                         │                |
+--------┼─────────────────────┼─────────────────────────┼─────────────────────────┼----------------+
         │ HTTPS / WSS         │ HTTPS / WSS             │ HTTP Scans              │ XSS Payloads   
         ▼                     ▼                         ▼                         ▼                
+---------------------------------------------------------------------------------------------------+
| [TRUST BOUNDARY 2: FastAPI Application Gateway & Ingestion Middleware]                            |
|                                                                                                   |
|   - Authentication Engine (Session / JWT / Argon2id)                                              |
|   - RBAC & Authorization Guard (Role-to-Permission Mapper)                                        |
|   - Tenant Context Extraction & Verification Middleware                                           |
|   - File Validation & Decompression Explosion Guard                                               |
+--------┬─────────────────────┬─────────────────────────┬─────────────────────────┬----------------+
         │ Queries             │ Enqueue                 │ Log Events              │ Read/Write     
         ▼                     ▼                         ▼                         ▼                
+-------------------+  +-------------------+  +-------------------+  +------------------------------+
| [BOUNDARY 3: DB]  |  | [BOUNDARY 4: MQ]  |  | [BOUNDARY 5: LOG] |  | [BOUNDARY 6: Storage]        |
| PostgreSQL 16+    |  | Redis Queue & DLQ |  | Tamper-Evident    |  | Local / Cloud Volume         |
| - Tenant Isolation|  | - Tenant Context  |  | Audit Ledger      |  | - Random UUID Filenames      |
| - RLS Policies    |  | - Signed Payloads |  | - Hash Chaining   |  | - Private Storage Root       |
+-------------------+  +---------┬---------+  +-------------------+  +--------------▲---------------+
                                 │ Dequeue                                          │ Read File     
                                 ▼                                                  │               
                       +------------------------------------------------------------┴---------------+
                       | [TRUST BOUNDARY 7: Background Worker Daemon]                               |
                       | - Tenant Context Re-Verification                                           |
                       | - Sandboxed Ingestion, OCR, and AI Extraction                              |
                       +----------------------------------------------------------------------------+
```

---

## 2. Threat Actor Profiles

| Threat Actor | Motivation | Capabilities | Access Level |
|--------------|------------|--------------|--------------|
| **External Anonymous Attacker** | Corporate espionage, data theft, service disruption | Internet-accessible network scanners, crafted malformed documents, automated XSS/SQLi attack tools | Zero credentials; accesses public endpoints |
| **Malicious Authenticated User (Tenant A)** | Competitive intelligence, unauthorized financial insight | Legitimate account in Tenant A; uses valid tokens to probe Tenant B endpoints | Low privilege within Tenant A; attempts cross-tenant horizontal escalation |
| **Disgruntled Reviewer (Insider)** | Fraudulent approval, invoice modification, sabotage | Can review and correct invoices; attempts to forge reviewer signatures or conceal audit trails | Reviewer role within tenant; attempts vertical privilege escalation |
| **Compromised Background Worker** | Remote code execution, lateral database access | Executes worker processes on host; attempts to read other tenant files or forge database entries | Worker execution context; access to Redis and storage volume |

---

## 3. STRIDE Threat Analysis

### 3.1 Spoofing (Identity Deception)
- **Threat S-01: Reviewer Identity Spoofing in Manual Corrections**
  - *Mechanism:* `PATCH /records/{record_id}/correct` accepts `payload.corrected_by` from the caller. A malicious user can submit corrections under the name of the CFO or internal auditor.
  - *Impact:* Complete breakdown of corporate compliance and accountability.
  - *Mitigation:* Derive actor identity strictly from the cryptographically verified server-side session/token. Reject client-provided identity strings.
- **Threat S-02: Tenant Identity Spoofing**
  - *Mechanism:* Attacker sends `X-Tenant-ID: tenant-b` header or payload parameter while authenticated as Tenant A.
  - *Impact:* Access to another company's financial documents.
  - *Mitigation:* Derive `tenant_id` exclusively from server-side database user membership records.

### 3.2 Tampering (Data Modification)
- **Threat T-01: Audit Log Modification & Deletion**
  - *Mechanism:* If audit log records are standard mutable database rows, an insider with database access could alter or delete audit trails.
  - *Impact:* Undetected financial fraud.
  - *Mitigation:* Tamper-evident cryptographic hash chaining (`sha256(prev_hash + entry_data)`), append-only repository methods, and periodic integrity verification.
- **Threat T-02: Document Payload Tampering in Redis Queue**
  - *Mechanism:* A compromised local process inserts forged job payloads into `docupulse:queue:jobs` targeting unauthorized file paths.
  - *Impact:* Arbitrary file processing or cross-tenant document corruption.
  - *Mitigation:* Verify tenant ownership and file path validity inside `process_document_job()` before executing worker pipelines.

### 3.3 Repudiation (Denial of Action)
- **Threat R-01: Unattributed Document Reprocessing or Deletion**
  - *Mechanism:* Triggering `/jobs/{id}/reprocess` without recording the acting user's ID.
  - *Impact:* Inability to determine who triggered reprocessing of disputed invoices.
  - *Mitigation:* Enforce mandatory audit event creation for every state mutation, capturing `actor_id`, `tenant_id`, `timestamp`, `ip_address`, and `correlation_id`.

### 3.4 Information Disclosure (Data Leakage)
- **Threat I-01: Cross-Tenant Financial Document Leakage**
  - *Mechanism:* Insecure Direct Object Reference (IDOR) on `/documents/{document_id}/results` and `/records/{record_id}`.
  - *Impact:* Catastrophic breach of customer financial records, supplier contracts, and pricing terms.
  - *Mitigation:* Centralized tenant-scoped repository queries (`WHERE tenant_id = :active_tenant_id`) and Row-Level Security (RLS) enforcement.
- **Threat I-02: Internal Secret & Stack Trace Exposure**
  - *Mechanism:* Unhandled exceptions returning database tracebacks, file paths, or credentials in HTTP 500 responses.
  - *Impact:* Reconnaissance data provided to attackers.
  - *Mitigation:* Global FastAPI exception handlers sanitizing all output to standardized error JSON structures.

### 3.5 Denial of Service (Service Exhaustion)
- **Threat D-01: Decompression Bomb (ZIP Bomb in XLSX)**
  - *Mechanism:* Uploading a crafted 42KB XLSX archive that unpacks to 4.5TB of XML data.
  - *Impact:* Server memory exhaustion, disk exhaustion, process crash.
  - *Mitigation:* Pre-decompression inspection of cumulative uncompressed size in `zipfile.ZipFile.infolist()` with strict 100MB ceiling and 100:1 compression ratio limit.
- **Threat D-02: Image Pixel Flood / Decompression Bomb**
  - *Mechanism:* Uploading an image with 100,000 x 100,000 dimensions (10 gigapixels).
  - *Impact:* Memory crash during PIL decompression.
  - *Mitigation:* Enforce `Image.MAX_IMAGE_PIXELS = 50_000_000` (50MP max) and pre-allocate sanity checks.
- **Threat D-03: Unbounded Concurrent Upload Flooding**
  - *Mechanism:* Scripted automated multi-gigabyte upload requests to `/documents/upload`.
  - *Impact:* Bandwidth and CPU starvation.
  - *Mitigation:* Redis-backed rate limiting per IP / tenant and HTTP request body size limits (e.g. 100MB max).

### 3.6 Elevation of Privilege (Unauthorized Capabilities)
- **Threat E-01: Viewer Role Escalating to Financial Reviewer**
  - *Mechanism:* A user with `VIEWER` role submits `PATCH /records/{record_id}/correct`.
  - *Impact:* Unauthorized modification of financial records.
  - *Mitigation:* Role-Based Access Control (RBAC) enforced server-side via FastAPI dependencies requiring `document:review` permission.
- **Threat E-02: Stored Cross-Site Scripting (XSS) Leading to Session Hijacking**
  - *Mechanism:* Injected script in invoice description executed in browser of Finance Manager or Admin.
  - *Impact:* Session hijacking, theft of authentication tokens, automated forged approvals.
  - *Mitigation:* Remove all unescaped `innerHTML` assignments, use safe DOM APIs (`textContent`), store session tokens in `HttpOnly` cookies, and enforce a strict Content Security Policy.

---

## 4. Threat Prioritization & Remediation Mapping

| Priority | Threat ID | Target Subsystem | Remediation Architecture |
|----------|-----------|------------------|--------------------------|
| **P0 (Critical)** | S-01, I-01, E-01 | Auth & RBAC | Implement Argon2id User Model, Centralized Permissions, and Tenant Context |
| **P0 (Critical)** | E-02 | Frontend Security | Complete `innerHTML` replacement with DOMPurify / `textContent`, and strict CSP |
| **P0 (Critical)** | I-01 | Data Isolation | Multi-tenant schema migration (`tenant_id` on all tables) and tenant-scoped queries |
| **P1 (High)** | D-01, D-02, D-03 | File Ingestion | ZIP bomb guard, image pixel limits, and request stream rate limiting |
| **P1 (High)** | T-01, S-01 | Audit Logging | Cryptographic hash chaining and server-derived actor attribution |
| **P2 (Medium)**| I-02 | API Security | CORS origin allowlisting, CSRF token validation, and sanitization middleware |
