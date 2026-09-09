# -*- coding: utf-8 -*-
"""UB Korea dashboard -> Supabase synchronization.

Server-side only.  Supabase secrets stay in .env and never reach browser JS.

ERP inventory upload writes to canonical tables:
- erp_daily_snapshots: parsed daily ERP audit snapshot (matched products only)
- inventory_snapshots: canonical current inventory used by analytics.latest_inventory
- daily_shipments: positive day delta only
- holding_snapshots: current-month holding snapshot
- inventory_cost_snapshots: ERP displayed cost snapshot
- shipment_month_reconciliations: previous-month authoritative total via RPC
- inbound_orders: confirmed ERP inbound schedule
- dashboard_import_unmatched: unmatched rows + negative MTD corrections
- dashboard_import_runs: one idempotent run log per source file
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
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

BASE_DIR = Path(__file__).resolve().parent


def _load_env_file(path: Path | None = None) -> None:
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
        self.payload = payload or {}
        self.tables: dict[tuple[str, str], dict[str, Any]] = {}
        for table in self.payload.get("tables") or []:
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
        out: list[set[str]] = []
        pk = {str(x) for x in (item.get("primary_key") or []) if x}
        if pk:
            out.append(pk)
        for con in item.get("unique_constraints") or []:
            cols = {str(x) for x in (con.get("columns") or []) if x}
            if cols:
                out.append(cols)
        return out


class SupabaseClient:
    """Small PostgREST client; intentionally standard-library only."""

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
        headers = {"apikey": self.key, "Accept": "application/json"}
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
            a, b = value.split(".", 1)
            return a, b
        return default_schema, value

    def schema(self) -> dict[str, Any]:
        if not self.configured:
            return {"configured": False, "tables": [],
                    "error": "프로젝트의 .env에 SUPABASE_URL과 서버용 키를 넣어주세요."}
        result = self._request("/rest/v1/rpc/dashboard_schema", method="POST",
                               body={"p_schemas": self.schema_names}, schema="public")
        if not isinstance(result, dict):
            raise SupabaseError("Supabase 스키마 응답 형식을 확인할 수 없습니다.")
        result["configured"] = True
        result["source"] = "dashboard_schema RPC"
        return result

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

    def rpc(self, name: str, body: dict[str, Any]) -> Any:
        return self._request("/rest/v1/rpc/" + urllib.parse.quote(name), method="POST",
                             body=body, schema="public")


TABLES = {
    "erp_snapshots": ("SUPABASE_TABLE_ERP_DAILY", "erp_daily_snapshots"),
    "inventory": ("SUPABASE_TABLE_INVENTORY", "inventory_snapshots"),
    "legacy_daily_inventory": ("SUPABASE_TABLE_DAILY_INVENTORY", "daily_inventory"),
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
    "erp_snapshots": {
        "product_code": ("product_code",), "snapshot_date": ("snapshot_date",),
        "month_to_date_shipped_quantity": ("month_to_date_shipped_quantity",),
        "holding_quantity": ("holding_quantity",),
        "holding_other_month_quantity": ("holding_other_month_quantity",),
        "effective_shipment_cumulative": ("effective_shipment_cumulative",),
        "daily_shipment_delta": ("daily_shipment_delta",),
        "stock_quantity": ("stock_quantity",), "wip_quantity": ("wip_quantity",),
        "source": ("source",), "source_file": ("source_file",), "raw_payload": ("raw_payload",),
    },
    "inventory": {
        "product_code": ("product_code",), "snapshot_date": ("snapshot_date",),
        "stock_quantity": ("stock_quantity",), "wip_quantity": ("wip_quantity",),
        "source": ("source",),
    },
    "legacy_daily_inventory": {
        "product_code": ("product_code",), "report_date": ("report_date",),
        "month_to_date_shipped_quantity": ("month_to_date_shipped_quantity",),
        "stock_quantity": ("stock_quantity",),
    },
    "shipments": {
        "product_code": ("product_code",), "shipment_date": ("shipment_date",),
        "quantity": ("quantity",), "source": ("source",), "note": ("note",),
    },
    "holdings": {
        "product_code": ("product_code",), "hold_month": ("hold_month",),
        "as_of_date": ("as_of_date",), "quantity": ("quantity",),
        "source": ("source",), "source_file": ("source_file",), "note": ("note",),
    },
    "costs": {
        "raw_product_code": ("raw_product_code", "source_product_code", "erp_product_code"),
        "product_code": ("product_code",), "as_of_date": ("as_of_date",),
        "unit_cost_krw": ("unit_cost_krw", "unit_cost", "inventory_unit_cost", "cost"),
        "source_name": ("source_name", "product_name"), "source": ("source",),
        "source_file": ("source_file",), "mapping_status": ("mapping_status",), "note": ("note",),
    },
    "inbound": {
        "source_record_key": ("source_record_key",), "product_code": ("product_code",),
        "source_name": ("source_name",), "expected_period": ("expected_period",),
        "expected_date": ("expected_date",), "timing_label": ("timing_label",),
        "quantity": ("quantity",), "import_method": ("import_method",),
        "status": ("status",), "source": ("source",), "received_quantity": ("received_quantity",),
    },
    "ecommerce": {
        "period": ("period",), "platform": ("platform",), "product_code": ("product_code",),
        "source_name": ("sku_name", "source_name", "product_name"),
        "quantity": ("quantity_sold", "quantity", "sales_quantity", "qty"),
        "sales_amount": ("revenue", "sales_amount", "sales_amount_vat_included", "supply_amount"),
        "unit_price": ("supply_price_vat_incl", "unit_price", "sales_price", "unit_supply_price"),
        "unit_cost": ("unit_cost", "landed_unit_cost", "landed_cost", "cost"),
        "total_cost": ("total_cost", "cost_amount"), "source": ("source",),
    },
}

CONFLICTS = {
    "erp_snapshots": [("product_code", "snapshot_date", "source")],
    "inventory": [("product_code", "snapshot_date", "source")],
    "shipments": [("product_code", "shipment_date", "source")],
    "holdings": [("product_code", "hold_month", "as_of_date", "source")],
    "costs": [("raw_product_code", "as_of_date", "source")],
    "inbound": [("source_record_key",)],
    "ecommerce": [("period", "platform", "product_code")],
}


class DashboardSync:
    def __init__(self, client: SupabaseClient | None = None):
        self.client = client or SupabaseClient()
        self._catalog: SchemaCatalog | None = None

    def catalog(self, refresh: bool = False) -> SchemaCatalog:
        if self._catalog is None or refresh:
            self._catalog = SchemaCatalog(self.client.schema())
        return self._catalog

    def table_name(self, key: str) -> str:
        env, default = TABLES[key]
        return (os.getenv(env) or default).strip()

    def _table_info(self, key: str) -> tuple[str, str, set[str]]:
        schema, name = self.client.split_table(self.table_name(key), self.client.data_schema)
        return schema, name, self.catalog().columns(schema, name)

    def _table_exists(self, key: str) -> bool:
        schema, name, _ = self._table_info(key)
        return self.catalog().get(schema, name) is not None

    def _legacy_inventory_name(self, date: str) -> str | None:
        # Used only as a read fallback for the first erp_daily_snapshots run.
        env = os.getenv("SUPABASE_TABLE_DAILY_INVENTORY")
        if env:
            return env.strip()
        candidate = "daily_inventory_" + date[:7].replace("-", "")
        if self.catalog().get(self.client.data_schema, candidate):
            return candidate
        if self.catalog().get(self.client.data_schema, "daily_inventory"):
            return "daily_inventory"
        return None

    def _build(self, key: str, logical: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        _, _, available = self._table_info(key)
        row: dict[str, Any] = {}
        mapping: dict[str, str] = {}
        for logical_name, aliases in COLUMNS[key].items():
            actual = next((x for x in aliases if x in available), None)
            if actual:
                mapping[logical_name] = actual
                if logical_name in logical:
                    row[actual] = logical[logical_name]
        return row, mapping

    def _conflict(self, key: str, mapping: dict[str, str]) -> list[str]:
        schema, name, _ = self._table_info(key)
        unique_sets = self.catalog().unique_sets(schema, name)
        for logical_set in CONFLICTS[key]:
            if not all(x in mapping for x in logical_set):
                continue
            actual = [mapping[x] for x in logical_set]
            if set(actual) in unique_sets:
                return actual
        raise SupabaseError(f"{schema}.{name}에 사용할 수 있는 PK/UNIQUE upsert 키가 없습니다.")

    def _upsert_logical(self, key: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        schema, name, available = self._table_info(key)
        if not available:
            raise SupabaseError(f"{schema}.{name} 테이블이 없습니다.")
        built: list[dict[str, Any]] = []
        mapping: dict[str, str] = {}
        for logical in rows:
            row, mapping = self._build(key, logical)
            if row:
                built.append(row)
        if not built:
            return 0
        return self.client.upsert(f"{schema}.{name}", built, self._conflict(key, mapping))

    def _record_unmatched(self, source_kind: str, source_file: str,
                          rows: list[dict[str, Any]], reason: str) -> int:
        if not rows or not self._table_exists("unmatched"):
            return 0
        schema, name, cols = self._table_info("unmatched")
        payload = [{"source_kind": source_kind, "source_file": source_file,
                    "row_key": _stable_hash(row), "source_payload": row,
                    "reason": reason, "status": "pending"} for row in rows]
        payload = [{k: v for k, v in row.items() if k in cols} for row in payload]
        return self.client.upsert(f"{schema}.{name}", payload,
                                  ["source_kind", "source_file", "row_key"])

    def _record_run(self, kind: str, source_file: str, status: str,
                    counts: dict[str, Any], error: str | None = None) -> None:
        if not self._table_exists("runs"):
            return
        schema, name, cols = self._table_info("runs")
        # Stable per file: a failed retry becomes success instead of a second run row.
        row = {"run_key": _stable_hash([kind, source_file]), "source_kind": kind,
               "source_file": source_file, "status": status, "counts": counts, "error": error}
        self.client.upsert(f"{schema}.{name}", [{k: v for k, v in row.items() if k in cols}], ["run_key"])

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

    def _previous_effective(self, date: str, ym: str, codes: list[str]) -> dict[str, float]:
        previous: dict[str, float] = {}
        if not codes:
            return previous
        # Preferred source: new ERP audit table.
        if self._table_exists("erp_snapshots"):
            try:
                rows = self.client.select(self.table_name("erp_snapshots"),
                                          ["product_code", "snapshot_date", "effective_shipment_cumulative"],
                                          query={"snapshot_date": "lt." + date,
                                                 "product_code": "in.(" + ",".join(codes) + ")",
                                                 "order": "snapshot_date.desc", "limit": 5000})
                for row in rows:
                    if str(row.get("snapshot_date") or "")[:7] != ym:
                        continue
                    code = str(row.get("product_code") or "")
                    if code and code not in previous and row.get("effective_shipment_cumulative") is not None:
                        previous[code] = float(row["effective_shipment_cumulative"])
            except SupabaseError:
                pass
        # First-run bridge: historical September snapshots live in daily_inventory_YYYYMM.
        missing = [c for c in codes if c not in previous]
        legacy = self._legacy_inventory_name(date)
        if missing and legacy:
            try:
                rows = self.client.select(legacy,
                                          ["product_code", "report_date", "month_to_date_shipped_quantity"],
                                          query={"report_date": "lt." + date,
                                                 "product_code": "in.(" + ",".join(missing) + ")",
                                                 "order": "report_date.desc", "limit": 5000})
                for row in rows:
                    if str(row.get("report_date") or "")[:7] != ym:
                        continue
                    code = str(row.get("product_code") or "")
                    if code and code not in previous and row.get("month_to_date_shipped_quantity") is not None:
                        previous[code] = float(row["month_to_date_shipped_quantity"])
            except SupabaseError:
                pass
        return previous

    def _reconcile_inbound(self, rows: list[dict[str, Any]], as_of_date: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Merge ERP confirmed inbound with the existing planning rows.

        The import-history sheet already creates ``planned`` rows.  Creating a second
        ERP row for the same physical shipment would double-count analytics inbound.
        Matching is therefore one-to-one:
        1) exact product + date + quantity,
        2) same product + quantity with a changed date (nearest unused row),
        3) otherwise a genuinely new ERP inbound row.
        """
        if not rows or not self._table_exists("inbound"):
            return rows, {"exact": 0, "rescheduled": 0, "new": len(rows)}
        codes = sorted({str(r.get("product_code") or "") for r in rows if r.get("product_code")})
        try:
            existing = self.client.select(
                self.table_name("inbound"),
                ["source_record_key", "product_code", "expected_date", "quantity",
                 "status", "source", "received_quantity"],
                query={"product_code": "in.(" + ",".join(codes) + ")",
                       "status": "in.(planned,confirmed,partial)",
                       "expected_date": "gte." + as_of_date[:7] + "-01",
                       "limit": 5000},
            )
        except SupabaseError:
            existing = []

        used: set[str] = set()
        exact = rescheduled = 0
        pending: list[dict[str, Any]] = []

        def apply_existing(row: dict[str, Any], old: dict[str, Any], changed_date: bool) -> None:
            nonlocal exact, rescheduled
            key = str(old.get("source_record_key") or "")
            used.add(key)
            row["source_record_key"] = key
            row["source"] = old.get("source") or row.get("source")
            received = float(old.get("received_quantity") or 0)
            row["received_quantity"] = received
            if received > 0:
                row["status"] = "received" if received >= float(row.get("quantity") or 0) else "partial"
            else:
                row["status"] = "confirmed"
            if changed_date:
                rescheduled += 1
            else:
                exact += 1

        # Exact matches first so a later same-quantity row cannot steal them.
        for row in rows:
            hit = next((old for old in existing
                        if str(old.get("source_record_key") or "") not in used
                        and str(old.get("product_code") or "") == str(row.get("product_code") or "")
                        and str(old.get("expected_date") or "") == str(row.get("expected_date") or "")
                        and float(old.get("quantity") or 0) == float(row.get("quantity") or 0)), None)
            if hit:
                apply_existing(row, hit, False)
            else:
                pending.append(row)

        # Date changes: only same product + same quantity.  Never fuzzy-match quantity.
        for row in pending:
            candidates = [old for old in existing
                          if str(old.get("source_record_key") or "") not in used
                          and str(old.get("product_code") or "") == str(row.get("product_code") or "")
                          and float(old.get("quantity") or 0) == float(row.get("quantity") or 0)]
            if candidates:
                try:
                    target = _dt.date.fromisoformat(str(row.get("expected_date")))
                    candidates.sort(key=lambda old: abs((_dt.date.fromisoformat(str(old.get("expected_date"))) - target).days))
                except (TypeError, ValueError):
                    pass
                apply_existing(row, candidates[0], True)

        return rows, {"exact": exact, "rescheduled": rescheduled,
                      "new": len(rows) - exact - rescheduled}

    def sync_erp_plan(self, plan: dict[str, Any], source_file: str,
                      products: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.client.configured:
            return {"configured": False, "ok": False,
                    "message": "로컬 반영은 완료됐지만 Supabase 환경변수가 없어 DB 저장은 건너뛰었습니다."}
        self.catalog(refresh=True)
        products = products or {}
        date = str(plan.get("기준일") or "")
        ym = str(plan.get("월") or date[:7])
        matched = list(plan.get("changes") or [])
        unmatched = list(plan.get("unmatched") or [])
        counts: dict[str, Any] = {"matched": len(matched), "unmatched": len(unmatched)}

        def code_for(change: dict[str, Any]) -> str:
            key = str(change.get("품목키") or "")
            return str(change.get("품목코드") or (products.get(key) or {}).get("code") or "").strip()

        codes = sorted({code_for(c) for c in matched if code_for(c)})
        previous = self._previous_effective(date, ym, codes)

        erp_rows: list[dict[str, Any]] = []
        inv_rows: list[dict[str, Any]] = []
        shipment_rows: list[dict[str, Any]] = []
        holding_rows: list[dict[str, Any]] = []
        cost_rows: list[dict[str, Any]] = []
        recon_args: list[dict[str, Any]] = []
        inbound_rows: list[dict[str, Any]] = []
        negative_rows: list[dict[str, Any]] = []
        inbound_occurrence: defaultdict[tuple[str, str, int], int] = defaultdict(int)

        for change in matched:
            code = code_for(change)
            if not code:
                continue
            raw_mtd = int(change.get("금월출고") or 0)
            effective_mtd = int(change.get("실제출고") or raw_mtd)
            prev = previous.get(code)
            signed_delta = None if prev is None else effective_mtd - int(prev)

            if signed_delta is not None and signed_delta < 0:
                negative_rows.append({"product_code": code, "report_date": date,
                                      "current_mtd": effective_mtd, "previous_mtd": prev,
                                      "delta": signed_delta,
                                      "reason": "월누계가 직전 스냅샷보다 감소"})
            elif signed_delta is not None and signed_delta > 0:
                shipment_rows.append({"product_code": code, "shipment_date": date,
                                      "quantity": signed_delta, "source": "inventory_report",
                                      "note": None})

            erp_rows.append({"product_code": code, "snapshot_date": date,
                             "month_to_date_shipped_quantity": raw_mtd,
                             "holding_quantity": int(change.get("홀딩_이번달") or change.get("홀딩") or 0),
                             "holding_other_month_quantity": int(change.get("홀딩_다른달") or 0),
                             "effective_shipment_cumulative": effective_mtd,
                             "daily_shipment_delta": signed_delta,
                             "stock_quantity": change.get("당일재고", change.get("재고실사")),
                             "wip_quantity": int(change.get("작업중") or 0),
                             "source": "erp_inventory_report", "source_file": source_file,
                             "raw_payload": change})
            inv_rows.append({"product_code": code, "snapshot_date": date,
                             "stock_quantity": change.get("당일재고", change.get("재고실사")),
                             "wip_quantity": int(change.get("작업중") or 0),
                             "source": "inventory_report_" + date.replace("-", "")})
            holding_rows.append({"product_code": code, "hold_month": _month_start(ym),
                                 "as_of_date": date,
                                 "quantity": int(change.get("홀딩_이번달") or change.get("홀딩") or 0),
                                 "source": "inventory_report", "source_file": source_file})
            if change.get("원가") is not None:
                cost_rows.append({"raw_product_code": change.get("품목코드") or code,
                                  "product_code": code, "as_of_date": date,
                                  "unit_cost_krw": change.get("원가"),
                                  "source_name": change.get("ERP품목"), "source": "inventory_report",
                                  "source_file": source_file, "mapping_status": "matched"})
            if change.get("전월출고") is not None:
                recon_args.append({"p_raw_product_code": change.get("품목코드") or code,
                                   "p_shipment_month": _month_start(_previous_month(ym)),
                                   "p_reconciled_at": date,
                                   "p_reported_month_total": change.get("전월출고"),
                                   "p_source_file": source_file,
                                   "p_note": "dashboard ERP sync"})

            for entry in change.get("입고예정") or []:
                for detail in entry.get("내역") or []:
                    expected = str(detail.get("d") or "")
                    qty = int(detail.get("q") or 0)
                    if not expected or qty <= 0:
                        continue
                    triple = (code, expected, qty)
                    inbound_occurrence[triple] += 1
                    occurrence = inbound_occurrence[triple]
                    key = f"erp-inbound:{code}:{expected}:{qty}:{occurrence}"
                    inbound_rows.append({"source_record_key": key, "product_code": code,
                                         "source_name": change.get("ERP품목"),
                                         "expected_period": expected[:7] + "-01",
                                         "expected_date": expected,
                                         "timing_label": entry.get("시점"), "quantity": qty,
                                         "import_method": "일반", "status": "confirmed",
                                         "source": "inventory_report", "received_quantity": 0})

        inbound_rows, inbound_stats = self._reconcile_inbound(inbound_rows, date)
        counts["inbound_exact_existing"] = inbound_stats["exact"]
        counts["inbound_rescheduled_existing"] = inbound_stats["rescheduled"]
        counts["inbound_new"] = inbound_stats["new"]

        try:
            required = ["erp_snapshots", "inventory", "shipments", "holdings", "costs", "inbound"]
            missing = [self.table_name(k) for k in required if not self._table_exists(k)]
            if missing:
                raise SupabaseError("필수 대상 테이블이 없습니다: " + ", ".join(missing))

            counts["erp_daily_snapshots"] = self._upsert_logical("erp_snapshots", erp_rows)
            counts["inventory_snapshots"] = self._upsert_logical("inventory", inv_rows)
            counts["daily_shipments"] = self._upsert_logical("shipments", shipment_rows)
            counts["holding_snapshots"] = self._upsert_logical("holdings", holding_rows)
            counts["inventory_cost_snapshots"] = self._upsert_logical("costs", cost_rows)
            counts["inbound_orders"] = self._upsert_logical("inbound", inbound_rows)

            recon_ok = 0
            for args in recon_args:
                self.client.rpc("record_monthly_shipment_reconciliation", args)
                recon_ok += 1
            counts["shipment_month_reconciliations"] = recon_ok
            counts["unmatched_products"] = self._record_unmatched("erp_inventory", source_file,
                                                                    unmatched, "product_not_matched")
            counts["negative_mtd_corrections"] = self._record_unmatched("erp_inventory", source_file,
                                                                          negative_rows, "mtd_decreased")
            counts["baseline_without_previous"] = sum(1 for c in codes if c not in previous)
            self._record_run("erp_inventory", source_file, "success", counts)
            return {"configured": True, "ok": True, "counts": counts,
                    "message": "Supabase 자동 저장 완료"}
        except Exception as exc:
            try:
                self._record_run("erp_inventory", source_file, "failed", counts, str(exc))
            except Exception:
                pass
            return {"configured": True, "ok": False, "counts": counts, "message": str(exc)}

    def _product_code_map(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for key in ("products", "aliases"):
            schema, name = self.client.split_table(self.table_name(key), self.client.data_schema)
            columns = self.catalog().columns(schema, name)
            code_col = "product_code" if "product_code" in columns else None
            name_cols = [x for x in ("source_name", "standard_name", "alias", "name", "display_name")
                         if x in columns]
            if not code_col or not name_cols:
                continue
            try:
                for row in self.client.select(self.table_name(key), [code_col] + name_cols):
                    code = str(row.get(code_col) or "").strip()
                    for col in name_cols:
                        name = _norm(row.get(col))
                        if code and name:
                            result.setdefault(name, code)
            except SupabaseError:
                pass
        map_file = BASE_DIR / "data" / "ecom_product_map.json"
        if map_file.exists():
            try:
                for name, code in json.loads(map_file.read_text(encoding="utf-8")).items():
                    if code:
                        result[_norm(name)] = str(code)
            except (OSError, ValueError):
                pass
        return result

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
            cost = change.get("원가")
            rows.append({"period": _month_start(str(change.get("연월") or "")),
                         "platform": platform, "product_code": str(code), "source_name": sku_name,
                         "quantity": qty, "sales_amount": amount,
                         "unit_price": round(amount / qty, 4) if qty else 0,
                         "unit_cost": cost, "total_cost": (cost * qty) if cost is not None else None,
                         "source": "ecommerce_transaction"})
        counts = {"matched": len(rows), "unmatched": len(unresolved)}
        try:
            counts["ecommerce_sku_monthly"] = self._upsert_logical("ecommerce", rows)
            counts["unmatched_queue"] = self._record_unmatched("ecommerce", source_file,
                                                                 unresolved, "product_not_matched")
            self._record_run("ecommerce", source_file, "success", counts)
            return {"configured": True, "ok": True, "counts": counts,
                    "message": "Supabase 자동 저장 완료"}
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
