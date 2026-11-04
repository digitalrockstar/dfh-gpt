# DFH GPT — Rebuild Blueprint

**Status:** Approved direction; implementation in progress  
**Source baseline:** `digitalrockstar/dfh-demo` at `c4b8c523fd7eaa303514821d9d0a48a743e721ed`  
**Target:** `digitalrockstar/dfh-gpt`  
**Operating principle:** Preserve the source repository as an immutable reference. All product development happens in this repository.

## 1. Product intent

Build a retail operating system for a multi-region, multi-format dry-fruit retailer operating at 500+ store scale. The platform must connect the complete operating cycle:

**Plan → Buy → Receive → Store → Sell → Replenish → Reconcile → Learn**

It is an operational system of record, not a collection of disconnected dashboards. Every important KPI should be traceable to underlying business events and transactions. Every exception should have a business owner, a next action, and an auditable resolution.

The existing demo is a valuable functional baseline. It contains working concepts and implementations for POS, SKU, procurement, inventory, wastage, demand, finance, CRM/WhatsApp and Control Tower. It is not assumed to be production-ready. We will preserve useful domain logic while deliberately replacing weak foundations and closing integration gaps.

## 2. Product principles

1. **One source of truth:** inventory, sales, customer, procurement and finance records must have explicit system-of-record ownership.
2. **Transaction integrity first:** stock, payment, invoice, journal and settlement operations must be atomic, idempotent where appropriate, auditable and safe under concurrency.
3. **Operationally useful UX:** prioritize workflows, queues, decisions and exceptions over decorative charts.
4. **Role-aware, not role-fragmented:** one permission and data-scope model, with tailored workspaces for each role.
5. **Offline by design where required:** POS must continue selling safely through connectivity loss and reconcile deterministically.
6. **Explainable analytics:** every metric has a definition, owner, grain, source, refresh SLA and reconciliation method.
7. **Progressive complexity:** modular monolith first; do not introduce microservices without a measured need.
8. **Demo data must be labelled:** synthetic data, assumptions and modeled results must never be presented as live business facts.
9. **Secure defaults:** no hard-coded secrets, demo credentials in production, or permissive fallback authentication.
10. **Prove complete journeys:** release acceptance is based on end-to-end business scenarios, not only screen completion.

## 3. Recommended target architecture

### Application shape

Start with a **modular monolith** with explicit domain boundaries and stable internal contracts. This minimizes operational overhead while preserving the option to extract services later.

- **Backend:** Python, FastAPI, Pydantic and SQLAlchemy 2.x.
- **Database:** PostgreSQL as the transactional system of record.
- **Schema changes:** Alembic migrations, reviewed and tested in both clean-install and upgrade paths.
- **Analytics:** governed SQL models and pre-aggregations/materialized views for operational dashboards; Parquet/DuckDB for large historical analysis and reproducible offline model work where justified.
- **Frontend:** TypeScript application with a reusable component system, route-level modules, typed API client and responsive layouts. Prefer a modern build pipeline over growing the current hand-wired vanilla-JS surface.
- **POS:** dedicated offline-capable application surface with local persistence, durable sync queue, idempotency keys, conflict handling and explicit sync state.
- **Background work:** durable job queue for imports, exports, notifications, forecast runs and scheduled reporting. Jobs require retries, dead-letter visibility and idempotent handlers.
- **Integration boundary:** adapters for payment, messaging, tax, commerce and logistics providers. External systems must not be coupled directly to domain rules.
- **Deployment:** environment-based configuration, health/readiness endpoints, structured logs, metrics, error reporting, backups and documented restore drills.

Do not split into microservices in the first rebuild. Keep domain modules independently testable and communicate through application services/domain events.

### Data ownership and transaction rules

- Inventory quantity is derived from an immutable stock-movement ledger. A balance table may be maintained as a projection/cache, never as an independent competing truth.
- POS invoices and returns are the source of truth for retail sales events. Analytics history must ingest these events with documented latency and reconciliation.
- Finance postings are immutable journal entries. Corrections use reversal and replacement, not silent mutation.
- Procurement commitments, receipts, supplier invoices and payments remain distinct lifecycle objects with explicit matching rules.
- Customer identity and consent are governed centrally. POS, CRM and messaging reference the same customer identity model.
- Use an outbox pattern for reliable publication of business events after database commit.
- Every business document has a stable identifier, lifecycle state, created/updated actor, timestamps and audit history.
- Monetary values use fixed-precision decimal arithmetic and explicit currency. Never use binary floating point for money.
- All stock and financial posting operations must define concurrency and duplicate-submission behavior.

## 4. Domain map and rebuild scope

| Domain | Core capabilities | Key completion criteria |
|---|---|---|
| Executive Control Tower | CEO, COO, CFO workspaces; business review; exceptions | Governed KPIs, targets, drill-through and action ownership |
| SKU & Product | SKU master, variants, packs, barcodes, tax, pricing, substitutions | Versioned master data and effective dates |
| Store & Network | stores, regions, territories, warehouses, calendars, operating hours | Valid hierarchy and scoped access |
| POS & Omnichannel Sales | billing, returns, discounts, tenders, day close, offline sync | Reliable invoice/payment/stock integration |
| Procurement | vendors, price books, RFQ/PO, shipment, GRN, supplier invoice, match | Partial receipts, split invoices, landed cost and approvals |
| Inventory & WMS | ledger, batches, FEFO, transfers, indents, counts, repacking | Single stock truth and traceable movement |
| Demand & Replenishment | forecast, safety stock, reorder, transfer recommendations | Backtesting, overrides, explainability and feedback |
| Wastage & Quality | expiry, damage, shrinkage, markdown, claims, evidence | Approval, disposition and inventory/finance integration |
| Finance & Tax | chart of accounts, journals, settlements, payables, GST, close | Reconciled subledgers and period controls |
| Customer & Loyalty | customer 360, consent, loyalty, campaigns, WhatsApp | POS-linked history and auditable consent |
| People & Workforce | employee master, roles, attendance/rosters, productivity | Role-scoped workflows and privacy controls |
| Platform & Governance | users, roles, approvals, audit, configuration, integrations | Least privilege, approval matrix and admin controls |

## 5. Executive workspaces — all three, advanced depth

All three are first-class role-specific views within the Control Tower. They share the same governed metric layer, date/filter context, hierarchy, targets and drill-through behavior.

### CEO — strategic performance

- Net revenue, growth, gross margin, contribution and EBITDA (only where accounting definitions/data support it).
- Like-for-like / same-store sales growth with a documented comparable-store cohort.
- Store productivity, revenue per store and per square foot where area data is validated.
- Category/brand mix, customer retention, loyalty contribution and cohort trends.
- Inventory turns, working capital, cash conversion and return on invested capital where data supports them.
- Store openings, closures, ramp-up and expansion pipeline.
- Strategic risks, concentration, forecast outlook and top decisions requiring intervention.

### COO — execution and operating health

- Sales vs target, store availability, fill rate and lost-sales proxies.
- Stockout rate, inventory accuracy, ageing, expiry exposure and shrinkage.
- Warehouse throughput, dispatch SLA, vendor OTIF and lead-time variability.
- Replenishment/transfer execution, store receiving and cycle-count health.
- Wastage by reason, category, supplier, region and store.
- POS uptime, offline backlog, sync lag, day-close completion and cash variance.
- Store productivity, operating compliance and unresolved operational exceptions.

### CFO — financial control and performance

- Net sales, gross profit, gross margin, operating expense and EBITDA bridge.
- Cash position, cash flow, working capital and cash conversion cycle.
- Inventory valuation and ageing exposure, AP ageing and supplier concentration.
- Store/region/category profitability with allocation assumptions made visible.
- Payment settlement variance, gateway/bank reconciliation and cash over/short.
- GST output/input-credit working, filing readiness and mismatch queues.
- Budget vs actual, forecast vs actual, period close checklist and audit exceptions.

### Shared executive analytics contract

Each KPI must have: business definition, formula, unit, currency/tax basis, source tables/events, aggregation grain, exclusions, owner, refresh SLA, target source, reconciliation test and effective date.

Dashboard capabilities: MTD/QTD/YTD/custom periods; previous-period and YoY comparisons; actual vs target; trend and variance bridge; region → city → store → category → SKU → transaction drill-through where meaningful; saved filters; export with access checks; exception assignment; weekly/monthly business review packs.

**Important:** EBITDA, same-store growth, store profitability, customer LTV/CAC, revenue per square foot and working-capital metrics must be marked unavailable or provisional until required inputs and definitions are approved. Do not fabricate missing values.

## 6. Identity, access and governance

- RBAC plus data scope: company, legal entity, region, city, store, warehouse and functional scope.
- Separate role permissions from data visibility. A user may have a capability but only within assigned scope.
- Enforce authorization server-side on every endpoint and background job.
- Support maker-checker approvals for sensitive operations: purchase orders, supplier changes, discounts, stock adjustments, refunds, journal postings, period locks and access administration.
- Append-only audit events for authentication, access changes, approvals, postings, exports and sensitive data access.
- Secrets only through environment/secret manager; rotate signing keys and provider credentials.
- Password hashing with current strong parameters; short-lived sessions/tokens, refresh rotation, revocation and rate limiting.
- PII minimization, consent history, retention policy and export/delete workflows appropriate to applicable law.
- No demo fallback secret or universal admin account in production configuration.

## 7. Cross-module contracts to prove

1. POS sale → invoice → tender → stock movement → customer transaction → finance posting → analytics.
2. POS return → refund/credit → stock disposition → journal reversal/adjustment → customer history.
3. Replenishment proposal → approval → transfer/PO → dispatch → receipt → ledger update → availability.
4. PO → partial GRN → landed cost → supplier invoice → three-way match → payable → payment → bank reconciliation.
5. Wastage report → evidence → approval → stock disposition → markdown/claim where relevant → finance impact.
6. Store day close → tender reconciliation → cash variance → journal → finance dashboard.
7. Consent change → messaging eligibility → campaign/WhatsApp event → delivery status → audit.
8. User/role/scope change → immediate authorization effect → audit event.

## 8. Delivery sequence

### Phase 0 — baseline and blueprint
- Replicate and verify source history/tree.
- Establish rebuild branch strategy and architecture/product decision records.
- Capture current routes, APIs, schema, workflows and known gaps.
- Define acceptance scenarios and baseline test results.

### Phase 1 — platform foundation
- PostgreSQL configuration and Alembic migration framework.
- Environment validation and secret management.
- Identity, RBAC, data scopes, audit and approval primitives.
- Typed API contracts, error model, pagination, request IDs and idempotency.
- CI checks, test fixtures, local development and deployment skeleton.

### Phase 2 — core data and transaction spine
- Store/network/SKU/vendor/customer/employee masters.
- Inventory ledger, sales/invoice/tender lifecycle and finance journal foundations.
- Outbox/event contracts and reconciliation checks.
- Import/export and data-quality framework.

### Phase 3 — operational execution
- POS/offline sync, procurement, WMS, transfers, replenishment, wastage/quality.
- Complete the operational cross-module journeys above.
- Add workforce workflows and operational approvals.

### Phase 4 — finance and customer completion
- Finance close, settlement/bank reconciliation, GST working and payables.
- CRM identity linkage, loyalty, consent and messaging provider adapters.
- Operational controls and audit readiness.

### Phase 5 — executive intelligence
- KPI dictionary and targets/budget model.
- CEO, COO and CFO advanced workspaces.
- Drill-through, variance drivers, exception ownership and business review mode.
- Reconciliation and metric certification.

### Phase 6 — production hardening
- Performance, concurrency, security and accessibility testing.
- Disaster recovery, backup restore, observability and runbooks.
- Data migration rehearsal, operational training and release gates.

Phases may overlap only when upstream contracts are stable. A phase is not complete because its UI exists; its business journey, authorization, data correctness and tests must pass.

## 9. Quality gates

- Unit tests for domain calculations and lifecycle transitions.
- API integration tests against PostgreSQL, not SQLite-only confidence.
- Contract tests for internal and external adapters.
- End-to-end tests for the eight cross-module journeys.
- Concurrency tests for stock, invoices, document numbering, payments and journal posting.
- Offline POS tests for duplicate sync, retry, partial failure and conflict recovery.
- Role/scope negative tests for every sensitive API family.
- Data reconciliation tests for sales, inventory, finance and executive KPIs.
- Accessibility, responsive and performance budgets for key workflows.
- Security checks for dependencies, secrets, authentication, authorization and common web vulnerabilities.

## 10. Working agreements

- Keep `main` releasable; work in short-lived feature branches and merge through reviewed pull requests.
- Never force-push the source repository. Do not modify `dfh-demo`.
- Every material design choice gets an ADR when it affects architecture, data or security.
- Every module change updates API/domain documentation and tests.
- Seed data is deterministic, clearly synthetic and separate from production migrations.
- Never claim a test, deployment, integration or reconciliation passed unless it was actually run and its result captured.
- Use small, reviewable commits with explicit scope.
- Do not introduce provider-specific integrations until credentials, sandbox behavior and failure modes are documented.

## 11. Immediate next actions

1. Record the exact source baseline and confirm the mirrored tree/commit history.
2. Inspect the copied application locally or through a complete file-level review; run baseline tests and document failures.
3. Create architecture decision records for PostgreSQL/migrations, frontend framework, authentication/session model, event/outbox design and analytics storage.
4. Establish CI and a repeatable local test environment.
5. Begin Phase 1 only after the baseline and migration strategy are understood.

## 12. Explicit non-goals for the first release

- Premature microservices.
- AI-generated actions that post money, stock or customer communications without approval.
- Unverified financial metrics presented as certified.
- Live tax, payment, messaging or commerce integrations without provider validation.
- Replacing every proven domain rule solely to achieve a new framework.
