#!/usr/bin/env python3
"""Regenerate the fixed, fictional sales source CSV used by this demo."""

import csv
from datetime import date
from pathlib import Path


HEADERS = [
    "order_id",
    "order_date",
    "region",
    "product",
    "category",
    "quantity",
    "unit_price",
    "customer_note",
]
REGIONS = ["North", "South", "East", "West"]
PRODUCTS = [
    ("Cedar & Clay mug", "Home goods", "12.50"),
    ("Rêve planter", "Home goods", "24.00"),
    ("Mosslight lamp", "Lighting", "38.75"),
    ("Tidal notebook", "Paper goods", "8.40"),
    ("Orbit canvas tote", "Carry", "18.00"),
    ("Maple serving tray", "Kitchen", "29.25"),
    ("Slate pen set", "Paper goods", "14.60"),
    ("Finch steel bottle", "Drinkware", "22.00"),
]
NOTES = [
    "Fictional order for the offline CSV walkthrough.",
    'Quoted note: "keep the sample row intact" & review.',
    "Café pickup example · no real customer data.",
]
SOURCE_PATH = Path(__file__).resolve().parents[1] / "data" / "source_sales.csv"


def build_rows():
    rows = []
    for index in range(72):
        order_number = index + 1
        month = index // 12 + 1
        day = 2 + (index * 7) % 27
        product_index = (index * 3 + month) % len(PRODUCTS)
        product, category, unit_price = PRODUCTS[product_index]
        row = [
            "SYN-{:04d}".format(order_number),
            date(2026, month, day).isoformat(),
            REGIONS[index % len(REGIONS)],
            product,
            category,
            str(1 + (index * 5) % 6),
            unit_price,
            NOTES[index % len(NOTES)] if index % 9 == 0 else "",
        ]
        if index in {4, 13, 27, 40, 55, 68}:
            row[0] = " " + row[0] + " "
            row[2] = " " + row[2] + " "
        if index in {1, 31, 61}:
            row[3] = " " + row[3] + " "
        if index in {8, 48}:
            row[5] = " " + row[5] + " "
        if index in {10, 50}:
            row[6] = " " + row[6] + " "
        rows.append(row)

    rows.extend(
        [
            ["SYN-0001", "2026-06-22", "North", "Cedar & Clay mug", "Home goods", "2", "12.50", "Intentional repeated ID for quarantine demo."],
            ["SYN-BAD-01", "2026-06-23", "", "Rêve planter", "Home goods", "1", "24.00", "Missing region; kept for review."],
            ["SYN-BAD-02", "2026-06-24", "South", "Tidal notebook", "Paper goods", "1", "8.40", "Invalid date example."],
            ["SYN-BAD-03", "2026-06-25", "East", "Orbit canvas tote", "Carry", "many", "18.00", "Invalid quantity example."],
            ["SYN-BAD-04", "2026-06-26", "West", "Maple serving tray", "Kitchen", "1", "NaN", "Invalid price example."],
            ["SYN-BAD-05", "2026-06-27", "North", "Slate pen set", "Paper goods", "1", "14.601", "Three decimal places are rejected."],
            ["SYN-BAD-06", "2026-06-28", "South", "Finch steel bottle", "Drinkware", "1", "22.00"],
        ]
    )
    rows[74][1] = "2026-13-24"
    return rows


def main():
    SOURCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SOURCE_PATH.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(HEADERS)
        writer.writerows(build_rows())
    print("Wrote {} fictional source records to {}.".format(len(build_rows()), SOURCE_PATH))


if __name__ == "__main__":
    main()
