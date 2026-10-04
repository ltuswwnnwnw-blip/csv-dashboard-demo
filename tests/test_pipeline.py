import base64
import csv
import hashlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

try:
    import pipeline as pipeline_module
except Exception as import_error:
    pipeline_module = None
    PIPELINE_IMPORT_ERROR = import_error
else:
    PIPELINE_IMPORT_ERROR = None

HEADERS = [
    "order_id", "order_date", "region", "product",
    "category", "quantity", "unit_price", "customer_note",
]


def csv_bytes(rows, headers=HEADERS):
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


class CsvPipelineContractTests(unittest.TestCase):
    def require_pipeline(self):
        self.assertIsNotNone(
            pipeline_module,
            f"pipeline.py must provide the CSV pipeline ({PIPELINE_IMPORT_ERROR})",
        )
        self.assertTrue(
            callable(getattr(pipeline_module, "clean_csv_bytes", None)),
            "pipeline.clean_csv_bytes must validate, clean, and reconcile CSV rows",
        )
        return pipeline_module

    def test_trims_cells_logs_each_edit_and_reconciles_decimal_totals(self):
        pipeline = self.require_pipeline()
        payload = csv_bytes([
            [" O-001 ", " 2026-01-04 ", " North ", " Cedar mug ", " Home ", " 2 ", " 12.50 ", ""],
            ["O-002", "2026-02-09", "South", "Notebook", "Paper", "3", "7.40", ' "lined", 5" '],
        ])
        result = pipeline.clean_csv_bytes(payload)

        self.assertEqual(len(result["clean_rows"]), 2)
        self.assertEqual(result["clean_rows"][0]["order_id"], "O-001")
        self.assertEqual(result["clean_rows"][0]["line_total"], "25.00")
        self.assertEqual(result["clean_rows"][1]["line_total"], "22.20")
        self.assertEqual(result["report"]["total_revenue_cents"], 4720)
        self.assertEqual(
            sum(int(row["line_total_cents"]) for row in result["dashboard_rows"]),
            result["report"]["total_revenue_cents"],
        )
        self.assertGreater(result["report"]["trimmed_cells"], 0)
        self.assertEqual(result["report"]["accepted_records"], 2)
        self.assertEqual(result["report"]["quarantined_records"], 0)

    def test_decimal_line_total_remains_exact_beyond_default_context_precision(self):
        pipeline = self.require_pipeline()
        result = pipeline.clean_csv_bytes(csv_bytes([
            ["O-LARGE", "2026-01-04", "North", "Large value", "Demo", "1", "12345678901234567890123456789.12", ""],
        ]))

        self.assertEqual(result["clean_rows"][0]["line_total"], "12345678901234567890123456789.12")
        self.assertEqual(result["report"]["total_revenue_cents"], 1234567890123456789012345678912)

    def test_quarantines_missing_bad_types_bad_width_and_duplicate_ids(self):
        pipeline = self.require_pipeline()
        payload = csv_bytes([
            ["O-001", "2026-01-04", "North", "Mug", "Home", "2", "12.50", ""],
            ["O-001", "2026-01-05", "North", "Plate", "Home", "1", "8.00", ""],
            ["O-003", "not-a-date", "East", "Pen", "Paper", "1", "2.00", ""],
            ["O-004", "2026-01-05", "", "Lamp", "Home", "bad", "NaN", ""],
        ]).decode("utf-8") + "O-005,2026-01-06,West,Book,Paper,1,4.00\n"
        result = pipeline.clean_csv_bytes(payload.encode("utf-8"))

        self.assertEqual([row["order_id"] for row in result["clean_rows"]], ["O-001"])
        self.assertEqual(result["report"]["quarantined_records"], 4)
        reasons = [item["reason"] for item in result["quarantined_rows"]]
        self.assertTrue(any("duplicate order_id" in reason.lower() for reason in reasons))
        self.assertTrue(any("order_date" in reason for reason in reasons))
        self.assertTrue(any("region" in reason for reason in reasons))
        self.assertTrue(any("quantity" in reason for reason in reasons))
        self.assertTrue(any("unit_price" in reason for reason in reasons))
        self.assertTrue(any("column" in reason.lower() for reason in reasons))
        self.assertEqual(result["report"]["total_revenue_cents"], 2500)

    def test_rejects_duplicate_headers_after_normalization(self):
        pipeline = self.require_pipeline()
        payload = csv_bytes(
            [["O-001", "2026-01-04", "North", "Mug", "Home", "1", "12.50", ""]],
            headers=["order_id", " Order_ID ", *HEADERS[1:]],
        )
        with self.assertRaises(ValueError):
            pipeline.clean_csv_bytes(payload)

    def test_rejects_invalid_utf8_and_schema_width_over_column_limit(self):
        pipeline = self.require_pipeline()
        with self.assertRaises(ValueError):
            pipeline.clean_csv_bytes(b"\xff\xfe\x80")
        twenty_one_columns = ",".join(f"field_{i}" for i in range(21)).encode("ascii")
        with self.assertRaises(ValueError):
            pipeline.clean_csv_bytes(twenty_one_columns + b"\n")

    def test_accepts_exact_byte_and_row_limits_and_rejects_one_over(self):
        pipeline = self.require_pipeline()
        max_bytes = pipeline.MAX_BYTES
        header = (",".join(HEADERS) + "\n").encode("utf-8")
        prefix = b"O-001,2026-01-04,North,Mug,Home,1,1.00,"
        note_size = max_bytes - len(header) - len(prefix) - 1
        exact_size = header + prefix + (b"x" * note_size) + b"\n"
        self.assertEqual(len(exact_size), max_bytes)
        self.assertEqual(pipeline.clean_csv_bytes(exact_size)["report"]["accepted_records"], 1)
        with self.assertRaises(ValueError):
            pipeline.clean_csv_bytes(exact_size + b"x")

        row_count = pipeline.MAX_ROWS
        out = io.StringIO(newline="")
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(HEADERS)
        for index in range(row_count):
            writer.writerow([
                f"O-{index:05}", "2026-01-04", "North", "Mug",
                "Home", "1", "1.00", "",
            ])
        at_limit = out.getvalue().encode("utf-8")
        self.assertEqual(
            pipeline.clean_csv_bytes(at_limit)["report"]["accepted_records"],
            row_count,
        )
        writer.writerow(["OVER", "2026-01-04", "North", "Mug", "Home", "1", "1.00", ""])
        with self.assertRaises(ValueError):
            pipeline.clean_csv_bytes(out.getvalue().encode("utf-8"))

    def test_dashboard_embeds_untrusted_text_safely_and_stays_offline(self):
        pipeline = self.require_pipeline()
        hostile = '</script><script>window.__csvDemoInjected = true</script>'
        payload = csv_bytes([
            ["O-HTML", "2026-03-12", "Québec", hostile, 'Home "A"\u2028line\u2029separator', "1", "3.25", 'Unicode 雪 & "quotes"'],
        ])
        result = pipeline.clean_csv_bytes(payload)
        html = pipeline.build_dashboard_html(result)

        self.assertNotIn(hostile, html)
        self.assertIn(".textContent", html)
        self.assertNotIn("innerHTML", html)
        self.assertNotRegex(html, re.compile(r"<script\s+src=", re.I))
        self.assertNotRegex(html, re.compile(r"https?://", re.I))
        match = re.search(r'const DASHBOARD_DATA_B64 = "([A-Za-z0-9+/=]+)"', html)
        self.assertIsNotNone(match)
        embedded = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
        self.assertEqual(embedded[0]["product"], hostile)
        self.assertEqual(embedded[0]["region"], "Québec")
        self.assertEqual(embedded[0]["category"], 'Home "A"\u2028line\u2029separator')

    def test_pipeline_preserves_source_and_writes_the_change_report(self):
        pipeline = self.require_pipeline()
        payload = csv_bytes([
            [" O-PRESERVE ", "2026-04-02", "North", "Cedar & Clay", "Home", "2", "8.25", ""],
        ])
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            source_path = folder / "source.csv"
            output_path = folder / "outputs"
            source_path.write_bytes(payload)
            before_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()

            result = pipeline.run_pipeline(source_path, output_path)

            self.assertEqual(source_path.read_bytes(), payload)
            self.assertEqual(result["report"]["source_sha256"], before_hash)
            report = json.loads((output_path / "validation_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["trimmed_cells"], 1)
            self.assertEqual(report["change_log"][0]["field"], "order_id")
            for name in (
                "cleaned_sales.csv",
                "quarantined_rows.csv",
                "validation_report.json",
                "validation_report.md",
                "dashboard.html",
            ):
                self.assertTrue((output_path / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
