import unittest

from supabase_sync import DashboardSync, SchemaCatalog, SupabaseClient


def table(name, columns, primary_key=None):
    return {
        "schema": "public",
        "name": name,
        "type": "table",
        "columns": [{"name": col, "data_type": "text", "nullable": True} for col in columns],
        "primary_key": primary_key or [],
        "unique_constraints": [],
        "foreign_keys": [],
    }


class FakeClient:
    configured = True
    data_schema = "public"
    schema_names = ["public", "analytics"]
    split_table = staticmethod(SupabaseClient.split_table)

    def __init__(self):
        self.writes = []
        self.payload = {"tables": [
            table("daily_inventory", [
                "product_code", "report_date", "stock_quantity", "wip_quantity",
                "holding_this_month", "month_to_date_shipment", "actual_month_to_date_shipment",
                "available_stock", "source", "source_file", "source_name"
            ], ["product_code", "report_date"]),
            table("daily_shipments", [
                "product_code", "shipment_date", "quantity", "source", "source_file", "note"
            ], ["product_code", "shipment_date", "source"]),
            table("holding_snapshots", [
                "product_code", "hold_month", "as_of_date", "quantity", "source", "source_file", "note"
            ], ["product_code", "hold_month", "as_of_date", "source"]),
            table("inventory_cost_snapshots", [
                "product_code", "source_product_code", "source_name", "as_of_date", "unit_cost",
                "source", "source_file"
            ], ["source_product_code", "as_of_date", "source"]),
            table("shipment_month_reconciliations", [
                "product_code", "shipment_month", "reconciled_at", "reported_month_total",
                "source", "source_file"
            ], ["product_code", "shipment_month", "source"]),
            table("inbound_orders", [
                "source_record_key", "product_code", "source_name", "expected_date", "quantity",
                "status", "source", "source_file"
            ], ["source_record_key"]),
            table("ecommerce_sku_monthly", [
                "period", "platform", "product_code", "source_name", "quantity",
                "sales_amount", "unit_price", "unit_cost", "total_cost", "source", "source_file"
            ], ["period", "platform", "product_code"]),
            table("products", ["product_code", "product_name"], ["product_code"]),
            table("product_aliases", ["product_code", "alias_name"], ["product_code", "alias_name"]),
            table("dashboard_import_unmatched", [
                "source_kind", "source_file", "row_key", "source_payload", "reason", "status"
            ], ["source_kind", "source_file", "row_key"]),
            table("dashboard_import_runs", [
                "run_key", "source_kind", "source_file", "status", "counts", "error"
            ], ["run_key"]),
        ]}

    def schema(self):
        return self.payload

    def select(self, table_name, columns, query=None):
        if table_name.endswith("product_aliases"):
            return [{"product_code": "BOIL0058", "alias_name": "쿠팡 아보카도 SKU"}]
        return []

    def upsert(self, table_name, rows, conflict):
        self.writes.append((table_name, rows, conflict))
        return len(rows)


class SupabaseSyncTests(unittest.TestCase):
    def test_catalog_reads_primary_and_unique_keys(self):
        catalog = SchemaCatalog({"tables": [table("items", ["id", "name"], ["id"])]})
        self.assertEqual(catalog.columns("public", "items"), {"id", "name"})
        self.assertIn({"id"}, catalog.unique_sets("public", "items"))

    def test_ecommerce_upsert_uses_alias_and_unique_key(self):
        client = FakeClient()
        sync = DashboardSync(client)
        result = sync.sync_ecommerce([
            {"플랫폼": "쿠팡", "SKU": "쿠팡 아보카도 SKU", "연월": "2026-09",
             "수량": 10, "공급가액": 61000, "원가": 2854}
        ], [], "거래내역.xls")

        self.assertTrue(result["ok"])
        ecommerce_write = next(item for item in client.writes if item[0].endswith("ecommerce_sku_monthly"))
        row = ecommerce_write[1][0]
        self.assertEqual(row["period"], "2026-09-01")
        self.assertEqual(row["product_code"], "BOIL0058")
        self.assertEqual(row["total_cost"], 28540)
        self.assertEqual(ecommerce_write[2], ["period", "platform", "product_code"])

    def test_erp_plan_writes_each_existing_business_table(self):
        client = FakeClient()
        sync = DashboardSync(client)
        plan = {
            "기준일": "2026-09-07", "월": "2026-09", "전월": "2026-08",
            "changes": [{
                "품목키": "테스트", "품목코드": "SKU-001", "ERP품목": "테스트 상품",
                "금월출고": 25, "실제출고": 22, "당일재고": 100, "작업중": 5,
                "재고실사": 105, "가용재고": 92, "홀딩": 3, "홀딩_이번달": 3,
                "원가": 1234, "전월출고": 80,
                "입고예정": [{"월": "2026-10", "수량": 20,
                              "내역": [{"d": "2026-10-10", "q": 20}]}],
            }],
            "unmatched": [],
        }
        result = sync.sync_erp_plan(plan, "재고.xls", {"테스트": {"code": "SKU-001"}})

        self.assertTrue(result["ok"])
        names = {item[0].split(".")[-1] for item in client.writes}
        self.assertTrue({"daily_inventory", "daily_shipments", "holding_snapshots",
                         "inventory_cost_snapshots", "shipment_month_reconciliations",
                         "inbound_orders"}.issubset(names))
        shipment = next(item for item in client.writes if item[0].endswith("daily_shipments"))[1][0]
        self.assertEqual(shipment["quantity"], 22)

    def test_openapi_fallback_contains_columns(self):
        client = SupabaseClient("https://example.supabase.co", "test-key")
        payload = client._schema_from_openapi({
            "definitions": {"items": {"properties": {"id": {"type": "integer"},
                                                          "name": {"type": "string"}},
                                        "required": ["id"]}}
        })
        self.assertEqual(payload["tables"][0]["name"], "items")
        self.assertFalse(payload["tables"][0]["columns"][0]["nullable"])


if __name__ == "__main__":
    unittest.main()
