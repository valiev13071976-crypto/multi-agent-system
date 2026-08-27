"""Tenant-scoped dataset persistence — in-memory + SQLite."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from typing import Protocol

from data_intel.contracts import DatasetDescriptor, DataTransformation, utc_now
from data_intel.errors import DATASET_ACCESS_DENIED, DATASET_NOT_FOUND, DataIntelError
from security.tenant import normalize_tenant_id, tenants_match


class DatasetStore(Protocol):
    def save_dataset(self, descriptor: DatasetDescriptor, rows_by_table: dict[str, list[dict]]) -> None: ...
    def get_dataset(self, dataset_id: str, *, tenant_id: str) -> DatasetDescriptor | None: ...
    def get_rows(self, dataset_id: str, *, tenant_id: str, table_id: str | None = None) -> list[dict]: ...
    def save_transformation(self, tenant_id: str, tx: DataTransformation) -> None: ...
    def save_blob(self, dataset_id: str, name: str, data: bytes, *, tenant_id: str) -> None: ...
    def get_blob(self, dataset_id: str, name: str, *, tenant_id: str) -> bytes | None: ...
    def save_partial(self, dataset_id: str, batch_index: int, payload: dict, *, tenant_id: str) -> None: ...
    def list_partials(self, dataset_id: str, *, tenant_id: str) -> dict[int, dict]: ...


class InMemoryDatasetStore:
    def __init__(self):
        self._desc: dict[str, DatasetDescriptor] = {}
        self._rows: dict[str, dict[str, list[dict]]] = {}
        self._tx: list[tuple[str, DataTransformation]] = []
        self._blobs: dict[tuple[str, str], bytes] = {}
        self._partials: dict[str, dict[int, dict]] = {}
        self._lock = threading.RLock()

    def save_dataset(self, descriptor: DatasetDescriptor, rows_by_table: dict[str, list[dict]]) -> None:
        with self._lock:
            self._desc[descriptor.dataset_id] = descriptor
            self._rows[descriptor.dataset_id] = {
                k: [dict(r) for r in v] for k, v in (rows_by_table or {}).items()
            }

    def get_dataset(self, dataset_id: str, *, tenant_id: str) -> DatasetDescriptor | None:
        with self._lock:
            d = self._desc.get(dataset_id)
            if d is None:
                return None
            if not tenants_match(d.tenant_id, tenant_id):
                return None
            return d

    def get_rows(self, dataset_id: str, *, tenant_id: str, table_id: str | None = None) -> list[dict]:
        d = self.get_dataset(dataset_id, tenant_id=tenant_id)
        if d is None:
            raise DataIntelError(DATASET_ACCESS_DENIED if dataset_id in self._desc else DATASET_NOT_FOUND)
        tables = self._rows.get(dataset_id) or {}
        if table_id:
            return [dict(r) for r in tables.get(table_id, [])]
        out: list[dict] = []
        for rows in tables.values():
            out.extend(dict(r) for r in rows)
        return out

    def save_transformation(self, tenant_id: str, tx: DataTransformation) -> None:
        self._tx.append((normalize_tenant_id(tenant_id), tx))

    def save_blob(self, dataset_id: str, name: str, data: bytes, *, tenant_id: str) -> None:
        d = self.get_dataset(dataset_id, tenant_id=tenant_id)
        if d is None:
            raise DataIntelError(DATASET_ACCESS_DENIED)
        self._blobs[(dataset_id, name)] = data

    def get_blob(self, dataset_id: str, name: str, *, tenant_id: str) -> bytes | None:
        if self.get_dataset(dataset_id, tenant_id=tenant_id) is None:
            return None
        return self._blobs.get((dataset_id, name))

    def save_partial(self, dataset_id: str, batch_index: int, payload: dict, *, tenant_id: str) -> None:
        if self.get_dataset(dataset_id, tenant_id=tenant_id) is None:
            # allow partials during large ingest before full descriptor exists
            pass
        self._partials.setdefault(dataset_id, {})[int(batch_index)] = dict(payload)
        self._partials[dataset_id][int(batch_index)]["_tenant"] = normalize_tenant_id(tenant_id)

    def list_partials(self, dataset_id: str, *, tenant_id: str) -> dict[int, dict]:
        tid = normalize_tenant_id(tenant_id)
        out = {}
        for idx, payload in (self._partials.get(dataset_id) or {}).items():
            if payload.get("_tenant") == tid:
                out[idx] = {k: v for k, v in payload.items() if k != "_tenant"}
        return out


_SCHEMA = """
CREATE TABLE IF NOT EXISTS data_intel_datasets (
  dataset_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS data_intel_rows (
  dataset_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  table_id TEXT NOT NULL,
  row_index INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (dataset_id, table_id, row_index)
);
CREATE TABLE IF NOT EXISTS data_intel_blobs (
  dataset_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  name TEXT NOT NULL,
  data BLOB NOT NULL,
  PRIMARY KEY (dataset_id, name)
);
CREATE TABLE IF NOT EXISTS data_intel_partials (
  dataset_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  batch_index INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (dataset_id, batch_index)
);
CREATE TABLE IF NOT EXISTS data_intel_transformations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


class SqliteDatasetStore:
    """Durable tenant-scoped dataset store (dedicated path or shared SqliteConnection)."""

    available = True

    def __init__(
        self,
        path: str | None = None,
        *,
        db_path: str | None = None,
        shared_connection=None,
        owns_connection: bool | None = None,
        conn: sqlite3.Connection | None = None,
    ):
        self._lock = threading.RLock()
        self._local = threading.local()
        self._shared = shared_connection
        self._legacy_conn = conn
        resolved = db_path or path
        if shared_connection is not None:
            self.path = str(getattr(shared_connection, "path", ".") or ".")
            self.owns_connection = False if owns_connection is None else bool(owns_connection)
            self.connection_mode = "shared"
            self.persistence_backend = "sqlite"
        elif conn is not None:
            self.path = ":memory:"
            self.owns_connection = False if owns_connection is None else bool(owns_connection)
            self.connection_mode = "injected"
            self.persistence_backend = "sqlite"
        elif resolved:
            from pathlib import Path

            self.path = str(resolved)
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.owns_connection = True if owns_connection is None else bool(owns_connection)
            self.connection_mode = "dedicated"
            self.persistence_backend = "sqlite"
        else:
            raise ValueError("dataset_store_requires_path_or_shared_connection")
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        if self._shared is not None:
            return self._shared.connect()
        if self._legacy_conn is not None:
            return self._legacy_conn
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _commit(self, conn: sqlite3.Connection) -> None:
        if self._shared is not None and hasattr(self._shared, "maybe_autocommit"):
            self._shared.maybe_autocommit()
            return
        conn.commit()

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            conn.executescript(_SCHEMA)
            self._commit(conn)

    def close(self) -> None:
        with self._lock:
            if not self.owns_connection:
                return
            if self._shared is not None:
                return
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                conn.close()
                self._local.conn = None

    def save_dataset(self, descriptor: DatasetDescriptor, rows_by_table: dict[str, list[dict]]) -> None:
        payload = {
            "dataset_id": descriptor.dataset_id,
            "tenant_id": descriptor.tenant_id,
            "source_document_id": descriptor.source_document_id,
            "format": descriptor.format,
            "sheets": list(descriptor.sheets),
            "tables": [
                {
                    "table_id": t.table_id,
                    "sheet": t.sheet,
                    "range": t.range,
                    "header_row": t.header_row,
                    "row_count": t.row_count,
                    "confidence": t.confidence,
                    "unresolved": t.unresolved,
                    "columns": [
                        {
                            "source_name": c.source_name,
                            "normalized_name": c.normalized_name,
                            "inferred_type": c.inferred_type,
                            "semantic_role": c.semantic_role,
                            "confidence": c.confidence,
                        }
                        for c in t.columns
                    ],
                }
                for t in descriptor.tables
            ],
            "row_count": descriptor.row_count,
            "column_count": descriptor.column_count,
            "checksum": descriptor.checksum,
            "schema_version": descriptor.schema_version,
            "created_at": descriptor.created_at.isoformat(),
            "provenance": dict(descriptor.provenance),
            "status": descriptor.status,
        }
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT OR REPLACE INTO data_intel_datasets(dataset_id, tenant_id, payload_json, created_at) VALUES (?,?,?,?)",
                (
                    descriptor.dataset_id,
                    descriptor.tenant_id,
                    json.dumps(payload, ensure_ascii=False),
                    descriptor.created_at.isoformat(),
                ),
            )
            conn.execute(
                "DELETE FROM data_intel_rows WHERE dataset_id=?", (descriptor.dataset_id,)
            )
            for table_id, rows in (rows_by_table or {}).items():
                for i, row in enumerate(rows):
                    conn.execute(
                        "INSERT INTO data_intel_rows(dataset_id, tenant_id, table_id, row_index, payload_json) VALUES (?,?,?,?,?)",
                        (
                            descriptor.dataset_id,
                            descriptor.tenant_id,
                            table_id,
                            i,
                            json.dumps(row, ensure_ascii=False, default=str),
                        ),
                    )
            self._commit(conn)

    def get_dataset(self, dataset_id: str, *, tenant_id: str) -> DatasetDescriptor | None:
        from data_intel.contracts import ColumnDescriptor, TableDescriptor
        from datetime import datetime

        tid = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT payload_json, tenant_id FROM data_intel_datasets WHERE dataset_id=?",
                (dataset_id,),
            ).fetchone()
        if row is None:
            return None
        if not tenants_match(row["tenant_id"], tid):
            return None
        p = json.loads(row["payload_json"])
        tables = []
        for t in p.get("tables") or []:
            cols = tuple(
                ColumnDescriptor(
                    source_name=c["source_name"],
                    normalized_name=c["normalized_name"],
                    inferred_type=c.get("inferred_type") or "string",
                    semantic_role=c.get("semantic_role") or "unknown",
                    confidence=c.get("confidence") or "medium",
                )
                for c in t.get("columns") or []
            )
            tables.append(
                TableDescriptor(
                    table_id=t["table_id"],
                    sheet=t["sheet"],
                    range=t.get("range") or "",
                    header_row=int(t.get("header_row") or 1),
                    columns=cols,
                    row_count=int(t.get("row_count") or 0),
                    confidence=t.get("confidence") or "medium",
                    unresolved=bool(t.get("unresolved")),
                )
            )
        created = p.get("created_at") or utc_now().isoformat()
        try:
            created_at = datetime.fromisoformat(created)
        except ValueError:
            created_at = utc_now()
        return DatasetDescriptor(
            dataset_id=p["dataset_id"],
            tenant_id=p["tenant_id"],
            source_document_id=p.get("source_document_id") or "",
            format=p.get("format") or "",
            sheets=tuple(p.get("sheets") or ()),
            tables=tuple(tables),
            row_count=int(p.get("row_count") or 0),
            column_count=int(p.get("column_count") or 0),
            checksum=p.get("checksum") or "",
            schema_version=p.get("schema_version") or "1.0.0",
            created_at=created_at,
            provenance=p.get("provenance") or {},
            status=p.get("status") or "ready",
        )

    def get_rows(self, dataset_id: str, *, tenant_id: str, table_id: str | None = None) -> list[dict]:
        if self.get_dataset(dataset_id, tenant_id=tenant_id) is None:
            raise DataIntelError(DATASET_ACCESS_DENIED)
        tid = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            if table_id:
                cur = conn.execute(
                    "SELECT payload_json FROM data_intel_rows WHERE dataset_id=? AND tenant_id=? AND table_id=? ORDER BY row_index",
                    (dataset_id, tid, table_id),
                )
            else:
                cur = conn.execute(
                    "SELECT payload_json FROM data_intel_rows WHERE dataset_id=? AND tenant_id=? ORDER BY table_id, row_index",
                    (dataset_id, tid),
                )
            return [json.loads(r["payload_json"]) for r in cur.fetchall()]

    def save_transformation(self, tenant_id: str, tx: DataTransformation) -> None:
        payload = {
            "operation": tx.operation,
            "input_refs": list(tx.input_refs),
            "output_ref": tx.output_ref,
            "parameters": dict(tx.parameters),
            "provenance": dict(tx.provenance),
            "created_at": tx.created_at.isoformat(),
        }
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO data_intel_transformations(tenant_id, payload_json, created_at) VALUES (?,?,?)",
                (normalize_tenant_id(tenant_id), json.dumps(payload), tx.created_at.isoformat()),
            )
            self._commit(conn)

    def save_blob(self, dataset_id: str, name: str, data: bytes, *, tenant_id: str) -> None:
        if self.get_dataset(dataset_id, tenant_id=tenant_id) is None:
            raise DataIntelError(DATASET_ACCESS_DENIED)
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT OR REPLACE INTO data_intel_blobs(dataset_id, tenant_id, name, data) VALUES (?,?,?,?)",
                (dataset_id, normalize_tenant_id(tenant_id), name, data),
            )
            self._commit(conn)

    def get_blob(self, dataset_id: str, name: str, *, tenant_id: str) -> bytes | None:
        if self.get_dataset(dataset_id, tenant_id=tenant_id) is None:
            return None
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT data FROM data_intel_blobs WHERE dataset_id=? AND name=? AND tenant_id=?",
                (dataset_id, name, normalize_tenant_id(tenant_id)),
            ).fetchone()
        return bytes(row["data"]) if row else None

    def save_partial(self, dataset_id: str, batch_index: int, payload: dict, *, tenant_id: str) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT OR REPLACE INTO data_intel_partials(dataset_id, tenant_id, batch_index, payload_json) VALUES (?,?,?,?)",
                (
                    dataset_id,
                    normalize_tenant_id(tenant_id),
                    int(batch_index),
                    json.dumps(payload, ensure_ascii=False, default=str),
                ),
            )
            self._commit(conn)

    def list_partials(self, dataset_id: str, *, tenant_id: str) -> dict[int, dict]:
        tid = normalize_tenant_id(tenant_id)
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                "SELECT batch_index, payload_json FROM data_intel_partials WHERE dataset_id=? AND tenant_id=?",
                (dataset_id, tid),
            )
            return {int(r["batch_index"]): json.loads(r["payload_json"]) for r in cur.fetchall()}
