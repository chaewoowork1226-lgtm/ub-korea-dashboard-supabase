import unittest

import erp_stock


class ErpStockParserTests(unittest.TestCase):
    def test_cost_and_inventory_fields_are_kept_for_supabase(self):
        html = """
        <table>
          <tr>
            <th>품목</th><th>부품코드</th><th>금월 출고수량</th><th>홀딩재고</th>
            <th>당일 재고</th><th>작업중 재고</th><th>가용 재고</th><th>단가</th>
          </tr>
          <tr>
            <td>테스트 상품</td><td>SKU-001</td><td>25</td><td>3</td>
            <td>100</td><td>5</td><td>92</td><td>1,234</td>
          </tr>
        </table>
        """.encode("utf-8")
        products = {"테스트_상품": {"code": "SKU-001", "brand": "테스트"}}

        plan = erp_stock.plan(html, products, "20260907101500_재고.xls")

        self.assertEqual(plan["기준일"], "2026-09-07")
        self.assertEqual(plan["연결됨"], 1)
        change = plan["changes"][0]
        self.assertEqual(change["품목코드"], "SKU-001")
        self.assertEqual(change["당일재고"], 100)
        self.assertEqual(change["재고실사"], 105)
        self.assertEqual(change["작업중"], 5)
        self.assertEqual(change["가용재고"], 92)
        self.assertEqual(change["원가"], 1234)


if __name__ == "__main__":
    unittest.main()
