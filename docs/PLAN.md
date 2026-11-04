# PLAN

Full product plan: `docs/DFH_Product_Plan_v1.pdf` (25 pages; source `docs/build_book.py`).

## Build order (module by module, each end to end with tests)
0. Foundation: schema (95 tables), masters, 6-month seed, theme tokens. DONE.
0b. App shell, Platform thin slice (login, roles, scope, audit), Module 2 SKU master, Control Tower v0. DONE.
1. Module 2 SKU master (API, screens, import, price history).
2. Module 10 Platform, thin slice (login, roles, audit, approvals) because every module needs it.
3. Module 4 Inventory and warehouse (ledger, batches, FEFO, transfers, repack, indents, counts). DONE (browser smoke run in the merge session).
4. Module 3 Procurement and logistics (price book, landed cost, PO, shipment tracker, GRN, three-way match). DONE (GRN posts to the thin stock slice; Module 4 ledger takes over).
5. POS foundation (PWA, IndexedDB, sync with idempotency keys, invoice series per device, day close, returns within 10 days for defective or damaged). DONE (offline first; sales post to the Module 4 ledger).
6. Module 5 Wastage and shrink. DONE (built early on a thin batch and stock slice; Module 4 to take over batches).
7. Module 7 Finance and tax reconciliation (assumed GST rates, GSP on hold). DONE (browser smoke pending).
8. Module 1 Control Tower. DONE (browser smoke steps pending).
9. Module 6 Demand and replenishment. DONE (built early on a modelled stock position; Module 4 and Module 3 replace on hand and on order).
10. Module 8 Customers and WhatsApp. DONE (sends and payments simulated).
11. Module 9 HR basic. DONE.
12. Module 10 Platform operations (access administration, approval rules and decisions, audit viewer, device and import health, entities and FX). DONE.

## Next integration hardening
- DONE: link synced POS bills to CRM customer purchase history and expose bill-level sales, average bill value, SKU lines per bill, payment tenders and returns to analytics. POS transaction metrics remain separate from aggregate Parquet KPIs to prevent double-counting.
- NEXT: reconcile all operational stock movements to the inventory ledger and retire remaining snapshot-based stock positions where the ledger is authoritative.

## Definition of done per module
Tables seeded, API with tests, screens in all themes, edge cases listed in the PDF covered or listed as open, entry in UPDATES.md.

## Demo storyline (10 minutes, CEO)
Control Tower exceptions, drill into cash shortage and warehouse shrink, POS offline demo, procurement landed cost compare,
near-expiry action, finance close, WhatsApp order, ownership and pilot path.

## Planted findings (table demo_finding)
F1 high-shrink warehouse (Hyderabad), F2 vendor price creep, F3 Pune stockouts on almonds and cashews, F4 weekend cash shortage
(one Mumbai store), F5 three stores not syncing, F6 dried pineapple expiry in kiosks, F7 Diwali gift box component shortage,
F8 September input credit mismatch (Karnataka).
