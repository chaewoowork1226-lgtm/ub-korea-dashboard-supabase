# -*- coding: utf-8 -*-
"""UB Korea dashboard -> Supabase synchronization.

The browser never receives the Supabase key.  This module runs inside the local
Python HTTP server and talks to PostgREST with environment variables only.
Before writing, it reads the live catalog produced by ``dashboard_schema()`` and
keeps only columns that really exist.  That lets the dashboard reuse older UB
Korea table variants without hard-coding one exact database revision.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable


BASE_DIR = Path(__file__).resolve().parent


def _load_env_file(path: Path | None = None) -> None:
    """Load a small, conventional .env file without adding a runtime dependency."""
    path = path or (BASE_DIR / ".env")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] == value[-1:] and value[:1] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file()


class SupabaseError(RuntimeError):
    pass


def _stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _norm(value: Any) -> str:
    text = str(value or "").lower()
    for junk in ("스페인no.1", "세계no.1", "지구no.1", "직수입", "벌크", "세트", "캔"):
        text = text.replace(junk, "")
    return re.sub(r"[^0-9a-z가-힣]", "", text)


def _month_start(ym: str) -> str:
    return ym[:7] + "-01"


def _previous_month(ym: str) -> str:
    year, month = map(int, ym[:7].split("-"))
    first = _dt.date(year, month, 1)
    return (first - _dt.timedelta(days=1)).strftime("%Y-%m")


def _chunks(rows: list[dict[str, Any]], size: int = 300) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


class SchemaCatalog:
    def __init__(self, payload: dict[str, Any] | None):
        payload = payload or {}
        self.payload = payload
        self.tables: dict[tuple[str, str], dict[str, Any]] = {}
        for table in payload.get("tables") or []:
            schema = str(table.get("schema") or "public")
            name = str(table.get("name") or "")
            if name:
                self.tables[(schema, name)] = table

    def get(self, schema: str, table: str) -> dict[str, Any] | None:
        return self.tables.get((schema, table))

    def columns(self, schema: str, table: str) -> set[str]:
        item = self.get(schema, table) or {}
        return {str(c.get("name")) for c in (item.get("columns") or []) if c.get("name")}

    def unique_sets(self, schema: str, table: str) -> list[set[str]]:
        item = self.get(schema, table) or {}
        result: list[set[str]] = []
        primary = {str(x) for x in (item.get("primary_key") or []) if x}
        if primary:
            result.append(primary)
        for constraint in item.get("unique_constraints") or []:
            cols = {str(x) for x in (constraint.get("columns") or []) if x}
            if cols:
                result.append(cols)
        return result


class SupabaseClient:
    """Minimal server-side PostgREST client using only Python's standard library."""

    def __init__(self, url: str | None = None, key: str | None = None):
        self.url = (url or os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
        self.key = (key or os.getenv("SUPABASE_SECRET_KEY") or
                    os.getenv("SUPABASE_SERVICE_ROLE_KEY") or
                    os.getenv("SUPABASE_KEY") or "").strip()
        self.data_schema = (os.getenv("SUPABASE_DATA_SCHEMA") or "public").strip()
        self.schema_names = [x.strip() for x in
                             (os.getenv("SUPABASE_SCHEMA_NAMES") or "public,analytics").split(",")
                             if x.strip()]

    @property
    def configured(self) -> bool:
        return bool(self.url and self.key)

    def _request(self, path: str, *, method: str = "GET", body: Any = None,
                 query: dict[str, Any] | None = None, schema: str | None = None,
                 prefer: str | None = None) -> Any:
        if not self.configured:
            raise SupabaseError("SUPABASE_URL과 Supabase 서버용 키가 설정되지 않았습니다.")
        qs = urllib.parse.urlencode(query or {}, safe="(),.*:\"-")
        endpoint = self.url + path + (("?" + qs) if qs else "")
        encoded = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "apikey": self.key,
            "Accept": "application/json",
        }
        # New sb_secret_* keys are opaque API keys, not JWTs.  Supabase requires
        # them in `apikey` only.  Legacy service_role keys are JWTs and retain the
        # Authorization header for backwards compatibility.
        if not self.key.startswith("sb_"):
            headers["Authorization"] = "Bearer " + self.key
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        if schema:
            headers["Accept-Profile"] = schema
            headers["Content-Profile"] = schema
        if prefer:
            headers["Prefer"] = prefer
        req = urllib.request.Request(endpoint, data=encoded, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(detail)
                detail = parsed.get("message") or parsed.get("hint") or detail
            except ValueError:
                pass
            raise SupabaseError(f"Supabase 요청 실패 ({exc.code}): {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SupabaseError(f"Supabase에 연결하지 못했습니다: {exc}") from exc

    @staticmethod
    def split_table(value: str, default_schema: str = "public") -> tuple[str, str]:
        if "." in value:
            return tuple(value.split(".", 1))  # type: ignore[return-value]
        return default_schema, value

    def schema(self) -> dict[str, Any]:
        """Read fresh metadata on every call; no cache is intentional."""
        if not self.configured:
            return {"configured": False, "tables": [],
                    "error": "프로젝트의 .env에 SUPABASE_URL과 서버용 키를 넣어주세요."}
        try:
            result = self._request("/rest/v1/rpc/dashboard_schema", method="POST",
                                   body={"p_schemas": self.schema_names},
                                   schema="public")
            if isinstance(result, dict):
                result["configured"] = True
                result["source"] = "dashboard_schema RPC"
                return result
        except SupabaseError as rpc_error:
            # A server-side secret key can still read the current PostgREST OpenAPI
            # description.  It is a useful fallback until the setup SQL is installed.
            try:
                spec = self._request("/rest/v1/", schema=self.data_schema)
                fallback = self._schema_from_openapi(spec or {})
                fallback.update({"configured": True, "source": "PostgREST OpenAPI",
                                 "warning": "PK/FK와 파티션까지 보려면 supabase/setup_dashboard_integration.sql을 실행하세요."})
                return fallback
            except SupabaseError:
                raise rpc_error
        raise SupabaseError("Supabase 스키마 응답 형식을 확인할 수 없습니다.")

    def _schema_from_openapi(self, spec: dict[str, Any]) -> dict[str, Any]:
        definitions = spec.get("definitions") or spec.get("components", {}).get("schemas", {}) or {}
        tables = []
        for name, definition in sorted(definitions.items()):
            props = definition.get("properties") or {}
            required = set(definition.get("required") or [])
            columns = []
            for ordinal, (col, info) in enumerate(props.items(), 1):
                typ = info.get("format") or info.get("type") or "unknown"
                columns.append({"name": col, "data_type": typ, "nullable": col not in required,
                                "default": info.get("default"), "ordinal": ordinal, "pk": False})
            tables.append({"schema": self.data_schema, "name": name, "type": "table",
                           "columns": columns, "primary_key": [], "unique_constraints": [],
                           "foreign_keys": [], "parent_schema": None, "parent_name": None})
        return {"generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(), "tables": tables}

    def select(self, table: str, columns: list[str], *, query: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        schema, name = self.split_table(table, self.data_schema)
        params = {"select": ",".join(columns)}
        params.update(query or {})
        result = self._request("/rest/v1/" + urllib.parse.quote(name), query=params, schema=schema)
        return result if isinstance(result, list) else []

    def upsert(self, table: str, rows: list[dict[str, Any]], conflict: list[str]) -> int:
        if not rows:
            return 0
        schema, name = self.split_table(table, self.data_schema)
        for batch in _chunks(rows):
            self._request("/rest/v1/" + urllib.parse.quote(name), method="POST", body=batch,
                          query={"on_conflict": ",".join(conflict)}, schema=schema,
                          prefer="resolution=merge-duplicates,return=minimal")
        return len(rows)


TABLES = {
    "inventory": ("SUPABASE_TABLE_DAILY_INVENTORY", "daily_inventory"),
    "shipments": ("SUPABASE_TABLE_DAILY_SHIPMENTS", "daily_shipments"),
    "holdings": ("SUPABASE_TABLE_HOLDINGS", "holding_snapshots"),
    "costs": ("SUPABASE_TABLE_INVENTORY_COSTS", "inventory_cost_snapshots"),
    "reconciliations": ("SUPABASE_TABLE_RECONCILIATIONS", "shipment_month_reconciliations"),
    "inbound": ("SUPABASE_TABLE_INBOUND", "inbound_orders"),
    "ecommerce": ("SUPABASE_TABLE_ECOMMERCE", "ecommerce_sku_monthly"),
    "products": ("SUPABASE_TABLE_PRODUCTS", "products"),
    "aliases": ("SUPABASE_TABLE_PRODUCT_ALIASES", "product_aliases"),
    "unmatched": ("SUPABASE_TABLE_UNMATCHED", "dashboard_import_unmatched"),
    "runs": ("SUPABASE_TABLE_IMPORT_RUNS", "dashboard_import_runs"),
}


COLUMNS: dict[str, dict[str, tuple[str, ...]]] = {
    "inventory": {
        "product_code": ("product_code", "sku", "item_code"),
        "report_date": ("report_date", "snapshot_date", "as_of_date"),
        "stock_quantity": ("stock_quantity", "physical_stock", "quantity"),
        "wip_quantity": ("wip_quantity", "work_in_progress", "wip"),
        "holding_this_month": ("holding_this_month", "holding_quantity", "holding"),
        "month_to_date_shipment": ("month_to_date_shipment", "mtd_shipment", "shipped_quantity"),
        "actual_month_to_date_shipment": ("actual_month_to_date_shipment", "actual_mtd_shipment"),
        "available_stock": ("available_stock", "available_quantity"),
        "source": ("source",), "source_file": ("source_file",),
        "source_name": ("source_name", "product_name"),
    },
    "shipments": {
        "product_code": ("product_code", "sku", "item_code"),
        "shipment_date": ("shipment_date", "report_date", "date"),
        "quantity": ("quantity", "shipment_quantity", "qty"),
        "source": ("source",), "source_file": ("source_file",), "note": ("note",),
    },
    "holdings": {
        "product_code": ("product_code", "sku", "item_code"),
        "hold_month": ("hold_month", "month", "period"),
        "as_of_date": ("as_of_date", "snapshot_date", "report_date"),
        "quantity": ("quantity", "holding_quantity", "qty"),
        "source": ("source",), "source_file": ("source_file",), "note": ("note",),
    },
    "costs": {
        "product_code": ("product_code", "sku", "item_code"),
        "source_product_code": ("source_product_code", "erp_product_code"),
        "source_name": ("source_name", "product_name"),
        "as_of_date": ("as_of_date", "snapshot_date", "report_date"),
        "unit_cost": ("unit_cost", "inventory_unit_cost", "cost"),
        "source": ("source",), "source_file": ("source_file",),
    },
    "reconciliations": {
        "product_code": ("product_code", "sku", "item_code"),
        "shipment_month": ("shipment_month", "month", "period"),
        "reconciled_at": ("reconciled_at", "as_of_date", "report_date"),
        "reported_month_total": ("reported_month_total", "reported_previous_month_qty"),
        "source": ("source",), "source_file": ("source_file",),
    },
    "inbound": {
        "source_record_key": ("source_record_key", "external_id"),
        "product_code": ("product_code", "sku", "item_code"),
        "source_name": ("source_name", "product_name"),
        "expected_date": ("expected_date", "arrival_date"),
        "quantity": ("quantity", "qty"), "status": ("status",),
        "source": ("source",), "source_file": ("source_file",),
    },
    "ecommerce": {
        "period": ("period", "month", "year_month"),
        "platform": ("platform", "channel", "marketplace"),
        "product_code": ("product_code", "sku", "item_code"),
        "source_name": ("source_name", "product_name", "sku_name"),
        "quantity": ("quantity", "sales_quantity", "qty"),
        "sales_amount": ("sales_amount", "sales_amount_vat_included", "supply_amount",
                         "sales_revenue", "gross_sales", "revenue"),
        "unit_price": ("unit_price", "sales_price", "unit_sales_price",
                       "selling_price_vat_included", "unit_supply_price"),
        "unit_cost": ("unit_cost", "landed_unit_cost", "landed_cost", "cost"),
        "total_cost": ("total_cost", "cost_amount"),
        "source": ("source",), "source_file": ("source_file",),
    },
}


CONFLICTS = {
    "inventory": [("product_code", "report_date")],
    "shipments": [("product_code", "shipment_date", "source"), ("product_code", "shipment_date")],
    "holdings": [("product_code", "hold_month", "as_of_date", "source"),
                 ("product_code", "hold_month", "as_of_date")],
    "costs": [("source_product_code", "as_of_date", "source"),
              ("product_code", "as_of_date", "source")],
    "reconciliations": [("product_code", "shipment_month", "source"),
                        ("product_code", "shipment_month")],
    "inbound": [("source_record_key",)],
    "ecommerce": [("period", "platform", "product_code")],
}


class DashboardSync:
    def __init__(self, client: SupabaseClient | None = None):
        self.client = client or SupabaseClient()
        self._catalog: SchemaCatalog | None = None

    def table_name(self, key: str) -> str:
        env, default = TABLES[key]
        return (os.getenv(env) or default).strip()

    def catalog(self, refresh: bool = False) -> SchemaCatalog:
        if self._catalog is None or refresh:
            payload = self.client.schema()
            self._catalog = SchemaCatalog(payload)
        return self._catalog

    def _table_info(self, key: str) -> tuple[str, str, set[str]]:
        table = self.table_name(key)
        schema, name = self.client.split_table(table, self.client.data_schema)
        return schema, name, self.catalog().columns(schema, name)

    def _table_exists(self, key: str) -> bool:
        schema, name, _ = self._table_info(key)
        return self.catalog().get(schema, name) is not None

    def _build(self, key: str, logical: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        _, _, available = self._table_info(key)
        mapping: dict[str, str] = {}
        row: dict[str, Any] = {}
        for logical_name, aliases in COLUMNS[key].items():
            actual = next((name for name in aliases if name in available), None)
            if actual:
                mapping[logical_name] = actual
                if logical_name in logical:
                    row[actual] = logical[logical_name]
        return row, mapping

    def _conflict(self, key: str, mapping: dict[str, str]) -> list[str]:
        schema, name, _ = self._table_info(key)
        unique_sets = self.catalog().unique_sets(schema, name)
        for logical_set in CONFLICTS[key]:
            if not all(item in mapping for item in logical_set):
                continue
            actual = [mapping[item] for item in logical_set]
            if not unique_sets or set(actual) in unique_sets:
                return actual
        raise SupabaseError(f"{schema}.{name}에 사용할 수 있는 PK/UNIQUE upsert 키가 없습니다.")

    def _upsert_logical(self, key: str, logical_rows: list[dict[str, Any]]) -> int:
        schema, name, available = self._table_info(key)
        if not available:
            return 0
        built: list[dict[str, Any]] = []
        mapping: dict[str, str] = {}
        for logical in logical_rows:
            row, mapping = self._build(key, logical)
            if row:
                built.append(row)
        if not built:
            return 0
        return self.client.upsert(f"{schema}.{name}", built, self._conflict(key, mapping))

    def _product_code_map(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for key in ("products", "aliases"):
            table = self.table_name(key)
            schema, name = self.client.split_table(table, self.client.data_schema)
            columns = self.catalog().columns(schema, name)
            code_col = next((x for x in ("product_code", "sku", "item_code") if x in columns), None)
            name_cols = [x for x in ("alias", "alias_name", "source_name", "external_name",
                                     "product_name", "name", "display_name", "product_key") if x in columns]
            if not code_col or not name_cols:
                continue
            try:
                for row in self.client.select(table, [code_col] + name_cols):
                    code = str(row.get(code_col) or "").strip()
                    if not code:
                        continue
                    for col in name_cols:
                        normalized = _norm(row.get(col))
                        if normalized:
                            result.setdefault(normalized, code)
            except SupabaseError:
                continue
        map_file = BASE_DIR / "data" / "ecom_product_map.json"
        if map_file.exists():
            try:
                for name, code in json.loads(map_file.read_text(encoding="utf-8")).items():
                    if code:
                        result[_norm(name)] = str(code)
            except (ValueError, OSError):
                pass
        return result

    def _record_unmatched(self, source_kind: str, source_file: str,
                          rows: list[dict[str, Any]], reason: str = "product_not_matched") -> int:
        schema, name, cols = self._table_info("unmatched")
        required = {"source_kind", "source_file", "row_key", "source_payload", "reason"}
        if not required.issubset(cols) or not rows:
            return 0
        payload = []
        for row in rows:
            payload.append({"source_kind": source_kind, "source_file": source_file,
                            "row_key": _stable_hash(row), "source_payload": row,
                            "reason": reason, "status": "pending"})
        return self.client.upsert(f"{schema}.{name}", payload,
                                  ["source_kind", "source_file", "row_key"])

    def _record_run(self, kind: str, source_file: str, status: str,
                    counts: dict[str, Any], error: str | None = None) -> None:
        schema, name, cols = self._table_info("runs")
        if "run_key" not in cols:
            return
        row = {"run_key": _stable_hash([kind, source_file, counts]), "source_kind": kind,
               "source_file": source_file, "status": status, "counts": counts,
               "error": error}
        self.client.upsert(f"{schema}.{name}", [{k: v for k, v in row.items() if k in cols}],
                           ["run_key"])

    def status(self) -> dict[str, Any]:
        if not self.client.configured:
            return {"configured": False, "connected": False,
                    "message": "SUPABASE_URL과 서버용 키를 .env에 설정하세요."}
        try:
            schema = self.client.schema()
            return {"configured": True, "connected": True,
                    "tables": len(schema.get("tables") or []), "message": "Supabase 연결됨"}
        except Exception as exc:
            return {"configured": True, "connected": False, "message": str(exc)}

    def sync_erp_plan(self, plan: dict[str, Any], source_file: str,
                      products: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.client.configured:
            return {"configured": False, "ok": False,
                    "message": "로컬 반영은 완료됐지만 Supabase 환경변수가 없어 DB 저장은 건너뛰었습니다."}
        self.catalog(refresh=True)
        products = products or {}
        date = str(plan.get("기준일") or "")
        ym = str(plan.get("월") or date[:7])
        matched = plan.get("changes") or []
        counts: dict[str, Any] = {"matched": len(matched), "unmatched": len(plan.get("unmatched") or [])}

        def product_code(change: dict[str, Any]) -> str:
            key = str(change.get("품목키") or "")
            return str(change.get("품목코드") or (products.get(key) or {}).get("code") or "").strip()

        inventory, shipments, holdings, costs, reconciliations, inbound = [], [], [], [], [], []
        negative_rows = []
        previous: dict[str, float] = {}
        # Read previous MTD snapshots in one request when the compatible columns exist.
        inv_schema, inv_name, inv_cols = self._table_info("inventory")
        inv_map = {logical: next((x for x in aliases if x in inv_cols), None)
                   for logical, aliases in COLUMNS["inventory"].items()}
        code_col, date_col = inv_map.get("product_code"), inv_map.get("report_date")
        mtd_col = inv_map.get("actual_month_to_date_shipment") or inv_map.get("month_to_date_shipment")
        codes = sorted({product_code(c) for c in matched if product_code(c)})
        if code_col and date_col and mtd_col and codes:
            try:
                rows = self.client.select(f"{inv_schema}.{inv_name}", [code_col, date_col, mtd_col], query={
                    date_col: "lt." + date, code_col: "in.(" + ",".join(codes) + ")",
                    "order": date_col + ".desc", "limit": 5000,
                })
                for row in rows:
                    if str(row.get(date_col) or "")[:7] != ym:
                        continue
                    code = str(row.get(code_col) or "")
                    if code not in previous and row.get(mtd_col) is not None:
                        previous[code] = float(row[mtd_col])
            except SupabaseError:
                previous = {}

        for change in matched:
            code = product_code(change)
            if not code:
                continue
            raw_mtd = int(change.get("금월출고") or 0)
            actual_mtd = int(change.get("실제출고") or 0)
            inventory.append({"product_code": code, "report_date": date,
                              "stock_quantity": change.get("당일재고", change.get("재고실사")),
                              "wip_quantity": int(change.get("작업중") or 0),
                              "holding_this_month": int(change.get("홀딩_이번달") or change.get("홀딩") or 0),
                              "month_to_date_shipment": raw_mtd,
                              "actual_month_to_date_shipment": actual_mtd,
                              "available_stock": change.get("가용재고"),
                              "source": "inventory_report", "source_file": source_file,
                              "source_name": change.get("ERP품목")})
            current_for_delta = actual_mtd if inv_map.get("actual_month_to_date_shipment") else raw_mtd
            delta = current_for_delta - int(previous.get(code, 0))
            note = None
            if delta < 0:
                note = f"누계가 직전 스냅샷보다 {abs(delta)}개 감소하여 일별 출고는 0으로 저장"
                negative_rows.append({"product_code": code, "report_date": date,
                                      "current_mtd": current_for_delta, "previous_mtd": previous.get(code),
                                      "reason": note})
                delta = 0
            shipments.append({"product_code": code, "shipment_date": date, "quantity": delta,
                              "source": "inventory_report", "source_file": source_file, "note": note})
            holdings.append({"product_code": code, "hold_month": _month_start(ym),
                             "as_of_date": date,
                             "quantity": int(change.get("홀딩_이번달") or change.get("홀딩") or 0),
                             "source": "inventory_report", "source_file": source_file})
            if change.get("원가") is not None:
                costs.append({"product_code": code, "source_product_code": change.get("품목코드") or code,
                              "source_name": change.get("ERP품목"), "as_of_date": date,
                              "unit_cost": change.get("원가"), "source": "inventory_report",
                              "source_file": source_file})
            if change.get("전월출고") is not None:
                reconciliations.append({"product_code": code, "shipment_month": _month_start(_previous_month(ym)),
                                        "reconciled_at": date,
                                        "reported_month_total": change.get("전월출고"),
                                        "source": "inventory_report", "source_file": source_file})
            for index, entry in enumerate(change.get("입고예정") or []):
                for detail_index, detail in enumerate(entry.get("내역") or []):
                    expected = str(detail.get("d") or "")
                    key = f"erp-inbound:{code}:{expected}:{index}:{detail_index}"
                    inbound.append({"source_record_key": key, "product_code": code,
                                    "source_name": change.get("ERP품목"), "expected_date": expected,
                                    "quantity": detail.get("q"), "status": "confirmed",
                                    "source": "inventory_report", "source_file": source_file})

        unmatched = list(plan.get("unmatched") or []) + negative_rows
        try:
            targets = [("inventory", inventory), ("shipments", shipments),
                       ("holdings", holdings), ("costs", costs),
                       ("reconciliations", reconciliations), ("inbound", inbound)]
            missing = [self.table_name(key) for key, rows in targets if rows and not self._table_exists(key)]
            if unmatched and not self._table_exists("unmatched"):
                missing.append(self.table_name("unmatched"))
            counts["daily_inventory"] = self._upsert_logical("inventory", inventory)
            counts["daily_shipments"] = self._upsert_logical("shipments", shipments)
            counts["holding_snapshots"] = self._upsert_logical("holdings", holdings)
            counts["inventory_cost_snapshots"] = self._upsert_logical("costs", costs)
            counts["shipment_month_reconciliations"] = self._upsert_logical("reconciliations", reconciliations)
            counts["inbound_orders"] = self._upsert_logical("inbound", inbound)
            counts["unmatched_queue"] = self._record_unmatched("erp_inventory", source_file, unmatched)
            if missing:
                counts["missing_tables"] = missing
            self._record_run("erp_inventory", source_file, "partial" if missing else "success", counts)
            return {"configured": True, "ok": not missing, "counts": counts,
                    "message": ("일부 대상 테이블이 없어 부분 저장했습니다: " + ", ".join(missing))
                    if missing else "Supabase 자동 저장 완료"}
        except Exception as exc:
            try:
                self._record_run("erp_inventory", source_file, "failed", counts, str(exc))
            except Exception:
                pass
            return {"configured": True, "ok": False, "counts": counts, "message": str(exc)}

    def sync_ecommerce(self, changes: list[dict[str, Any]], unmatched: list[dict[str, Any]],
                       source_file: str) -> dict[str, Any]:
        if not self.client.configured:
            return {"configured": False, "ok": False,
                    "message": "화면 반영은 완료됐지만 Supabase 환경변수가 없어 DB 저장은 건너뛰었습니다."}
        self.catalog(refresh=True)
        product_map = self._product_code_map()
        rows, unresolved = [], list(unmatched or [])
        for change in changes or []:
            platform = str(change.get("플랫폼") or "")
            sku_name = str(change.get("SKU") or "")
            code = (change.get("품목코드") or product_map.get(_norm(platform + "|" + sku_name)) or
                    product_map.get(_norm(sku_name)))
            if not code:
                unresolved.append(change)
                continue
            qty = int(change.get("수량") or 0)
            amount = int(change.get("공급가액") or 0)
            rows.append({"period": _month_start(str(change.get("연월") or "")),
                         "platform": platform, "product_code": str(code),
                         "source_name": sku_name, "quantity": qty,
                         "sales_amount": amount,
                         "unit_price": round(amount / qty, 4) if qty else 0,
                         "unit_cost": change.get("원가"), "source": "ecommerce_transaction",
                         "total_cost": ((change.get("원가") or 0) * qty)
                         if change.get("원가") is not None else None,
                         "source_file": source_file})
        counts = {"matched": len(rows), "unmatched": len(unresolved)}
        try:
            missing = []
            if rows and not self._table_exists("ecommerce"):
                missing.append(self.table_name("ecommerce"))
            if unresolved and not self._table_exists("unmatched"):
                missing.append(self.table_name("unmatched"))
            counts["ecommerce_sku_monthly"] = self._upsert_logical("ecommerce", rows)
            counts["unmatched_queue"] = self._record_unmatched("ecommerce", source_file, unresolved)
            if missing:
                counts["missing_tables"] = missing
            self._record_run("ecommerce", source_file, "partial" if missing else "success", counts)
            return {"configured": True, "ok": not missing, "counts": counts,
                    "message": ("일부 대상 테이블이 없어 부분 저장했습니다: " + ", ".join(missing))
                    if missing else "Supabase 자동 저장 완료"}
        except Exception as exc:
            try:
                self._record_run("ecommerce", source_file, "failed", counts, str(exc))
            except Exception:
                pass
            return {"configured": True, "ok": False, "counts": counts, "message": str(exc)}


_SYNC: DashboardSync | None = None


def get_sync() -> DashboardSync:
    global _SYNC
    if _SYNC is None:
        _SYNC = DashboardSync()
    return _SYNC


def get_schema() -> dict[str, Any]:
    return get_sync().client.schema()


def get_status() -> dict[str, Any]:
    return get_sync().status()


def sync_erp_plan(plan: dict[str, Any], source_file: str,
                  products: dict[str, Any] | None = None) -> dict[str, Any]:
    return get_sync().sync_erp_plan(plan, source_file, products)


def sync_ecommerce(changes: list[dict[str, Any]], unmatched: list[dict[str, Any]],
                   source_file: str) -> dict[str, Any]:
    return get_sync().sync_ecommerce(changes, unmatched, source_file)
