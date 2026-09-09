# Supabase 연결 설정

이 대시보드는 Excel을 로컬 Python 서버에서 파싱한 뒤 Supabase로 자동 저장합니다. Supabase 비밀키는 브라우저 코드로 전달되지 않습니다.

## 1. Supabase에서 한 번만 실행

Supabase Dashboard의 **SQL Editor**에서 `supabase/setup_dashboard_integration.sql` 전체를 실행합니다.

이 SQL은 기존 재고·출고·홀딩·이커머스·원가 테이블을 다시 만들거나 지우지 않습니다. 다음 두 보조 테이블과 읽기 전용 스키마 함수만 추가합니다.

- `dashboard_import_unmatched`: 품목코드 미매칭과 음수 누계 차이를 원본 JSON으로 보관
- `dashboard_import_runs`: 파일별 적재 결과 기록
- `dashboard_schema(text[])`: 테이블, 컬럼, PK/FK, 파티션을 현재 DB에서 동적으로 조회

두 보조 테이블은 RLS가 활성화되고 `anon`/`authenticated` 접근이 철회됩니다. 스키마 함수도 서버 역할에서만 호출할 수 있습니다.

## 2. 환경변수 설정

`.env.example`을 `.env`로 복사하고 아래 두 값을 채웁니다.

```text
SUPABASE_URL=https://프로젝트-ref.supabase.co
SUPABASE_SECRET_KEY=sb_secret_...
```

신형 Secret key가 없다면 기존 `SUPABASE_SERVICE_ROLE_KEY`를 사용할 수 있습니다. 이 키는 RLS를 우회할 수 있으므로 절대 Git에 올리거나 HTML/JavaScript에 넣지 마세요. `.gitignore`가 `.env`를 제외하도록 설정되어 있습니다.

## 3. 기존 테이블과 자동 매핑

저장할 때마다 실제 Supabase 스키마를 읽고, 존재하는 테이블·컬럼만 사용합니다.

| Excel 데이터 | 기본 Supabase 대상 |
|---|---|
| 재고 스냅샷 | `daily_inventory` |
| 누계 차이로 계산한 일별 출고 | `daily_shipments` |
| 월별 홀딩 스냅샷 | `holding_snapshots` |
| ERP `단가` | `inventory_cost_snapshots` |
| 전월 출고 정산값 | `shipment_month_reconciliations` |
| 확정 입고예정 | `inbound_orders` |
| 쿠팡/컬리 월별 집계 | `ecommerce_sku_monthly` |

테이블 이름이 다르면 `.env.example`의 `SUPABASE_TABLE_...` 항목을 활성화해 바꿀 수 있습니다.

Upsert는 해당 테이블의 현재 PK/UNIQUE 제약을 확인한 뒤 실행합니다. 필요한 고유키가 없으면 중복 가능성이 있는 insert를 강행하지 않고 오류로 기록합니다.

### 이커머스 품목 매칭

우선 `product_aliases`, 다음으로 `products`의 상품명/품목코드를 사용합니다. 그래도 매칭되지 않으면 `dashboard_import_unmatched`에 보관합니다. 회사에서 확정한 수동 매핑은 `data/ecom_product_map.json`에 다음처럼 추가할 수 있습니다.

```json
{
  "쿠팡 원본 SKU 이름": "BOIL0058",
  "쿠팡|플랫폼까지 구분할 SKU 이름": "BOIL0044"
}
```

자동 유사도 추정으로 잘못된 품목코드를 붙이지 않는 것이 원칙입니다.

## 4. 실행

Windows에서는 `run_dashboard.bat`을 더블 클릭하거나 터미널에서 실행합니다.

```powershell
py -m pip install -r requirements.txt
py forecast_tool.py serve
```

브라우저에서 `http://127.0.0.1:8787/`이 열립니다.

- 재고표: 수요예측 탭의 ERP 업로드 영역에서 미리보기 후 반영
- 이커머스: 이커머스 관리 탭의 거래내역 업로드 영역에서 미리보기 후 반영
- 스키마: `Database Schema` 탭을 열거나 **스키마 새로고침** 클릭

Supabase에서 테이블/컬럼/PK/FK/파티션이 바뀌면 다음 새로고침에 그대로 반영됩니다.

## 5. 검증

실제 키를 넣은 뒤 다음 순서로 확인합니다.

1. `Database Schema` 탭에 현재 테이블과 관계가 표시되는지 확인
2. 같은 재고 파일을 두 번 반영하고 대상 테이블의 행 수가 늘지 않는지 확인
3. `dashboard_import_runs`에서 `success`와 테이블별 건수를 확인
4. `dashboard_import_unmatched`에서 미매칭 품목을 검토해 `product_aliases` 또는 수동 매핑에 추가

로컬 자동 테스트는 다음으로 실행합니다.

```powershell
py -m unittest discover -s tests -v
```
