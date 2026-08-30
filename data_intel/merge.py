"""Controlled dataset merge / join."""

from __future__ import annotations

from collections import defaultdict

from data_intel.cleaning import clean_text
from data_intel.errors import DATASET_JOIN_EXPLOSION, MERGE_CONFLICT, DataIntelError


def _key_tuple(row: dict, keys: list[str]) -> tuple:
    return tuple(clean_text(row.get(k)) or "" for k in keys)


def merge_datasets(
    left_rows: list[dict],
    right_rows: list[dict],
    *,
    keys: list[str],
    how: str = "inner",
    left_prefix: str = "l_",
    right_prefix: str = "r_",
    allow_many_to_many: bool = False,
    max_output_rows: int = 500_000,
) -> dict:
    """Join datasets. how: inner|left|right|full|append."""
    how = (how or "inner").lower()
    if how == "append" or how == "union":
        out = [dict(r) for r in left_rows] + [dict(r) for r in right_rows]
        return {
            "rows": out,
            "unmatched_left": [],
            "unmatched_right": [],
            "conflicts": [],
            "one_to_many": [],
            "how": how,
        }

    if not keys:
        raise DataIntelError(MERGE_CONFLICT)

    l_index: dict[tuple, list[int]] = defaultdict(list)
    r_index: dict[tuple, list[int]] = defaultdict(list)
    for i, row in enumerate(left_rows):
        l_index[_key_tuple(row, keys)].append(i)
    for i, row in enumerate(right_rows):
        r_index[_key_tuple(row, keys)].append(i)

    conflicts = []
    one_to_many = []
    matched_keys = set(l_index) & set(r_index)
    for k in matched_keys:
        if len(l_index[k]) > 1 or len(r_index[k]) > 1:
            one_to_many.append({"key": list(k), "left": l_index[k], "right": r_index[k]})
            if len(l_index[k]) > 1 and len(r_index[k]) > 1:
                conflicts.append({"key": list(k), "reason": "many_to_many"})
                if not allow_many_to_many:
                    raise DataIntelError(DATASET_JOIN_EXPLOSION)

    def combine(lrow, rrow):
        out = {}
        for k, v in lrow.items():
            out[f"{left_prefix}{k}" if not str(k).startswith("__") else k] = v
        for k, v in rrow.items():
            nk = f"{right_prefix}{k}" if not str(k).startswith("__") else f"{right_prefix}{k}"
            out[nk] = v
        for k in keys:
            out[k] = lrow.get(k) if lrow.get(k) is not None else rrow.get(k)
        return out

    rows = []
    unmatched_left = []
    unmatched_right = []
    seen_pairs: set[tuple[int, int]] = set()

    def add_pair(i, j):
        if (i, j) in seen_pairs:
            return
        seen_pairs.add((i, j))
        rows.append(combine(left_rows[i], right_rows[j]))

    if how == "inner":
        for k in matched_keys:
            for i in l_index[k]:
                for j in r_index[k]:
                    add_pair(i, j)
        for k, lidxs in l_index.items():
            if k not in matched_keys:
                unmatched_left.extend(lidxs)
        for k, ridxs in r_index.items():
            if k not in matched_keys:
                unmatched_right.extend(ridxs)
    elif how == "left":
        for k, lidxs in l_index.items():
            ridxs = r_index.get(k, [])
            if not ridxs:
                for i in lidxs:
                    rows.append(dict(left_rows[i]))
                    unmatched_left.append(i)
            else:
                for i in lidxs:
                    for j in ridxs:
                        add_pair(i, j)
        for k, ridxs in r_index.items():
            if k not in matched_keys:
                unmatched_right.extend(ridxs)
    elif how == "right":
        for k, ridxs in r_index.items():
            lidxs = l_index.get(k, [])
            if not lidxs:
                for j in ridxs:
                    rows.append(dict(right_rows[j]))
                    unmatched_right.append(j)
            else:
                for i in lidxs:
                    for j in ridxs:
                        add_pair(i, j)
        for k, lidxs in l_index.items():
            if k not in matched_keys:
                unmatched_left.extend(lidxs)
    elif how == "full":
        for k, lidxs in l_index.items():
            ridxs = r_index.get(k, [])
            if not ridxs:
                for i in lidxs:
                    rows.append(dict(left_rows[i]))
                    unmatched_left.append(i)
            else:
                for i in lidxs:
                    for j in ridxs:
                        add_pair(i, j)
        for k, ridxs in r_index.items():
            if k not in l_index:
                for j in ridxs:
                    rows.append(dict(right_rows[j]))
                    unmatched_right.append(j)
    else:
        raise DataIntelError(MERGE_CONFLICT)

    if len(rows) > max_output_rows:
        raise DataIntelError(DATASET_JOIN_EXPLOSION)

    return {
        "rows": rows,
        "unmatched_left": unmatched_left,
        "unmatched_right": unmatched_right,
        "conflicts": conflicts,
        "one_to_many": one_to_many,
        "how": how,
        "keys": list(keys),
    }
