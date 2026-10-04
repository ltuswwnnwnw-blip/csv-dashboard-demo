# Offline CSV dashboard demo

An AI-assisted, offline CSV-cleaning and sales-dashboard demonstration that uses synthetic data only. It is a portfolio sample, not client work. The pipeline uses Python's standard library; the generated HTML dashboard has no external dependencies or network requests.

## Run the synthetic example

From the project root, with Python 3 available:

```powershell
python tools/generate_sample.py
python pipeline.py
```

The first command creates the synthetic input. The second validates and processes it. The current generated sample contains 79 source rows: 72 accepted, 7 quarantined, and 19 trimmed cells. Accepted-row revenue is USD 4,993.70. These figures describe only the synthetic sample and are not client results.

Open `outputs/dashboard.html` directly in a browser. The dashboard works offline. The pipeline also writes:

- `outputs/cleaned_sales.csv`
- `outputs/quarantined_rows.csv`
- `outputs/validation_report.json`
- `outputs/validation_report.md`
- `CSV_Dashboard_Demo.zip` at the project root

## Input schema and limits

The reference input has these columns:

```text
order_id, order_date, region, product, category, quantity, unit_price, customer_note
```

`order_id`, `order_date`, `region`, `product`, `category`, `quantity`, and `unit_price` are required. `customer_note` is optional. Column order may vary. The pipeline trims outer whitespace and case-folds header labels; it rejects duplicate, missing, or unknown labels.

The input limit is 10,000,000 bytes (10 MB), 20,000 data rows, and 20 columns. Dates must use `YYYY-MM-DD`; quantity must be a positive whole number; unit price must be a positive decimal with no more than two fractional digits. The reference currency is USD.

## Cleaning and validation rules

The generated validation report lists these five rules and any affected rows:

1. **Normalize and validate headers:** trim outer whitespace and case-fold labels; reject duplicate, missing, or unknown headers.
2. **Trim cell whitespace:** trim outer whitespace from cells and record each changed cell; keep the source file unchanged.
3. **Validate required fields and types:** quarantine rows with missing or invalid required values.
4. **Quarantine duplicate order IDs:** keep the first otherwise-valid occurrence and quarantine later repeats.
5. **Derive line totals exactly:** multiply quantity by unit price with decimal arithmetic; do not round or fill missing inputs.

## Reproduce checks

The project tests use only the Python standard library:

```powershell
python -m unittest discover -s tests -v
```

## License

The MIT license in `LICENSE` applies to this synthetic-data demonstration repository. It does not apply to client data or client-specific work.
