# CONTEXT (read first in a new session)

**Owner:** Akshay (Bengaluru). Python first. Serving notice at Uppercase and interviewing at Dry Fruit House (DFH).
**Goal:** Build an unsolicited demo (web apps, analytics, offline POS) to showcase in DFH round 2, likely with the CEO.
Deadline: about one week from 31 Oct 2026. The demo is branded as DFH's own and labeled as synthetic data.

**DFH as understood:** multi-store dry fruit and nut retailer (HSR Layout, Bengaluru HQ), 300+ stores (demo assumes 500),
~18-20 warehouses, old POS with weak API and report automation, no organized WMS, manual finance reconciliation,
Oracle Cloud ERP under discussion, website offers WhatsApp and call only. Imports from many vendors (many to many with SKUs,
cost varies by vendor and month). Return policy: 10 days, only if defective or damaged. No manufacturing.

**Decisions so far**
- Keep all 10 modules (Control Tower, SKU master, Procurement and logistics, Inventory OMS/WMS, Wastage, Demand, Finance, Customers/WhatsApp, HR basic, Platform) plus the offline-first POS. No module is cut. Build module by module, end to end, test, then move on.
- POS is mandatory: online hosted, offline storage, sync when online, to show it can be built in house.
- Lean OMS/WMS with procurement and logistics tracking (pre-sales) so finance can streamline tax filing; Oracle ERP seen as overkill now.
- GST and GSP integration on hold. Flat assumed rates, stated on screen (tax_assumption table).
- 6 months of history (May to Oct 2026). Scale to 24 months later if size allows (6 months is 22 MB Parquet, so 24 months is fine).
- Theme: 2 to 3 color tones, light on the eyes, glass effect slider, dark and light plus 3 to 4 tones, per-module themes later.
- Design reference: docs/skills/apple-design.md (response, springs, materials, typography).
- Writing style for Akshay: brutally honest, confident, humble, no em dashes, no arrows, not robotic, plain vocabulary.
- Token use: frugal. Work locally, report only on completion.

**Status:** foundation, app shell, SKU master, Control Tower (full), Modules 3, 4, 5, 6, 7, 8, 9 (People) and 10 (Platform), plus the offline-first Store POS, are built and tested (see UPDATES.md). Module 4 owns batches and stock; wastage, demand and POS read from or post to it. Module 7 runs on a modelled tender split because POS holds only demo bills. Module 8 sends and payments are simulated (no WhatsApp API or gateway). **Next:** close the POS-to-CRM customer transaction linkage and make synced bill-level sales available to analytics without double-counting the aggregate history. The Module 7 and Control Tower browser steps in scripts/smoke.py have not run: its first Control Tower assertion is stale (expects 5 KPI cards). `make smoke-crm` runs.

**Brand:** logo colors sampled as warm near-black (about #1A0E0E) and amber (about #E4A80C); DFH tone is the default theme.
**Bootstrap in a new session:** `git clone`, `make setup`, `make test`, `make run`. Read docs/UPDATES.md first.
