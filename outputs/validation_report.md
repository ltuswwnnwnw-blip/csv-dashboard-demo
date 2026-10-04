# CSV validation and change report

**Synthetic, AI-assisted sales demonstration.** No real customer or sales records are represented.

## Run summary

- Source: source_sales.csv
- Source SHA-256: dbf9d551076c420da7533de16342a7ec7848d17019b438a20657b3ce13262330
- Source size: 5476 bytes
- Data rows read: 79
- Accepted rows: 72
- Quarantined rows: 7
- Cell trims: 19
- Header labels normalized: 0
- Accepted units: 252
- Accepted revenue: USD 4993.70

Every total is derived only from accepted rows. Source values are preserved in the original CSV and in each quarantine record.

## The five fixed rules

1. **Normalize and validate headers** — Trim outer whitespace and case-fold header labels. Reject duplicate, missing, or unknown labels; column order may vary.
2. **Trim cell whitespace** — Trim outer whitespace from every cell and record each changed cell. Keep the original source file unchanged.
3. **Validate required fields and types** — Require order_id, order_date, region, product, category, quantity, and unit_price. Dates must be YYYY-MM-DD, quantity a positive whole number, and unit_price a positive decimal with no more than two fractional digits. Invalid rows are quarantined.
4. **Quarantine duplicate order IDs** — Keep the first otherwise-valid occurrence of each order_id. Quarantine later otherwise-valid repeats; never merge or discard them silently.
5. **Derive line totals exactly** — Calculate line_total as quantity multiplied by unit_price using Decimal arithmetic. Do not round or fill missing inputs.

## Header changes

No header labels changed.

## Cell change log

- Record 2, product: " Orbit canvas tote " → "Orbit canvas tote"
- Record 5, order_id: " SYN-0005 " → "SYN-0005"
- Record 5, region: " North " → "North"
- Record 9, quantity: " 5 " → "5"
- Record 11, unit_price: " 22.00 " → "22.00"
- Record 14, order_id: " SYN-0014 " → "SYN-0014"
- Record 14, region: " South " → "South"
- Record 28, order_id: " SYN-0028 " → "SYN-0028"
- Record 28, region: " West " → "West"
- Record 32, product: " Cedar & Clay mug " → "Cedar & Clay mug"
- Record 41, order_id: " SYN-0041 " → "SYN-0041"
- Record 41, region: " North " → "North"
- Record 49, quantity: " 1 " → "1"
- Record 51, unit_price: " 8.40 " → "8.40"
- Record 56, order_id: " SYN-0056 " → "SYN-0056"
- Record 56, region: " West " → "West"
- Record 62, product: " Maple serving tray " → "Maple serving tray"
- Record 69, order_id: " SYN-0069 " → "SYN-0069"
- Record 69, region: " North " → "North"

## Quarantined rows

- Record 73 — Duplicate order_id: a prior otherwise-valid row was kept. Original cells: ["SYN-0001","2026-06-22","North","Cedar & Clay mug","Home goods","2","12.50","Intentional repeated ID for quarantine demo."]
- Record 74 — Missing required field: region. Original cells: ["SYN-BAD-01","2026-06-23","","Rêve planter","Home goods","1","24.00","Missing region; kept for review."]
- Record 75 — Invalid order_date: expected YYYY-MM-DD. Original cells: ["SYN-BAD-02","2026-13-24","South","Tidal notebook","Paper goods","1","8.40","Invalid date example."]
- Record 76 — Invalid quantity: enter a positive whole number. Original cells: ["SYN-BAD-03","2026-06-25","East","Orbit canvas tote","Carry","many","18.00","Invalid quantity example."]
- Record 77 — Invalid unit_price: enter a positive decimal with at most two fractional digits. Original cells: ["SYN-BAD-04","2026-06-26","West","Maple serving tray","Kitchen","1","NaN","Invalid price example."]
- Record 78 — Invalid unit_price: enter a positive decimal with at most two fractional digits. Original cells: ["SYN-BAD-05","2026-06-27","North","Slate pen set","Paper goods","1","14.601","Three decimal places are rejected."]
- Record 79 — Column count mismatch: expected 8, found 7. Original cells: ["SYN-BAD-06","2026-06-28","South","Finch steel bottle","Drinkware","1","22.00"]

## Arithmetic check

Accepted revenue in integer cents: 499370.
Dashboard and report totals are recomputed from the same accepted rows and exact decimal line totals.
