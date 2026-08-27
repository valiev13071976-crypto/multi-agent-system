"""Shared helpers for acquisition parsers."""

from __future__ import annotations

import csv
import io
import json
import re
from xml.etree import ElementTree

from acquisition.freshness import freshness_label
from acquisition.identifiers import normalize_ean, normalize_mpn, normalize_sku, validate_ean
from acquisition.models import (
    CONTENT_TRUST_UNTRUSTED,
    ParsedRecord,
    RawArtifact,
    ValidationResult,
    fingerprint_record,
    new_id,
    utc_now,
)
from acquisition.validation import validate_record


def base_provenance(artifact: RawArtifact, parser_id: str, parser_version: str) -> dict:
    return {
        "source_id": artifact.source_id,
        "artifact_id": artifact.artifact_id,
        "parser_id": parser_id,
        "parser_version": parser_version,
        "fetched_at": artifact.fetched_at.isoformat(),
        "url": artifact.url,
        "checksum": artifact.checksum,
        "content_type": artifact.content_type,
        "document_id": artifact.document_id,
        "content_trust": artifact.content_trust or CONTENT_TRUST_UNTRUSTED,
    }


def make_record(
    *,
    artifact: RawArtifact,
    parser_id: str,
    parser_version: str,
    record_type: str,
    fields: dict,
    confidence: float = 0.8,
    raw_field_refs: dict | None = None,
    freshness_policy=None,
) -> ParsedRecord:
    cleaned = dict(fields)
    # Normalize common identifiers when present
    if "ean" in cleaned or "gtin" in cleaned:
        ean = normalize_ean(cleaned.get("ean") or cleaned.get("gtin"))
        if ean and validate_ean(ean):
            cleaned["ean"] = ean
    if "sku" in cleaned or "supplier_sku" in cleaned:
        sku = normalize_sku(cleaned.get("sku") or cleaned.get("supplier_sku"))
        if sku:
            cleaned["sku"] = sku
            cleaned.setdefault("supplier_sku", sku)
    if "mpn" in cleaned:
        mpn = normalize_mpn(cleaned.get("mpn"))
        if mpn:
            cleaned["mpn"] = mpn
    fp = fingerprint_record(
        {
            "source_id": artifact.source_id,
            "record_type": record_type,
            **{k: cleaned[k] for k in sorted(cleaned)},
        }
    )
    vr = validate_record(record_type, cleaned)
    fresh = freshness_label(fetched_at=artifact.fetched_at, policy=freshness_policy)
    return ParsedRecord(
        record_id=new_id("rec-"),
        parser_id=parser_id,
        parser_version=parser_version,
        source_id=artifact.source_id,
        artifact_id=artifact.artifact_id,
        tenant_id=artifact.tenant_id,
        record_type=record_type,
        fields=cleaned,
        confidence=confidence,
        fingerprint=fp,
        observed_at=utc_now(),
        provenance=base_provenance(artifact, parser_id, parser_version),
        raw_field_refs=raw_field_refs or {},
        validation_ok=vr.ok,
        validation_errors=vr.errors,
        freshness=fresh,
        content_trust=artifact.content_trust or CONTENT_TRUST_UNTRUSTED,
    )


def parse_csv_rows(text: str) -> list[dict[str, str]]:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    rows = []
    for row in reader:
        cleaned = {str(k).strip().lower(): str(v).strip() for k, v in (row or {}).items() if k}
        if any(cleaned.values()):
            rows.append(cleaned)
    return rows


def parse_json_payload(text: str):
    return json.loads(text)


def parse_xml_root(text: str):
    return ElementTree.fromstring(text)


def strip_html(text: str) -> str:
    no_script = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    no_tags = re.sub(r"(?s)<[^>]+>", " ", no_script)
    return re.sub(r"\s+", " ", no_script and no_tags or "").strip()


def map_price_fields(row: dict) -> dict:
    aliases = {
        "sku": ("sku", "supplier_sku", "article", "артикул", "code", "item_code"),
        "mpn": ("mpn", "manufacturer_sku", "mfr_part", "part_number"),
        "ean": ("ean", "gtin", "barcode", "штрихкод"),
        "brand": ("brand", "manufacturer", "бренд", "производитель"),
        "name": ("name", "title", "product", "наименование", "model_name"),
        "model": ("model", "модель"),
        "price": ("price", "цена", "cost", "amount"),
        "currency": ("currency", "валюта", "curr"),
        "stock": ("stock", "qty", "quantity", "наличие", "available"),
        "moq": ("moq", "min_qty", "minimum_order"),
        "warehouse": ("warehouse", "склад", "location"),
    }
    out = {}
    for canon, keys in aliases.items():
        for key in keys:
            if key in row and row[key] not in ("", None):
                out[canon] = row[key]
                break
    if "price" in out:
        try:
            out["price"] = float(str(out["price"]).replace(",", ".").replace(" ", ""))
        except ValueError:
            pass
    if "stock" in out:
        try:
            out["stock"] = float(str(out["stock"]).replace(",", ".").replace(" ", ""))
        except ValueError:
            pass
    if "moq" in out:
        try:
            out["moq"] = float(str(out["moq"]).replace(",", ".").replace(" ", ""))
        except ValueError:
            pass
    if "currency" in out:
        out["currency"] = str(out["currency"]).upper()
    return out


class BaseParser:
    def validate_output(self, record: ParsedRecord) -> ValidationResult:
        return validate_record(record.record_type, dict(record.fields))
