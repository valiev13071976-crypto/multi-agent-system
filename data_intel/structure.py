"""Deterministic spreadsheet structure detection."""

from __future__ import annotations

from data_intel.cleaning import clean_text, is_empty_row
from data_intel.contracts import CONF_HIGH, CONF_LOW, CONF_MEDIUM, CONF_UNRESOLVED, TableDescriptor
from data_intel.errors import STRUCTURE_AMBIGUOUS, DataIntelError
from data_intel.mapping import map_columns


def _row_nonempty_count(row: list) -> int:
    return sum(1 for c in row if clean_text(c) is not None)


def _looks_like_header(row: list) -> bool:
    texts = [clean_text(c) for c in row]
    nonempty = [t for t in texts if t]
    if len(nonempty) < 2:
        return False
    # Headers tend to be short strings, few pure numbers
    numeric = 0
    for t in nonempty:
        try:
            float(t.replace(",", ".").replace(" ", ""))
            numeric += 1
        except ValueError:
            pass
    return numeric <= max(1, len(nonempty) // 3)


def _score_header_candidate(grid: list[list], idx: int) -> float:
    if idx >= len(grid):
        return -1.0
    row = grid[idx]
    if not _looks_like_header(row):
        return -1.0
    score = float(_row_nonempty_count(row))
    # Prefer rows followed by denser data
    following = 0
    for j in range(idx + 1, min(idx + 6, len(grid))):
        following += _row_nonempty_count(grid[j])
    score += following * 0.1
    # Penalize title-like single-cell rows
    if _row_nonempty_count(row) == 1:
        score -= 5
    return score


def detect_tables_in_sheet(
    sheet_name: str,
    grid: list[list],
    *,
    dataset_id: str = "",
    allow_ambiguous: bool = True,
) -> list[TableDescriptor]:
    """Detect one or more logical tables on a sheet.

    Handles title rows, blank separators, repeated headers, footer totals.
    """
    if not grid:
        return []

    # Split by blank-row separators into regions
    regions: list[tuple[int, int]] = []
    start = None
    for i, row in enumerate(grid):
        empty = _row_nonempty_count(row) == 0
        if not empty and start is None:
            start = i
        if empty and start is not None:
            regions.append((start, i - 1))
            start = None
    if start is not None:
        regions.append((start, len(grid) - 1))

    tables: list[TableDescriptor] = []
    for ri, (r0, r1) in enumerate(regions):
        region = grid[r0 : r1 + 1]
        if len(region) < 2:
            continue
        # Skip footer-only regions (totals)
        first = region[0]
        first_text = " ".join(str(clean_text(c) or "") for c in first).lower()
        if any(t in first_text for t in ("итого", "total", "сумма")) and _row_nonempty_count(first) <= 3:
            continue

        # Score header candidates in first 5 rows of region
        best_idx = 0
        best_score = -1.0
        scores = []
        for off in range(min(5, len(region))):
            sc = _score_header_candidate(region, off)
            scores.append(sc)
            if sc > best_score:
                best_score = sc
                best_idx = off

        ambiguous = False
        # Two strong header candidates → ambiguous
        strong = sorted([s for s in scores if s > 0], reverse=True)
        if len(strong) >= 2 and strong[0] - strong[1] < 1.5:
            ambiguous = True

        if best_score < 0:
            # fallback: first nonempty row
            best_idx = 0
            ambiguous = True

        header_row_abs = r0 + best_idx
        headers_raw = [clean_text(c) or f"col_{i+1}" for i, c in enumerate(region[best_idx])]
        # Deduplicate headers
        seen: dict[str, int] = {}
        headers: list[str] = []
        for h in headers_raw:
            if h in seen:
                seen[h] += 1
                headers.append(f"{h}_{seen[h]}")
            else:
                seen[h] = 0
                headers.append(h)

        data_rows = []
        for abs_i in range(header_row_abs + 1, r1 + 1):
            row = grid[abs_i]
            # Skip repeated headers
            if _looks_like_header(row) and [clean_text(c) for c in row[: len(headers)]] == [
                clean_text(c) for c in region[best_idx][: len(headers)]
            ]:
                continue
            # Skip totals footer
            joined = " ".join(str(clean_text(c) or "") for c in row).lower()
            if any(t in joined for t in ("итого", "total")) and _row_nonempty_count(row) <= 3:
                continue
            mapping = {headers[j]: (row[j] if j < len(row) else None) for j in range(len(headers))}
            if is_empty_row(mapping):
                continue
            data_rows.append(mapping)

        if not data_rows:
            continue

        columns = map_columns(headers, data_rows)
        conf = CONF_UNRESOLVED if ambiguous else (CONF_HIGH if best_score >= 4 else CONF_MEDIUM)
        if ambiguous and not allow_ambiguous:
            raise DataIntelError(STRUCTURE_AMBIGUOUS)

        end_col = max(1, len(headers))
        tables.append(
            TableDescriptor(
                table_id=f"{sheet_name}:t{ri}" if sheet_name else f"t{ri}",
                sheet=sheet_name,
                range=f"A{header_row_abs + 1}:{_col(end_col)}{r1 + 1}",
                header_row=header_row_abs + 1,
                columns=columns,
                row_count=len(data_rows),
                confidence=conf,
                evidence={
                    "region_start": r0 + 1,
                    "region_end": r1 + 1,
                    "header_score": best_score,
                    "title_rows_skipped": best_idx,
                },
                unresolved=ambiguous,
            )
        )
    return tables


def _col(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s or "A"


def extract_table_rows(grid: list[list], table: TableDescriptor) -> list[dict]:
    """Re-extract data rows for a detected table from the sheet grid."""
    header_idx = table.header_row - 1
    if header_idx < 0 or header_idx >= len(grid):
        return []
    headers = [c.source_name for c in table.columns]
    header_vals = [clean_text(c) for c in grid[header_idx][: len(headers)]]
    rows = []
    for i in range(header_idx + 1, len(grid)):
        row = grid[i]
        if _row_nonempty_count(row) == 0:
            break
        joined = " ".join(str(clean_text(c) or "") for c in row).lower()
        if any(t in joined for t in ("итого", "total")) and _row_nonempty_count(row) <= 3:
            break
        # Repeated header only
        row_vals = [clean_text(c) for c in row[: len(headers)]]
        if row_vals == header_vals:
            continue
        mapping = {headers[j]: (row[j] if j < len(row) else None) for j in range(len(headers))}
        if is_empty_row(mapping):
            continue
        mapping["__source_row"] = i + 1
        rows.append(mapping)
    return rows
