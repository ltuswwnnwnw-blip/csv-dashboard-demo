#!/usr/bin/env python3
"""Build a reproducible, offline sales CSV cleanup demo using only Python's stdlib."""

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import sys
import zipfile
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "rules.json").read_text(encoding="utf-8"))
EXPECTED_HEADERS = tuple(CONFIG["headers"])
REQUIRED_FIELDS = tuple(CONFIG["required_fields"])
MAX_BYTES = int(CONFIG["max_input_bytes"])
MAX_ROWS = int(CONFIG["max_data_rows"])
MAX_COLUMNS = int(CONFIG["max_columns"])
CURRENCY = CONFIG["currency"]
RULES = CONFIG["rules"]
OUTPUT_HEADERS = EXPECTED_HEADERS + ("line_total",)


class InputRejected(ValueError):
    """Raised when the file itself cannot be safely interpreted as the reference CSV."""


def _money(cents):
    whole, fraction = divmod(cents, 100)
    return "{}.{:02d}".format(whole, fraction)


def _record_change(changes, row_number, field, before, after):
    changes.append(
        {
            "record_number": row_number,
            "field": field,
            "before": before,
            "after": after,
        }
    )


def clean_csv_bytes(source_bytes, source_name="source_sales.csv"):
    """Validate a UTF-8 reference CSV and return cleaned, quarantined, and dashboard rows."""
    if not isinstance(source_bytes, (bytes, bytearray)):
        raise TypeError("source_bytes must be bytes.")
    source_bytes = bytes(source_bytes)
    if len(source_bytes) > MAX_BYTES:
        raise InputRejected("Input exceeds the 10 MB (10,000,000 byte) file limit.")
    if not source_bytes:
        raise InputRejected("Input is empty; a CSV header row is required.")
    try:
        source_text = source_bytes.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise InputRejected("Input must be valid UTF-8, with or without a BOM.") from exc

    old_field_limit = csv.field_size_limit()
    csv.field_size_limit(MAX_BYTES)
    try:
        reader = csv.reader(io.StringIO(source_text, newline=""), strict=True)
        try:
            raw_headers = next(reader)
        except StopIteration as exc:
            raise InputRejected("Input has no CSV header row.") from exc
        except csv.Error as exc:
            raise InputRejected("The CSV header is malformed.") from exc

        if not raw_headers:
            raise InputRejected("Input has an empty CSV header row.")
        if len(raw_headers) > MAX_COLUMNS:
            raise InputRejected("Input exceeds the 20-column limit.")

        headers = [header.strip().casefold() for header in raw_headers]
        duplicates = sorted(name for name, count in Counter(headers).items() if count > 1)
        if duplicates:
            raise InputRejected("Duplicate headers after normalization: " + ", ".join(duplicates))
        expected = set(EXPECTED_HEADERS)
        missing_headers = sorted(expected.difference(headers))
        unknown_headers = sorted(set(headers).difference(expected))
        if missing_headers or unknown_headers:
            message = []
            if missing_headers:
                message.append("missing " + ", ".join(missing_headers))
            if unknown_headers:
                message.append("unknown " + ", ".join(unknown_headers))
            raise InputRejected("Header does not match the reference schema (" + "; ".join(message) + ").")

        changes = []
        header_changes = []
        for position, (before, after) in enumerate(zip(raw_headers, headers), start=1):
            if before != after:
                item = {
                    "column_number": position,
                    "before": before,
                    "after": after,
                }
                header_changes.append(item)
                _record_change(changes, 0, "header column {}".format(position), before, after)

        accepted = []
        quarantined = []
        seen_order_ids = set()
        row_count = 0

        while True:
            try:
                source_row = next(reader)
            except StopIteration:
                break
            except csv.Error as exc:
                raise InputRejected("Malformed CSV near record {}: {}.".format(row_count + 1, exc)) from exc

            row_count += 1
            if row_count > MAX_ROWS:
                raise InputRejected("Input exceeds the 20,000 data-row limit.")
            if len(source_row) > MAX_COLUMNS:
                raise InputRejected("A data row exceeds the 20-column limit.")

            if len(source_row) != len(headers):
                quarantined.append(
                    {
                        "record_number": row_count,
                        "reason": "Column count mismatch: expected {}, found {}.".format(
                            len(headers), len(source_row)
                        ),
                        "source_row": source_row,
                    }
                )
                continue

            source_by_header = dict(zip(headers, source_row))
            trimmed = {}
            for header in headers:
                before = source_by_header[header]
                after = before.strip()
                trimmed[header] = after
                if before != after:
                    _record_change(changes, row_count, header, before, after)
            normalized = {header: trimmed[header] for header in EXPECTED_HEADERS}

            issues = []
            for field in REQUIRED_FIELDS:
                if normalized[field] == "":
                    issues.append("Missing required field: {}.".format(field))

            parsed_date = None
            if normalized["order_date"]:
                try:
                    parsed_date = date.fromisoformat(normalized["order_date"])
                    if parsed_date.isoformat() != normalized["order_date"]:
                        parsed_date = None
                        issues.append("Invalid order_date: expected YYYY-MM-DD.")
                except ValueError:
                    issues.append("Invalid order_date: expected YYYY-MM-DD.")

            parsed_quantity = None
            if normalized["quantity"]:
                if not re.fullmatch(r"[0-9]+", normalized["quantity"]):
                    issues.append("Invalid quantity: enter a positive whole number.")
                else:
                    try:
                        parsed_quantity = int(normalized["quantity"])
                        if parsed_quantity <= 0:
                            parsed_quantity = None
                            issues.append("Invalid quantity: enter a positive whole number.")
                    except ValueError:
                        issues.append("Invalid quantity: enter a positive whole number.")

            parsed_price = None
            price_cents = None
            if normalized["unit_price"]:
                price_text = normalized["unit_price"]
                if not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]{1,2})?|\.[0-9]{1,2})", price_text):
                    issues.append("Invalid unit_price: enter a positive decimal with at most two fractional digits.")
                else:
                    try:
                        parsed_price = Decimal(price_text)
                        if not parsed_price.is_finite() or parsed_price <= 0:
                            parsed_price = None
                            issues.append("Invalid unit_price: enter a positive decimal with at most two fractional digits.")
                        else:
                            with localcontext() as context:
                                context.prec = max(28, len(parsed_price.as_tuple().digits) + 2)
                                price_cents = int(parsed_price * Decimal(100))
                    except (InvalidOperation, ValueError, OverflowError):
                        parsed_price = None
                        issues.append("Invalid unit_price: enter a positive decimal with at most two fractional digits.")

            order_id = normalized["order_id"]
            if issues:
                quarantined.append(
                    {
                        "record_number": row_count,
                        "reason": " ".join(issues),
                        "source_row": source_row,
                    }
                )
                continue

            if order_id in seen_order_ids:
                quarantined.append(
                    {
                        "record_number": row_count,
                        "reason": "Duplicate order_id: a prior otherwise-valid row was kept.",
                        "source_row": source_row,
                    }
                )
                continue
            seen_order_ids.add(order_id)

            line_total_cents = parsed_quantity * price_cents
            clean_row = dict(normalized)
            clean_row["line_total"] = _money(line_total_cents)
            clean_row["line_total_cents"] = line_total_cents
            clean_row["quantity_value"] = parsed_quantity
            clean_row["unit_price_decimal"] = str(parsed_price)
            clean_row["month"] = parsed_date.strftime("%Y-%m")
            clean_row["record_number"] = row_count
            accepted.append(clean_row)
    except csv.Error as exc:
        raise InputRejected("Malformed CSV: {}.".format(exc)) from exc
    finally:
        csv.field_size_limit(old_field_limit)

    total_revenue_cents = sum(row["line_total_cents"] for row in accepted)
    total_units = sum(row["quantity_value"] for row in accepted)
    reason_counts = Counter(item["reason"] for item in quarantined)
    report = {
        "report_version": "1",
        "schema_name": CONFIG["schema_name"],
        "source_name": Path(source_name).name,
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "source_size_bytes": len(source_bytes),
        "source_records": row_count,
        "accepted_records": len(accepted),
        "quarantined_records": len(quarantined),
        "header_changes": header_changes,
        "header_changes_count": len(header_changes),
        "trimmed_cells": sum(1 for item in changes if item["record_number"] > 0),
        "change_log": changes,
        "quarantine_reasons": dict(sorted(reason_counts.items())),
        "total_units": total_units,
        "total_revenue_cents": total_revenue_cents,
        "total_revenue": _money(total_revenue_cents),
        "currency": CURRENCY,
        "rules": RULES,
        "limits": {
            "max_input_bytes": MAX_BYTES,
            "max_data_rows": MAX_ROWS,
            "max_columns": MAX_COLUMNS,
            "reference_schema_columns": len(EXPECTED_HEADERS),
        },
    }

    dashboard_rows = [
        {
            "order_id": row["order_id"],
            "order_date": row["order_date"],
            "month": row["month"],
            "region": row["region"],
            "product": row["product"],
            "category": row["category"],
            "quantity": str(row["quantity_value"]),
            "line_total_cents": str(row["line_total_cents"]),
        }
        for row in accepted
    ]
    return {
        "clean_rows": accepted,
        "quarantined_rows": quarantined,
        "dashboard_rows": dashboard_rows,
        "report": report,
    }


def build_dashboard_html(result):
    rows_json = json.dumps(
        result["dashboard_rows"], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    report = result["report"]
    report_data = {
        "source_records": report["source_records"],
        "accepted_records": report["accepted_records"],
        "quarantined_records": report["quarantined_records"],
        "trimmed_cells": report["trimmed_cells"],
        "header_changes_count": report["header_changes_count"],
        "quarantine_reasons": report["quarantine_reasons"],
        "source_sha256": report["source_sha256"],
        "rules": report["rules"],
    }
    report_json = json.dumps(
        report_data, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    rows_b64 = base64.b64encode(rows_json).decode("ascii")
    report_b64 = base64.b64encode(report_json).decode("ascii")

    template = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>Sales dashboard · CSV demo</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #173448;
      --muted: #4c6475;
      --surface: #f2f5f7;
      --paper: #ffffff;
      --line: #c5d0d8;
      --accent: #006b70;
      --accent-soft: #dceeee;
      --navy: #173b50;
      --amber: #f1bd78;
      --good: #205f4d;
      --bad: #863f32;
      --radius: 10px;
    }
    *, *::before, *::after { box-sizing: border-box; }
    html { background: var(--surface); }
    body {
      margin: 0;
      color: var(--ink);
      background: var(--surface);
      font-family: "Segoe UI", Arial, sans-serif;
      font-size: 14px;
      line-height: 1.5;
    }
    a { color: #005b62; text-underline-offset: 3px; }
    a:focus-visible, button:focus-visible, input:focus-visible, select:focus-visible {
      outline: 3px solid #9a4c1d;
      outline-offset: 3px;
    }
    .skip-link {
      position: absolute;
      top: 8px;
      left: 8px;
      z-index: 5;
      padding: 8px 12px;
      transform: translateY(-160%);
      color: #fff;
      background: var(--navy);
      border-radius: 4px;
    }
    .skip-link:focus { transform: translateY(0); }
    .app-shell {
      display: grid;
      grid-template-columns: 248px minmax(0, 1fr);
      gap: 24px;
      width: min(100% - 40px, 1320px);
      margin: 20px auto;
      align-items: start;
    }
    .source-rail {
      position: relative;
      padding: 22px 18px 20px;
      color: #f5f8fa;
      background: var(--navy);
      border-radius: var(--radius);
      overflow: hidden;
    }
    .rail-brand {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 24px;
      font-size: 12px;
      font-weight: 800;
      letter-spacing: .09em;
    }
    .brand-mark {
      display: inline-grid;
      width: 36px;
      height: 36px;
      place-items: center;
      color: var(--navy);
      background: var(--amber);
      border-radius: 6px;
      font-size: 11px;
      letter-spacing: .02em;
    }
    .rail-kicker, .eyebrow {
      margin: 0 0 6px;
      font-size: 11px;
      font-weight: 750;
      letter-spacing: .1em;
      text-transform: uppercase;
    }
    .rail-kicker { color: #cfdae0; }
    .rail-title {
      margin: 0 0 17px;
      font-size: 19px;
      line-height: 1.2;
      letter-spacing: -.02em;
    }
    .step-list {
      position: relative;
      display: grid;
      gap: 18px;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .step-list::before {
      position: absolute;
      top: 14px;
      bottom: 14px;
      left: 14px;
      width: 1px;
      background: #728895;
      content: "";
    }
    .step-list li {
      position: relative;
      display: grid;
      grid-template-columns: 30px minmax(0, 1fr);
      gap: 10px;
      min-width: 0;
      align-items: start;
    }
    .step-number {
      z-index: 1;
      display: grid;
      width: 29px;
      height: 29px;
      place-items: center;
      color: #132f41;
      background: var(--amber);
      border-radius: 50%;
      font-size: 10px;
      font-weight: 800;
    }
    .step-copy strong, .step-copy small { display: block; }
    .step-copy strong { font-size: 13px; }
    .step-copy small {
      margin-top: 2px;
      color: #d1dce2;
      font-size: 11px;
      line-height: 1.4;
      overflow-wrap: anywhere;
    }
    .rail-note {
      margin: 22px 0 18px;
      padding-top: 15px;
      color: #e1e8ec;
      border-top: 1px solid #587080;
      font-size: 12px;
    }
    .rail-links { display: grid; gap: 8px; }
    .rail-links a {
      display: block;
      padding: 8px 10px;
      color: #f5f8fa;
      border: 1px solid #8da0ac;
      border-radius: 6px;
      font-size: 12px;
      font-weight: 650;
      text-decoration: none;
    }
    .rail-links a:hover { background: #24495f; }
    main { min-width: 0; }
    .page-header {
      display: flex;
      min-width: 0;
      justify-content: space-between;
      gap: 20px;
      align-items: end;
      margin: 3px 0 19px;
    }
    .eyebrow { color: var(--accent); }
    h1 {
      margin: 0;
      font-size: clamp(26px, 3vw, 36px);
      line-height: 1.12;
      letter-spacing: -.035em;
    }
    .intro {
      max-width: 660px;
      margin: 9px 0 0;
      color: var(--muted);
      font-size: 14px;
    }
    .sample-note {
      display: inline-flex;
      flex: 0 0 auto;
      gap: 7px;
      align-items: center;
      margin-top: 12px;
      padding: 5px 9px;
      color: #174a4e;
      background: var(--accent-soft);
      border: 1px solid #9fc9c8;
      border-radius: 99px;
      font-size: 11px;
      font-weight: 700;
    }
    .sample-dot {
      width: 7px;
      height: 7px;
      background: var(--accent);
      border-radius: 50%;
    }
    .panel {
      min-width: 0;
      padding: 17px;
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: var(--radius);
    }
    .panel-heading {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: baseline;
      margin-bottom: 12px;
    }
    .panel-heading h2, .section-title {
      margin: 0;
      font-size: 15px;
      line-height: 1.3;
    }
    .panel-heading p {
      margin: 0;
      color: var(--muted);
      font-size: 11px;
    }
    .filters {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr)) auto;
      gap: 12px;
      align-items: end;
    }
    .filter-field { min-width: 0; }
    label {
      display: block;
      margin-bottom: 5px;
      font-size: 12px;
      font-weight: 700;
    }
    input, select, button {
      width: 100%;
      min-width: 0;
      max-width: 100%;
      min-height: 40px;
      padding: 8px 10px;
      color: var(--ink);
      background: #fff;
      border: 1px solid #869aa8;
      border-radius: 6px;
      font: inherit;
    }
    select { text-overflow: ellipsis; }
    button {
      width: auto;
      cursor: pointer;
      color: #fff;
      background: var(--accent);
      border-color: var(--accent);
      font-size: 12px;
      font-weight: 700;
    }
    button:hover { background: #00575c; }
    .filter-help {
      margin: 10px 0 0;
      color: var(--muted);
      font-size: 11px;
    }
    .metrics {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin: 14px 0;
    }
    .metric {
      min-width: 0;
      padding: 14px 16px;
      background: #fff;
      border: 1px solid var(--line);
      border-left: 4px solid var(--accent);
      border-radius: 7px;
    }
    .metric-label {
      display: block;
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
    }
    .metric-value {
      display: block;
      margin-top: 4px;
      font-size: 23px;
      font-weight: 760;
      line-height: 1.2;
      letter-spacing: -.025em;
      font-variant-numeric: tabular-nums;
      overflow-wrap: anywhere;
    }
    .charts {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    .chart-panel { min-height: 230px; }
    .chart-panel:first-child { grid-column: 1 / -1; }
    .chart-rows {
      display: grid;
      gap: 9px;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .chart-rows li {
      display: grid;
      grid-template-columns: minmax(78px, 1.1fr) minmax(48px, 2fr) minmax(72px, auto);
      gap: 10px;
      min-width: 0;
      align-items: center;
    }
    .chart-label, .chart-value {
      min-width: 0;
      font-size: 11px;
      overflow-wrap: anywhere;
    }
    .chart-value {
      text-align: right;
      font-variant-numeric: tabular-nums;
      font-weight: 650;
    }
    .bar-track {
      width: 100%;
      height: 10px;
      overflow: hidden;
      background: #e2e9ed;
      border-radius: 8px;
    }
    .bar-fill {
      display: block;
      height: 100%;
      min-width: 2px;
      background: var(--accent);
      border-radius: inherit;
    }
    .chart-empty {
      margin: 10px 0 0;
      padding: 15px 12px;
      color: var(--muted);
      background: #f4f7f8;
      border: 1px dashed #869aa8;
      border-radius: 6px;
      font-size: 12px;
    }
    .section-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: baseline;
      margin: 22px 0 10px;
    }
    .section-head p {
      margin: 0;
      color: var(--muted);
      font-size: 11px;
      text-align: right;
    }
    .table-wrap {
      overflow-x: auto;
      background: #fff;
      border: 1px solid var(--line);
      border-radius: var(--radius);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
    }
    th, td {
      padding: 10px 9px;
      text-align: left;
      border-bottom: 1px solid #d8e0e5;
      vertical-align: top;
    }
    th {
      color: #334f61;
      background: #eaf0f3;
      font-size: 10px;
      letter-spacing: .04em;
      text-transform: uppercase;
    }
    td { overflow-wrap: anywhere; }
    tbody tr:last-child td { border-bottom: 0; }
    td:last-child, th:last-child { text-align: right; font-variant-numeric: tabular-nums; }
    .mobile-records { display: none; }
    .record-card {
      padding: 12px;
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 7px;
    }
    .record-card h3 { margin: 0 0 3px; font-size: 14px; overflow-wrap: anywhere; }
    .record-meta { margin: 0 0 9px; color: var(--muted); font-size: 11px; overflow-wrap: anywhere; }
    .record-detail {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      padding-top: 6px;
      color: var(--muted);
      border-top: 1px solid #e0e6ea;
      font-size: 11px;
    }
    .record-detail strong { color: var(--ink); text-align: right; overflow-wrap: anywhere; }
    .empty-records {
      margin: 0;
      padding: 15px;
      color: var(--muted);
      background: #fff;
      border: 1px dashed #869aa8;
      border-radius: 7px;
    }
    .report-panel { margin-top: 16px; }
    .report-panel summary {
      cursor: pointer;
      font-size: 13px;
      font-weight: 750;
    }
    .report-summary { margin: 12px 0 0; color: var(--muted); font-size: 12px; }
    .report-rule-list { display: grid; gap: 8px; margin: 12px 0 0; padding-left: 20px; }
    .report-rule-list li { font-size: 12px; }
    .report-rule-list strong { color: var(--ink); }
    .report-link {
      display: inline-block;
      margin-top: 12px;
      font-size: 12px;
      font-weight: 700;
    }
    .status-line {
      min-height: 18px;
      margin: 8px 0 0;
      color: var(--muted);
      font-size: 11px;
    }
    .page-footer {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      margin: 16px 0 8px;
      color: var(--muted);
      font-size: 11px;
    }
    .offline-mark { color: var(--good); font-weight: 750; }
    @media (max-width: 900px) {
      .app-shell { grid-template-columns: 210px minmax(0, 1fr); gap: 16px; width: min(100% - 32px, 1320px); }
      .source-rail { padding: 18px 14px; }
      .filters { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .filters button { width: 100%; }
    }
    @media (max-width: 680px) {
      .app-shell {
        display: block;
        width: auto;
        margin: 12px 16px;
      }
      .source-rail { margin-bottom: 16px; padding: 15px; }
      .rail-brand { margin-bottom: 13px; }
      .rail-title { margin-bottom: 12px; font-size: 17px; }
      .step-list { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 7px; }
      .step-list::before {
        top: 14px;
        right: 18%;
        bottom: auto;
        left: 18%;
        width: auto;
        height: 1px;
      }
      .step-list li { display: block; }
      .step-number { margin-bottom: 5px; }
      .step-copy strong { font-size: 11px; }
      .step-copy small { font-size: 10px; }
      .rail-note { margin: 12px 0; padding-top: 10px; }
      .rail-links { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .rail-links a { padding: 8px; }
      .page-header { display: block; margin-top: 0; }
      .intro { font-size: 13px; }
      .filters { grid-template-columns: minmax(0, 1fr); gap: 9px; }
      .metrics { gap: 7px; margin: 10px 0; }
      .metric { padding: 11px 9px; }
      .metric-label { font-size: 10px; }
      .metric-value { font-size: 17px; }
      .charts { grid-template-columns: minmax(0, 1fr); }
      .chart-panel:first-child { grid-column: auto; }
      .chart-panel { min-height: 0; padding: 14px; }
      .panel-heading { align-items: start; }
      .panel-heading p { max-width: 44%; text-align: right; }
      .table-wrap { display: none; }
      .mobile-records { display: grid; gap: 8px; }
      .section-head { align-items: start; }
      .section-head p { max-width: 48%; }
      .page-footer { display: block; }
      .page-footer span { display: block; margin-top: 3px; }
    }
    @media (max-width: 360px) {
      .app-shell { margin-inline: 16px; }
      .step-copy small { overflow-wrap: anywhere; }
      .metric-value { font-size: 15px; }
      .chart-rows li { grid-template-columns: minmax(60px, 1fr) minmax(38px, 1.2fr) minmax(60px, auto); gap: 6px; }
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { scroll-behavior: auto !important; transition-duration: .01ms !important; }
    }
  </style>
</head>
<body>
  <a class="skip-link" href="#main-content">Skip to sales data</a>
  <div class="app-shell">
    <aside class="source-rail" aria-labelledby="rail-heading">
      <div class="rail-brand"><span class="brand-mark" aria-hidden="true">CSV</span><span>ROW / REVIEW</span></div>
      <p class="rail-kicker">Source trail</p>
      <h2 class="rail-title" id="rail-heading">Every output has a path back.</h2>
      <ol class="step-list">
        <li><span class="step-number" aria-hidden="true">01</span><span class="step-copy"><strong>Source</strong><small>data/source_sales.csv</small></span></li>
        <li><span class="step-number" aria-hidden="true">02</span><span class="step-copy"><strong>Five rules</strong><small>trim · validate · explain</small></span></li>
        <li><span class="step-number" aria-hidden="true">03</span><span class="step-copy"><strong>Review</strong><small>clean rows + quarantine</small></span></li>
      </ol>
      <p class="rail-note">Rejected rows keep their source values and record number. No values are guessed or filled in.</p>
      <nav class="rail-links" aria-label="Demo files">
        <a href="cleaned_sales.csv" download>Download cleaned CSV</a>
        <a href="quarantined_rows.csv" download>Download quarantined rows</a>
        <a href="../data/source_sales.csv" download>Download original CSV</a>
        <a href="validation_report.md">Read full validation report</a>
      </nav>
    </aside>
    <main id="main-content">
      <header class="page-header">
        <div>
          <p class="eyebrow">Local CSV demonstration</p>
          <h1>Sales, with a paper trail.</h1>
          <p class="intro">A filterable view of fictional sales rows. The table, charts, and totals use accepted rows from the cleaned CSV.</p>
          <span class="sample-note"><span class="sample-dot" aria-hidden="true"></span>Synthetic data · AI-assisted · USD</span>
        </div>
      </header>
      <section class="panel" aria-labelledby="filter-heading">
        <div class="panel-heading">
          <h2 id="filter-heading">Find matching sales</h2>
          <p>Filters update all three charts and the row list.</p>
        </div>
        <div class="filters">
          <div class="filter-field">
            <label for="region-filter">Region</label>
            <select id="region-filter"><option value="">All regions</option></select>
          </div>
          <div class="filter-field">
            <label for="category-filter">Category</label>
            <select id="category-filter"><option value="">All categories</option></select>
          </div>
          <div class="filter-field">
            <label for="product-filter">Product contains</label>
            <input id="product-filter" type="search" autocomplete="off" placeholder="Search product names">
          </div>
          <button id="clear-filters" type="button">Clear filters</button>
        </div>
        <p class="filter-help">Search uses the product name. It does not change or rewrite any source data.</p>
        <p class="status-line" id="filter-status" role="status" aria-live="polite"></p>
      </section>
      <section class="metrics" aria-label="Three demo sales metrics">
        <article class="metric"><span class="metric-label">Demo revenue</span><strong class="metric-value" id="metric-revenue">$0.00</strong></article>
        <article class="metric"><span class="metric-label">Accepted orders</span><strong class="metric-value" id="metric-orders">0</strong></article>
        <article class="metric"><span class="metric-label">Units sold</span><strong class="metric-value" id="metric-units">0</strong></article>
      </section>
      <section class="charts" aria-label="Three charts for synthetic sales data">
        <article class="panel chart-panel">
          <div class="panel-heading"><h2>Revenue by month</h2><p>Accepted line totals</p></div>
          <div id="chart-month" role="img" aria-label="Monthly revenue chart"></div>
        </article>
        <article class="panel chart-panel">
          <div class="panel-heading"><h2>Revenue by region</h2><p>Accepted line totals</p></div>
          <div id="chart-region" role="img" aria-label="Revenue by region chart"></div>
        </article>
        <article class="panel chart-panel">
          <div class="panel-heading"><h2>Top products</h2><p>Up to eight by revenue</p></div>
          <div id="chart-product" role="img" aria-label="Top products by revenue chart"></div>
        </article>
      </section>
      <section aria-labelledby="orders-heading">
        <div class="section-head">
          <h2 class="section-title" id="orders-heading">Accepted order rows</h2>
          <p id="rows-count">Showing 0 rows</p>
        </div>
        <div class="table-wrap">
          <table>
            <caption class="visually-hidden">The first ten accepted rows matching the current filters</caption>
            <thead><tr><th scope="col">Order</th><th scope="col">Date</th><th scope="col">Product</th><th scope="col">Region</th><th scope="col">Category</th><th scope="col">Units</th><th scope="col">Revenue</th></tr></thead>
            <tbody id="orders-body"></tbody>
          </table>
        </div>
        <div class="mobile-records" id="mobile-records" aria-label="Matching accepted order rows"></div>
      </section>
      <details class="panel report-panel">
        <summary>What the pipeline changed and checked</summary>
        <div id="report-summary" class="report-summary"></div>
        <ol id="report-rules" class="report-rule-list"></ol>
        <a class="report-link" href="validation_report.md">Open the complete row-by-row validation and change report</a>
      </details>
      <footer class="page-footer">
        <span class="offline-mark">Offline view · calculations run in this browser</span>
        <span>Illustrative dataset; no real customers or sales are represented.</span>
      </footer>
    </main>
  </div>
  <script>
    const DASHBOARD_DATA_B64 = "__ROWS_B64__";
    const DASHBOARD_REPORT_B64 = "__REPORT_B64__";
    const decode = function(encoded) {
      const binary = atob(encoded);
      const bytes = Uint8Array.from(binary, function(character) { return character.charCodeAt(0); });
      return JSON.parse(new TextDecoder("utf-8").decode(bytes));
    };
    const DATA = decode(DASHBOARD_DATA_B64);
    const REPORT = decode(DASHBOARD_REPORT_B64);
    const byId = function(id) { return document.getElementById(id); };
    const formatInteger = function(value) {
      return value.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    };
    const formatMoney = function(cents) {
      const negative = cents < 0n;
      const absolute = negative ? -cents : cents;
      const dollars = formatInteger(absolute / 100n);
      const fraction = (absolute % 100n).toString().padStart(2, "0");
      return (negative ? "-$" : "$") + dollars + "." + fraction;
    };
    const setText = function(element, value) { element.textContent = String(value); };
    const addOptions = function(select, values) {
      Array.from(new Set(values)).sort(function(a, b) { return a.localeCompare(b); }).forEach(function(value) {
        select.add(new Option(value, value));
      });
    };
    addOptions(byId("region-filter"), DATA.map(function(row) { return row.region; }));
    addOptions(byId("category-filter"), DATA.map(function(row) { return row.category; }));
    const matchingRows = function() {
      const region = byId("region-filter").value;
      const category = byId("category-filter").value;
      const product = byId("product-filter").value.trim().toLocaleLowerCase();
      return DATA.filter(function(row) {
        return (!region || row.region === region)
          && (!category || row.category === category)
          && (!product || row.product.toLocaleLowerCase().includes(product));
      });
    };
    const aggregate = function(rows, key) {
      const totals = new Map();
      rows.forEach(function(row) {
        const label = row[key];
        const value = BigInt(row.line_total_cents);
        totals.set(label, (totals.get(label) || 0n) + value);
      });
      return Array.from(totals, function(pair) { return { label: pair[0], cents: pair[1] }; })
        .sort(function(a, b) { return a.label.localeCompare(b.label); });
    };
    const renderBars = function(targetId, entries, emptyText) {
      const target = byId(targetId);
      while (target.firstChild) target.removeChild(target.firstChild);
      if (!entries.length) {
        const empty = document.createElement("p");
        empty.className = "chart-empty";
        empty.textContent = emptyText;
        target.appendChild(empty);
        return;
      }
      const max = entries.reduce(function(value, item) { return item.cents > value ? item.cents : value; }, 0n);
      const list = document.createElement("ol");
      list.className = "chart-rows";
      entries.forEach(function(item) {
        const line = document.createElement("li");
        const label = document.createElement("span");
        label.className = "chart-label";
        label.textContent = item.label;
        const track = document.createElement("span");
        track.className = "bar-track";
        track.setAttribute("aria-hidden", "true");
        const fill = document.createElement("span");
        fill.className = "bar-fill";
        fill.style.width = Number((item.cents * 1000n) / max) / 10 + "%";
        track.appendChild(fill);
        const value = document.createElement("span");
        value.className = "chart-value";
        value.textContent = formatMoney(item.cents);
        line.append(label, track, value);
        list.appendChild(line);
      });
      target.appendChild(list);
    };
    const appendCell = function(row, value) {
      const cell = document.createElement("td");
      cell.textContent = String(value);
      row.appendChild(cell);
    };
    const renderRows = function(rows) {
      const shown = rows.slice(0, 10);
      const body = byId("orders-body");
      while (body.firstChild) body.removeChild(body.firstChild);
      const mobile = byId("mobile-records");
      while (mobile.firstChild) mobile.removeChild(mobile.firstChild);
      if (!shown.length) {
        const empty = document.createElement("p");
        empty.className = "empty-records";
        empty.textContent = "No accepted rows match these filters. Clear a filter to see more rows.";
        mobile.appendChild(empty);
        setText(byId("rows-count"), "No matching rows");
        return;
      }
      shown.forEach(function(item) {
        const tr = document.createElement("tr");
        appendCell(tr, item.order_id);
        appendCell(tr, item.order_date);
        appendCell(tr, item.product);
        appendCell(tr, item.region);
        appendCell(tr, item.category);
        appendCell(tr, formatInteger(BigInt(item.quantity)));
        appendCell(tr, formatMoney(BigInt(item.line_total_cents)));
        body.appendChild(tr);

        const card = document.createElement("article");
        card.className = "record-card";
        const title = document.createElement("h3");
        title.textContent = item.product;
        card.appendChild(title);
        const meta = document.createElement("p");
        meta.className = "record-meta";
        meta.textContent = item.order_id + " · " + item.order_date;
        card.appendChild(meta);
        [
          ["Region", item.region],
          ["Category", item.category],
          ["Units", formatInteger(BigInt(item.quantity))],
          ["Revenue", formatMoney(BigInt(item.line_total_cents))]
        ].forEach(function(pair) {
          const detail = document.createElement("div");
          detail.className = "record-detail";
          const name = document.createElement("span");
          name.textContent = pair[0];
          const value = document.createElement("strong");
          value.textContent = String(pair[1]);
          detail.append(name, value);
          card.appendChild(detail);
        });
        mobile.appendChild(card);
      });
      setText(byId("rows-count"), "Showing " + shown.length + " of " + rows.length + " matching accepted rows");
    };
    const render = function() {
      const rows = matchingRows();
      const revenue = rows.reduce(function(sum, row) { return sum + BigInt(row.line_total_cents); }, 0n);
      const units = rows.reduce(function(sum, row) { return sum + BigInt(row.quantity); }, 0n);
      setText(byId("metric-revenue"), formatMoney(revenue));
      setText(byId("metric-orders"), formatInteger(BigInt(rows.length)));
      setText(byId("metric-units"), formatInteger(units));
      renderBars("chart-month", aggregate(rows, "month"), "No monthly revenue for this selection.");
      renderBars("chart-region", aggregate(rows, "region"), "No regional revenue for this selection.");
      const products = aggregate(rows, "product")
        .sort(function(a, b) { return a.cents === b.cents ? a.label.localeCompare(b.label) : (a.cents > b.cents ? -1 : 1); })
        .slice(0, 8);
      renderBars("chart-product", products, "No product revenue for this selection.");
      renderRows(rows);
      setText(byId("filter-status"), rows.length + (rows.length === 1 ? " accepted row matches." : " accepted rows match."));
    };
    byId("region-filter").addEventListener("change", render);
    byId("category-filter").addEventListener("change", render);
    byId("product-filter").addEventListener("input", render);
    byId("clear-filters").addEventListener("click", function() {
      byId("region-filter").value = "";
      byId("category-filter").value = "";
      byId("product-filter").value = "";
      render();
      byId("region-filter").focus();
    });
    const reportSummary = byId("report-summary");
    reportSummary.textContent = REPORT.accepted_records + " of " + REPORT.source_records
      + " source rows were accepted; " + REPORT.quarantined_records
      + " were quarantined. " + REPORT.trimmed_cells + " cells had outer whitespace trimmed. "
      + REPORT.header_changes_count + " header labels were normalized. Source SHA-256: "
      + REPORT.source_sha256 + ".";
    REPORT.rules.forEach(function(rule) {
      const item = document.createElement("li");
      const title = document.createElement("strong");
      title.textContent = rule.name + ": ";
      const detail = document.createElement("span");
      detail.textContent = rule.detail;
      item.append(title, detail);
      byId("report-rules").appendChild(item);
    });
    render();
  </script>
</body>
</html>'''
    return template.replace("__ROWS_B64__", rows_b64).replace("__REPORT_B64__", report_b64)


def _write_csv(path, headers, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(rows)


def build_markdown_report(report, quarantined_rows):
    lines = [
        "# CSV validation and change report",
        "",
        "**Synthetic, AI-assisted sales demonstration.** No real customer or sales records are represented.",
        "",
        "## Run summary",
        "",
        "- Source: {}".format(report["source_name"]),
        "- Source SHA-256: {}".format(report["source_sha256"]),
        "- Source size: {} bytes".format(report["source_size_bytes"]),
        "- Data rows read: {}".format(report["source_records"]),
        "- Accepted rows: {}".format(report["accepted_records"]),
        "- Quarantined rows: {}".format(report["quarantined_records"]),
        "- Cell trims: {}".format(report["trimmed_cells"]),
        "- Header labels normalized: {}".format(report["header_changes_count"]),
        "- Accepted units: {}".format(report["total_units"]),
        "- Accepted revenue: {} {}".format(report["currency"], report["total_revenue"]),
        "",
        "Every total is derived only from accepted rows. Source values are preserved in the original CSV and in each quarantine record.",
        "",
        "## The five fixed rules",
        "",
    ]
    for rule in report["rules"]:
        lines.append("{}. **{}** — {}".format(rule["id"], rule["name"], rule["detail"]))
    lines.extend(["", "## Header changes", ""])
    if report["header_changes"]:
        for item in report["header_changes"]:
            lines.append(
                "- Column {}: {} → {}".format(
                    item["column_number"],
                    json.dumps(item["before"], ensure_ascii=False),
                    json.dumps(item["after"], ensure_ascii=False),
                )
            )
    else:
        lines.append("No header labels changed.")
    lines.extend(["", "## Cell change log", ""])
    cell_changes = [item for item in report["change_log"] if item["record_number"] > 0]
    if cell_changes:
        for item in cell_changes:
            lines.append(
                "- Record {}, {}: {} → {}".format(
                    item["record_number"],
                    item["field"],
                    json.dumps(item["before"], ensure_ascii=False),
                    json.dumps(item["after"], ensure_ascii=False),
                )
            )
    else:
        lines.append("No cell values needed trimming.")
    lines.extend(["", "## Quarantined rows", ""])
    if quarantined_rows:
        for item in quarantined_rows:
            original = json.dumps(item["source_row"], ensure_ascii=False, separators=(",", ":"))
            lines.append(
                "- Record {} — {} Original cells: {}".format(
                    item["record_number"], item["reason"], original
                )
            )
    else:
        lines.append("No rows were quarantined.")
    lines.extend(
        [
            "",
            "## Arithmetic check",
            "",
            "Accepted revenue in integer cents: {}.".format(report["total_revenue_cents"]),
            "Dashboard and report totals are recomputed from the same accepted rows and exact decimal line totals.",
            "",
        ]
    )
    return "\n".join(lines)


def write_artifacts(result, output_dir):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    clean_rows = []
    for row in result["clean_rows"]:
        clean_rows.append([row[field] for field in EXPECTED_HEADERS] + [row["line_total"]])
    _write_csv(output_dir / "cleaned_sales.csv", OUTPUT_HEADERS, clean_rows)

    quarantine_rows = [
        [
            item["record_number"],
            item["reason"],
            json.dumps(item["source_row"], ensure_ascii=False, separators=(",", ":")),
        ]
        for item in result["quarantined_rows"]
    ]
    _write_csv(
        output_dir / "quarantined_rows.csv",
        ["source_record", "reason", "source_row_json"],
        quarantine_rows,
    )
    report = result["report"]
    report_payload = dict(report)
    report_payload["quarantined_rows"] = result["quarantined_rows"]
    (output_dir / "validation_report.json").write_text(
        json.dumps(report_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (output_dir / "validation_report.md").write_text(
        build_markdown_report(report, result["quarantined_rows"]),
        encoding="utf-8",
        newline="\n",
    )
    (output_dir / "dashboard.html").write_text(
        build_dashboard_html(result), encoding="utf-8", newline="\n"
    )


def build_reproducible_zip(project_root, zip_path):
    project_root = Path(project_root).resolve()
    zip_path = Path(zip_path).resolve()
    excluded_dirs = {"publishing", ".git", "__pycache__", ".pytest_cache", ".mypy_cache"}
    files = []
    for directory, child_dirs, file_names in os.walk(project_root):
        child_dirs[:] = sorted(name for name in child_dirs if name not in excluded_dirs)
        for file_name in sorted(file_names):
            path = Path(directory) / file_name
            if path.resolve() == zip_path or path.suffix in {".pyc", ".pyo"}:
                continue
            relative = path.relative_to(project_root)
            files.append((relative.as_posix(), path))
    files.sort(key=lambda item: item[0])
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, path in files:
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            info.create_system = 3
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def run_pipeline(source_path, output_dir, zip_path=None):
    source_path = Path(source_path)
    with source_path.open("rb") as source:
        source_bytes = source.read(MAX_BYTES + 1)
    if len(source_bytes) > MAX_BYTES:
        raise InputRejected("Input exceeds the 10 MB (10,000,000 byte) file limit.")
    result = clean_csv_bytes(source_bytes, source_name=source_path.name)
    write_artifacts(result, output_dir)
    if zip_path is not None:
        build_reproducible_zip(ROOT, zip_path)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Clean the fictional sales CSV and build an offline dashboard.")
    parser.add_argument("--input", default=str(ROOT / "data" / "source_sales.csv"))
    parser.add_argument("--output", default=str(ROOT / "outputs"))
    parser.add_argument("--zip", default=str(ROOT / "CSV_Dashboard_Demo.zip"))
    args = parser.parse_args(argv)
    try:
        result = run_pipeline(args.input, args.output, args.zip)
    except (InputRejected, OSError, ValueError) as exc:
        print("CSV demo failed: {}".format(exc), file=sys.stderr)
        return 2
    report = result["report"]
    print(
        "Built offline dashboard: {} accepted, {} quarantined, {} trimmed cells, {} {} revenue.".format(
            report["accepted_records"],
            report["quarantined_records"],
            report["trimmed_cells"],
            CURRENCY,
            report["total_revenue"],
        )
    )
    print("Output folder: {}".format(Path(args.output).resolve()))
    print("ZIP: {}".format(Path(args.zip).resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
