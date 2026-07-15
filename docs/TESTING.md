# Kiểm thử — Genius (Antigravity 2.0)

> Tài liệu này hợp nhất `TEST_READY.md`, `test_report.md` và `challenge.md` (bản gốc trong [`history/`](./history/)).
> Tài liệu **canonical về hạ tầng test phân tán** vẫn là [`../TEST_INFRA.md`](../TEST_INFRA.md) (được tooling `.claude/` tham chiếu).

## 1. Trạng thái hiện tại (2026-07-15, sau đợt hardening P0/P1)

- **1.274 test** thu thập: **1.271 pass, 3 skip** (0 fail), ~2 phút trên máy dev. Bộ test **hermetic**: `conftest.py`/`ag_core.config` không nạp `.env` dưới pytest nên kết quả khớp CI dù máy dev có `.env`.
- **Coverage mã production: ~83%** (đo bằng `python -m pytest --cov=. --cov-fail-under=80`, cấu hình loại trừ test/scaffolding trong `.coveragerc`).
- **CI** (`.github/workflows/ci.yml`): ma trận per-OS theo nhánh — `windows-latest` cho `win`/`main`, `macos-latest` cho `mac`/`main` (bản mac chạy kèm uvloop) — Python 3.11, `pip install -r requirements.txt` → `python -m pytest`. `pytest-timeout` đặt trần 300s/test (backstop treo); `PytestUnraisableExceptionWarning` bị gate thành lỗi (bắt task async rò rỉ). Job **`coverage`** riêng (macOS, nhánh `mac`/`main`) chạy suite dưới pytest-cov với **ngưỡng chặn 80%** — tách khỏi job pytest thuần vì instrumentation đổi timing; chỉ nâng ngưỡng lên, không hạ xuống.
- **Gate độc lập OS** (job `lint-and-audit` + `docker-build` trên `ubuntu-latest`, mọi nhánh): `flake8 .`, `pip check`, `pip-audit` (advisory), và `docker build` smoke. Ngoài ra **pre-commit** vẫn chạy black 24.4.2 + flake8 cục bộ.
- **Không chạy 2 tiến trình pytest cùng lúc** trên một máy (dùng chung `genius.db` + service registry).

> Các số liệu cũ trong `history/` (243 test, 540 test…) là **snapshot lịch sử** ở các mốc trước, không phản ánh tổng hiện tại.

## 2. Bố cục & thu thập test

- Test ở **hai nơi**: `test_*.py` ở root và `tests/`. `pytest.ini` đặt `norecursedirs = projects .agents`.
- `verify_*.py` ở root là **script thủ công** (chạy `python verify_*.py`), không phải gate pytest.
- `conftest.py`: seed mock API key; fixture autouse đổi `SKILL_API_KEY` theo tên file (`valid-api-key` cho `*distributed*`/`*robustness*`/`*milestone3_adversarial*`, còn lại `mock-skill-key`); monkeypatch cho phép plain-SHA256 cho test legacy (**trừ** `test_upgrades` và `tests/test_realrun_hmac.py` — hai file này strict HMAC-only); tắt debate & response-cache để chạy xác định.
- Các cụm test mới từ R2–R4: `tests/test_upgrades.py` (hardening R2), `test_repo_graph.py` (ngữ cảnh budgeted R3), `test_agent_factory.py` / `test_code_graph_index.py` / `test_mcp_code_graph.py` / `test_cast_chunking.py` (R4), `tests/test_realrun_mcp.py` (subprocess stdio MCP **thật** qua SDK chính thức, ghim CHÍNH XÁC bộ 18 tool `genius_*` — thêm tool mới phải cập nhật `EXPECTED_TOOLS`), `tests/test_notebooklm.py` (backend provider + tool `notebooklm_*` qua CLI `nlm`, mock subprocess), `tests/test_realrun_hmac.py`.

## 3. Cấu trúc phủ (coverage tiers)

Bộ test theo triết lý opaque-box, requirement-driven, chia tầng (số liệu cấu trúc từ các snapshot; tổng hiện tại lớn hơn do mở rộng):

| Bộ | Tier 1 Feature | Tier 2 Boundary | Tier 3 Cross-feature | Tier 4 Real-world | Ghi chú |
|---|---|---|---|---|---|
| `test_e2e.py` | 33 | 30 | 6 | 5 | 6 tính năng lõi (server/auth/async/routing/retry/config) |
| `test_e2e_phase5.py` | 25 | 10 | 3 | 1 (+1 CI/CD) | 5 tính năng Phase 5 (Vector Memory, Security, DevOps, routing, CI) |
| `tests/test_distributed.py` | 30 | 30 | 6 | 5 | mạng tác tử phân tán (register/heartbeat/auth/dispatch/retry/workspace) |

## 4. Triết lý "Challenger / Forensic Auditor"

- Test đánh giá qua input/output/state/protocol, **không** dựa vào nội tại LLM. Kỹ thuật: Category-Partition, Boundary Value Analysis, Pairwise Combinatorial, Workload.
- Dùng **`MockNetworkProtocol`** (`tests/test_distributed.py`) tiêm lỗi vào **hub/worker production thật**: `latency` (async sleep), `drop_rate` (raise `asyncio.TimeoutError`), và `error_generators` (HTTP 429/503/401/400) — business logic chạy nguyên vẹn.
- **Mandate liêm chính:** cấm hardcode kết quả, cấm stub/facade, cấm lách task — có "Forensic Auditor" độc lập kiểm tra (xem `../TEST_INFRA.md`).

## 5. Kết quả kiểm thử đối kháng & chịu tải (từ `challenge.md`)

**Đánh giá rủi ro tổng thể: THẤP.** Các cơ chế JWT + checksum HMAC-SHA256 + rate limiter (`Retry-After`) + SQLite WAL đạt 100% pass qua toàn bộ stress/adversarial suite. Các đánh đổi kiến trúc còn lại đã có mitigation:

| # | Mức | Vấn đề | Mitigation (đã/đang áp dụng) |
|---|---|---|---|
| 1 | Medium | Tranh chấp lock SQLite khi ghi log song song cực lớn | **Hàng đợi ghi đơn luồng** (`SQLiteWriterThread`) đã triển khai; có thể migrate sang DBMS client-server cho tải doanh nghiệp |
| 2 | Low | Task chạy dư do sweeper prune worker khi jitter heartbeat | Đề xuất "grace period" / dynamic timeout để worker re-bind task |
| 3 | Low | Giả mạo checksum (MITM) nếu chỉ dùng plain SHA-256 | **Đã dùng HMAC-SHA256** (production HMAC-only) + JWT có `jti` chống replay |
| 4 | Low | Thundering herd khi nhiều worker reconnect đồng loạt | **Đã thêm jitter** ngẫu nhiên trong vòng reconnect/register của worker |

**Bảng stress (trích, tất cả PASS):** worker disconnect during dispatch; graceful deregistration; JWT identity spoofing bypass (bị từ chối); stale worker orphan recovery (404 → auto re-register); busy worker re-registration; result reporting retry; concurrent WS dispatch + disconnect; tenacity Retry-After backoff; WAL concurrency (reader <0.2s vs DELETE >0.9s); DB drive-offline resilience.

**Chưa kiểm (out of scope):** rò rỉ bộ nhớ server dài ngày; packet loss mạng thật (chỉ mô phỏng qua MockNetworkProtocol + WS disconnect cục bộ).

## 6. Ghi chú hiệu năng (từ `test_report.md`)

Các test chậm nhất (~3–10s) chủ yếu là **cố ý** — kiểm tra retry/timeout: các test không mock `httpx.AsyncClient.get` sẽ thử kết nối thật tới `localhost:8001` và retry 3 lần dưới tenacity (~10s); một số test Security/DevOps chạy `OpenAIProvider` thật (không mock) nên timeout. Khuyến nghị (một phần đã áp dụng): mock GET trong e2e/integration, mock provider trong test Security/DevOps, dọn `projects/` khỏi phạm vi thu thập.

## 7. Lệnh chạy nhanh

```bash
python -m pytest                              # toàn bộ (đúng như CI)
python -m pytest test_e2e.py -v               # E2E Phase 1-4
python -m pytest tests/test_distributed.py -v # mạng phân tán
python -m pytest -k "pattern"                 # lọc theo tên
pre-commit run --all-files                    # lint/format gate cục bộ
```
