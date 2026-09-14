"""Small realistic product-price-list fixture for the live evaluation --
equivalent in shape to the LG-TV supplier XLSX used in PR #71/#72/#73's
own regressions (SKU / product name / category / brand / EAN / purchase
price / retail price columns), sized at 5 rows so "give me a different
one" can be exercised more than twice without running out of rows.
"""

from __future__ import annotations

import io

SKUS = ["TV-A-1001", "TV-B-2002", "TV-C-3003", "TV-D-4004", "TV-E-5005"]

_ROWS = [
    ("TV-A-1001", "Модель A", "Телевизоры", "LG", "4600000000010", "90000", "129990"),
    ("TV-B-2002", "Модель B", "Телевизоры", "LG", "4600000000027", "95000", "139990"),
    ("TV-C-3003", "Модель C", "Телевизоры", "LG", "4600000000034", "99000", "149990"),
    ("TV-D-4004", "Модель D", "Телевизоры", "LG", "4600000000041", "105000", "159990"),
    ("TV-E-5005", "Модель E", "Телевизоры", "LG", "4600000000058", "110000", "169990"),
]

FILENAME = "LG_TV_price_list.xlsx"


def xlsx_bytes() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["sku", "product_name", "category", "brand", "ean", "purchase_price", "розница"])
    for row in _ROWS:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
