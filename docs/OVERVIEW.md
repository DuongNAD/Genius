# TỔNG QUAN DỰ ÁN — GENIUS (Antigravity 2.0)

> **Tài liệu canonical** mô tả kiến trúc, thành phần, chất lượng, rủi ro và khuyến nghị của toàn bộ codebase.
> **Cập nhật gần nhất:** 2026-07-01 (nhánh `main`).
> Tài liệu này hợp nhất các báo cáo tổng-quan trước đây; bản gốc được lưu trong [`history/`](./history/)
> (`Genius_Comprehensive_Report.md`, `BAO_CAO_KIEM_TRA_DU_AN_2026-06-30.md`, `PROJECT.md`, `HANDOFF_ROADMAP.md`).
> Xem thêm: [`TESTING.md`](./TESTING.md) · [`../PIPELINE_COMPARISON.md`](../PIPELINE_COMPARISON.md) · [`../TEST_INFRA.md`](../TEST_INFRA.md)

---

## 1. Tóm tắt điều hành

**Genius** (tên nội bộ *Antigravity 2.0*) là một **framework đa tác tử (multi-agent) phân tán** dùng để **tự động hoá lập trình, refactor và kiểm thử phần mềm**. Hệ thống mô phỏng một đội kỹ sư phần mềm: sáu tác tử AI chuyên biệt (Nghiên cứu → Kiến trúc → Lập trình → Kiểm thử → Bảo mật → DevOps), mỗi tác tử chạy như một **microservice FastAPI độc lập**, được điều phối bởi một **orchestrator bất đồng bộ (asyncio)**.

Điểm đặc trưng:
- **Local-CLI-first:** các tác tử gọi CLI cục bộ (`grok`, `claude`, `codex`) thay vì gọi thẳng API đám mây; API key chỉ là phương án dự phòng.
- **Hai chế độ vận hành:** HTTP trực tiếp tới từng server vai trò, hoặc **phân tán** qua một Central Hub điều phối worker qua WebSocket.
- **Vòng lặp tự chữa (self-healing):** code sinh ra được chạy `pytest`/`flake8` thật, log lỗi được đưa ngược lại cho tác tử Codex để tự vá.
- **Tích hợp MCP:** cắm trực tiếp vào Google Antigravity 2.0 như một bộ điều phối.

**Quy mô & sức khỏe hiện tại:**

| Chỉ số | Giá trị |
|---|---|
| Ngôn ngữ | Python 3.10+ (CI chạy 3.11, Windows) |
| Số file `.py` (không tính scratch) | 122 |
| Module trong `ag_core/` | 37 |
| File test (root + `tests/`) | 55 |
| Tổng số test thu thập | **540 (100% pass)** |
| Lint `flake8` | ✅ 0 lỗi |
| Format `black 24.4.2` | ✅ sạch |
| Số commit | 63 (khởi tạo 2026-06-27) |

---

## 2. Mục tiêu & bối cảnh

Genius đã tiến hoá **từ kiến trúc khối (monolith) điều khiển qua CLI** thành **nền tảng vi dịch vụ phân tán**. Mục tiêu: nhận một *prompt* mô tả phần mềm cần xây, rồi tự động chạy qua toàn bộ vòng đời phát triển và trả về các *artifact* (tài liệu nghiên cứu, thiết kế, mã nguồn, test, báo cáo bảo mật, script triển khai).

Dự án là **Windows-first** (CI chạy trên `windows-latest`), phản ánh trong nhiều đoạn xử lý CLI đặc thù Windows (`.cmd`/`.bat` bọc qua `cmd.exe /c`, dò đường dẫn `%APPDATA%`/`%LOCALAPPDATA%`).

---

## 3. Kiến trúc hệ thống

### 3.1 Luồng xử lý tổng quát

```
[Prompt người dùng]
   │
   ▼
Orchestrator (asyncio)  ──async httpx + JWT + HMAC checksum──►  FastAPI Skill Server (/run, /status)
   │                                                                │
   │  (chế độ --distributed: chọn worker theo vai trò               ▼
   │   qua Central Hub + WebSocket)                              Agent (BaseAgent)
   │                                                                │
   ▼                                                                ▼
[Artifacts: research.md, design.md, src/*, tests/*, review.md,   Provider (local CLI → LLM)
 audit.md, deploy.md, .agents/CURRENT_PROG.md]
```

### 3.2 Bảng vai trò & cổng

| Tác tử | Cổng | Provider | Vai trò |
|---|---|---|---|
| **Grok Researcher** | 8001 | GrokProvider | Phân tích yêu cầu, nghiên cứu → `research.md` |
| **Claude Architect** | 8002 | AnthropicProvider | Thiết kế kiến trúc (JSON `DesignPlan`) → `design.md` |
| **Codex Reviewer** | 8003 | OpenAIProvider | Sinh/review code + self-healing → `review.md` |
| **Tester Agent** | 8004 | OpenAIProvider | Sinh pytest chạy được → `test_generated.py` |
| **Security Agent** | 8005 | OpenAIProvider | Audit lỗ hổng (JSON verdict) → `audit.md` |
| **DevOps Agent** | 8006 | OpenAIProvider | Dockerfile/CI-CD/deploy → `deploy.md` |
| **Central Hub** | 8000 | — | Điều phối worker (chế độ phân tán) |
| **Dashboard** | 8080 | — | Giám sát realtime (FastAPI + WebSocket) |

### 3.3 Các điểm vào (entrypoints)

- **`serve.py`** — menu tương tác/CLI khởi chạy các server vai trò. Cờ: `--roles`, `--prompt`, `--auto-pilot`, `--distributed`, `--pipeline {sequential,e2e}`, `--hub-port`, `--doctor`. Ghi cổng thực đã bind vào `.agents/service_registry.json` để phát hiện động.
- **`orchestrator.py`** — bộ điều phối pipeline bất đồng bộ (gọi service qua `httpx`, poll `/status`, retry bằng `tenacity`).
- **`mcp_server.py`** — MCP server (stdio/HTTP) expose 8 tool cho Antigravity.
- **`dashboard.py`** — bảng giám sát port 8080.

### 3.4 Skill Server factory (`ag_core/skill_app.py`)

`create_skill_app(role)` dựng một app FastAPI có 2 endpoint:
- **`POST /run`** — xác thực JWT, kiểm HMAC checksum, rate-limit, hỗ trợ **idempotency** (`X-Idempotency-Key`), chạy tác tử dưới dạng background task, trả `{task_id, status:"processing"}`.
- **`GET /status/{task_id}`** — trả `{status, result|error}`.

`ROLE_MAP` ánh xạ vai trò → (agent, provider, model mặc định). Ở chế độ server, `stateless=True` tắt ghi file và vector memory để mỗi request không để lại dấu vết.

---

## 4. Đội hình tác tử (Agents)

Tất cả kế thừa **`BaseAgent`** (`ag_core/interfaces/base_agent.py`), chia sẻ vòng đời:
`parse slash-command` → `scan_context()` (quét project bằng `ProjectScanner`) → dựng `full_prompt` (history + prompt + memory + ngữ cảnh project) → `provider.send_prompt(..., system=<ROLE_PROMPT>)` → `log_transaction` → `write_output()`.

BaseAgent cung cấp: `scan_context`, `format_history`, `store_memory`/`retrieve_memory` (no-op nếu tắt memory), `resolve_output_file` (sentinel `"None"` = không ghi), `write_output` (ghi UTF-8, lỗi không fatal).

**Đặc thù từng tác tử:**
- **Codex Reviewer** — có **vòng lặp tự chữa**: sau khi LLM sinh code, chạy `flake8` + `pytest` thật; nếu fail thì lặp tối đa `max_retries` (mặc định 3), đưa log test vào lại prompt, trích code bằng `extract_code`, ghi lại file (có chặn path-traversal) rồi chạy lại.
- **Tester** — sinh module pytest, tự chạy `pytest <file>` với `PYTHONPATH` trỏ vào project + `src`, lặp tự chữa khi fail. (`__test__ = False` để pytest không thu thập nhầm.)
- **Security** — ép LLM trả JSON `{blocking, findings[]}`; audit rỗng bị coi là **fail-closed** (chặn).
- **DevOps** — sinh nhiều artifact, mỗi cái trong block riêng có tiền tố `# filepath:`.

---

## 5. Tầng Provider (Local-CLI-first)

`BaseProvider` (ABC) tích hợp **TokenBucket rate limiter** (10 req/s) + `asyncio.Semaphore(5)`, validate phản hồi qua pydantic, xử lý backoff 429 theo `Retry-After`.

Cả 3 provider đều bọc một **CLI vendor cục bộ**; API key đọc trong `__init__` chủ yếu để quyết định hành vi đăng nhập, còn request thật đi qua subprocess CLI:

- **AnthropicProvider** → `claude` CLI (`--bare --tools "" --output-format json`). Prompt dài (>1000 ký tự) ghi ra file tạm.
- **GrokProvider** → `grok` CLI. Không có API key thì tự chạy `grok login` phi tương tác. Dùng `--prompt-file`, `--session-id`, `--system-prompt-override`.
- **OpenAIProvider** → `codex exec` CLI (Codex Desktop), nạp prompt qua **stdin**, parse **JSONL event stream** (chịu nhiễu tốt), thu thập `agent_message` + token usage. Sandbox chỉ bật nếu `GENIUS_CODEX_SANDBOX` truthy.

**Hạ tầng CLI chống lỗi:**
- `cli_resolver.py` — `which_external()` **loại bỏ** mọi match nằm trong repo (tránh vòng lặp fork-bomb wrapper→run.py→agent→provider→wrapper).
- `cli_runner.py` — `communicate_with_timeout` giới hạn thời gian (mặc định 600s, cấu hình qua `GENIUS_CLI_TIMEOUT`), kill process khi quá hạn; `explain_cli_failure` ánh xạ stderr → gợi ý (hết credit, cần login, thiếu binary → gợi ý chạy `--doctor`).
- `code_extract.py` — trả về **block code lớn nhất** (không nối tất cả block, tránh gộp ví dụ/usage thành file hỏng).

---

## 6. Pipeline điều phối

Định nghĩa trong `orchestrator.py`. Tài liệu `PIPELINE_COMPARISON.md` **cố ý giữ hai pipeline tách biệt** (chỉ dùng chung scaffolding).

### 6.1 `run_pipeline` — tuần tự, đầy đủ 7 giai đoạn
1. **Fast-path slash-command:** nếu prompt bắt đầu bằng lệnh được route → gọi 1 tác tử rồi trả về.
2. **Grok** research → `research.md`.
3. **Claude** design → `design.md`, có vòng **tranh luận Grok⇄Claude** tuỳ chọn (early-exit khi `[APPROVED]`).
4. Vòng phản hồi **tương tác** qua stdin (tuỳ chọn).
5. **Parse design → danh sách file**, fan-out từng file dưới `Semaphore(3)`: Codex `/code` → Tester + Security song song → ghi file, chạy pytest, quét lỗ hổng → **self-healing retry**.
6. Tổng hợp `review.md` + `audit.md`, rồi **DevOps** `/deploy` → `deploy.md`.
7. **Fallback** một-file nếu không parse được file nào.

### 6.2 `run_e2e_pipeline` — nhẹ, 4 tác tử
Claude `/plan` → Grok critique → parse file → mỗi file chạy **hai vòng self-heal tuần tự** (Codex implement gated bởi flake8+pytest, rồi Tester gated bởi pytest). Không MessageBus, không Security/DevOps, không tương tác.

---

## 7. Chế độ phân tán (Distributed)

- **CentralHub** (`ag_core/distributed/hub.py`) — sở hữu registry worker, hàng đợi task (giới hạn 10.000, có eviction), config mặc định `max_workers=10`, `heartbeat_timeout=0.5s`, `task_timeout=60s`. Xử lý `/register`, `/heartbeat`, `/dispatch`, `/report_result` (kiểm tra worker báo cáo đúng là worker được giao, nếu không → 403), `/write_workspace_file` (chặn path-traversal). Có **sweeper** chạy mỗi 1s để prune worker chết, fail task treo, timeout task quá hạn.
- **ClientWorker** (`ag_core/distributed/worker.py`) — kết nối `ws://.../ws/connect?token=<JWT>`, gửi `register`, heartbeat 10s, nhận `run_task`. Reconnect exponential backoff (1s→60s) + jitter. Từ chối task khi bận / thiếu / sai checksum (trả failure có chữ ký). Báo kết quả qua WS (ký) hoặc `/report_result` (5 lần backoff, bọc `asyncio.shield` để sống sót qua cancel).

---

## 8. Bảo mật

- **Toàn vẹn payload:** `HMAC-SHA256` trên JSON chuẩn hoá (`sort_keys`, separator gọn), gửi ở header `X-Payload-SHA256`, verify bằng `hmac.compare_digest`.
- **Production là HMAC-only** — **không** có fallback plain-SHA256. Cờ `is_plain` chỉ tồn tại để `conftest.py` monkeypatch cho test legacy; đường production luôn HMAC.
- **Xác thực inter-service (JWT qua `X-API-Key`):** JWT HS256 tự cài đặt, bắt buộc secret khác rỗng, tự thêm `jti` (uuid4). `decode_jwt` kiểm `alg=HS256`, chữ ký constant-time, `exp`, và **chống replay** — mỗi `jti` lưu vào bảng SQLite `seen_jtis`; trùng → "Token replay detected".
- **Secret duy nhất `SKILL_API_KEY`** dùng cho cả HMAC lẫn JWT; hub và worker giải như nhau. Worker **fail loudly** (RuntimeError) nếu secret rỗng thay vì ký bằng secret rỗng.
- **`checksum_middleware`** ép checksum trên `/run` và `/status`, ký lại body phản hồi.
- **Che giấu secret trong git** (`git.py`): `_mask` redact basic-auth/token khỏi mọi log và thông báo lỗi.

---

## 9. Bộ nhớ & dữ liệu

- **`db.py`** — SQLite (WAL + `auto_vacuum=FULL`), 3 bảng: `conversations`, `agent_logs`, `seen_jtis`. **Hàng đợi ghi đơn luồng** (`SQLiteWriterThread`) tuần tự hoá mọi write để tránh tranh chấp, nhưng cho ngữ nghĩa đồng bộ (block chờ Event, re-raise lỗi).
- **`MessageBus`** (`message_bus.py`) — hộp thư A2A (Agent-to-Agent). `Artifact` mang id/name/content/type/parent_id/metadata; lưu in-memory (cap 100) + SQLite backing (bảng `artifacts`, WAL). Đọc in-memory trước rồi SQLite, trả bản mới nhất.
- **`VectorMemory`** (`vector_store.py`) — RAG hai tầng: ưu tiên `sentence-transformers` (all-MiniLM-L6-v2), fallback `SimpleTFIDFEmbedding` (128-dim, md5 buckets, L2-normalize); store dùng ChromaDB nếu có (dir `.chroma`), lỗi init thì **fallback SQLite** (bảng `agent_vector_memory_fallback`, xếp hạng bằng cosine). Dependency nặng chỉ dò bằng `find_spec` (không import lúc load).

---

## 10. Khả năng chống chịu (Resilience)

- **Degraded mode** (opt-in `GENIUS_DEGRADED_MODE`): xuất artifact một phần thay vì abort khi vài file lỗi hoặc DevOps lỗi; chỉ re-raise khi thất bại toàn phần. Tắt mặc định (CI giữ fail-fast).
- **Preflight doctor** (`serve.py --doctor` → `ag_core/diagnostics.py`): kiểm mỗi CLI (grok/claude/codex) có chạy được `--version` không, và `SKILL_API_KEY` đã set chưa; trả `READY`/`NOT READY` với exit code.
- **Idempotency:** orchestrator gửi `X-Idempotency-Key` ổn định qua các lần retry; skill server dedupe.
- **`gather_or_raise`:** chạy song song với `return_exceptions=True` để không nhánh nào bị mồ côi, rồi re-raise lỗi đầu tiên.
- **Retry/backoff:** `tenacity stop_after_attempt(3)` + backoff theo `Retry-After`, chỉ retry lỗi tạm thời (429/5xx/connection/checksum mismatch).
- **Path safety:** `safe_join` từ chối path tuyệt đối/`..`-escaping do model sinh.
- **Rate limiter:** TokenBucket bật ở production, **bỏ qua dưới pytest** trừ khi `ENABLE_RATE_LIMITER`.

---

## 11. Tích hợp MCP (Antigravity 2.0)

`mcp_server.py` (JSON-RPC qua stdio hoặc HTTP) expose **8 tool**:
- Đơn tác tử: `research`, `design`, `code`, `unit_test`, `security_audit`, `deploy`.
- Pipeline: **`orchestrate`** (chạy toàn bộ pipeline nền, trả `job_id` ngay) + **`orchestrate_status`** (poll → artifacts).

Đăng ký trong `~/.gemini/antigravity/mcp_config.json` bằng đường dẫn tuyệt đối tới `python.exe`. Cần chạy `python serve.py` (cổng 8001–8006) trước khi dùng `orchestrate`.

---

## 12. Kiểm thử & CI

- **Bố cục:** test ở **hai nơi** — `test_*.py` ở root (~45 file) và `tests/` (~15 file). `verify_*.py` là **script thủ công**, không phải gate. `pytest.ini` đặt `norecursedirs = projects .agents`.
- **`conftest.py`:** seed mock key; fixture autouse đổi `SKILL_API_KEY` theo tên file (`valid-api-key` cho `*distributed*`/`*robustness*`/`*milestone3_adversarial*`, còn lại `mock-skill-key`); monkeypatch cho phép plain-SHA256 cho test legacy (trừ `test_upgrades`); tắt debate & cache để xác định.
- **Triết lý "Challenger / Forensic Auditor"** (`TEST_INFRA.md`): test opaque-box theo yêu cầu, dùng **`MockNetworkProtocol`** tiêm lỗi (latency, drop packet, HTTP 429/503/401/400) vào **hub/worker production thật**. Mandate liêm chính: **cấm hardcode kết quả / stub facade** — có auditor độc lập kiểm tra.
- **Số lượng:** ~529 `def test_` thô, mở rộng qua parametrize thành **540 test thu thập**. `test_e2e.py` 74, `tests/test_distributed.py` 71, `test_e2e_phase5.py` 40.
- **CI (`.github/workflows/ci.yml`):** `windows-latest`, Python 3.11, `pip install -r requirements.txt` → `python -m pytest`. Lint **không** nằm trong CI mà ở **pre-commit** (black 24.4.2 + flake8 7.0.0 — **pin, không có trong `requirements.txt`**).

---

## 13. Tech stack & triển khai

- **Runtime:** pydantic 2.12, FastAPI, uvicorn, httpx, websockets, tenacity, PyYAML, python-dotenv, tiktoken, pathspec, pytest + pytest-asyncio.
- **Docker:** `Dockerfile` (python:3.11-slim, expose 8000–8006 + 8080, `CMD python serve.py`); `docker-compose.yml` dựng 8 service trên volume chung `genius_data` (hub, dashboard, và 1 container/vai trò).
- **Cấu hình:** `config.yaml` (metadata, model per-provider, scanner excludes, service URL map) + `.env` (secrets, `ag_core/config.py` đi ngược cây thư mục để tìm). Dưới pytest, URL service được thêm hậu tố `/role` (test và prod giải URL khác nhau). `.agents/service_registry.json` override cổng động.

---

## 14. Tình trạng hiện tại (đã kiểm chứng 2026-07-01)

Sau khi **merge `origin/main`** (14 commit local + 36 commit remote phân kỳ, đã giải 38 conflict theo bản remote superset) và dọn dẹp:

| Gate | Kết quả |
|---|---|
| `python -m pytest` (lệnh CI) | ✅ **540 passed, 0 failed** (~110s) |
| `flake8` | ✅ 0 lỗi |
| `black 24.4.2` (bản pin) | ✅ 122 file sạch |
| Import runtime entrypoints + module mới | ✅ OK |
| Đồng bộ remote | ✅ `main` = `origin/main` @ `ccdd59d` |

**Cảnh báo (không chặn):** 80 warning trong pytest — chủ yếu `DeprecationWarning` của `websockets.legacy` và một `PytestUnraisableExceptionWarning` (coroutine cleanup) trong `tests/test_challenger_m2_1.py`.

---

## 15. Đánh giá — Điểm mạnh & Rủi ro

### 15.1 Điểm mạnh
- **Kiến trúc rõ ràng, tách bạch:** vai trò → provider → CLI phân lớp gọn; skill server stateless dễ scale.
- **Bảo mật nghiêm túc cho một dự án nội bộ:** HMAC-only ở production, JWT có chống replay (`jti`), che secret trong log git, chặn path-traversal ở nhiều lớp.
- **Chống chịu tốt:** self-healing loop chạy test thật, degraded mode, doctor, idempotency, timeout CLI, `gather_or_raise`.
- **Văn hoá test mạnh:** 540 test với tiêm lỗi mạng thật, mandate cấm giả lập kết quả.

### 15.2 Rủi ro & Nợ kỹ thuật
1. **Phụ thuộc `.agents/skills/<role>/run.py` + `api.py` sinh cục bộ** (bị gitignore). Nhiều wrapper (`claude`, `codex`, `grok`, `tester`) sẽ **fail nếu skill chưa được sinh** — rào cản onboarding.
2. **Lệch phiên bản black:** máy dev đang có black 26.5.1 nhưng dự án pin 24.4.2 → format bằng `black` global gây churn lệch chuẩn. **Khuyến nghị dùng `pre-commit run` thay vì `black` trực tiếp.**
3. **Hai pipeline (`sequential`/`e2e`) trùng scaffolding** nhưng cố ý không gộp — `PIPELINE_COMPARISON.md` đánh dấu ngữ nghĩa verify per-file là "rủi ro gộp Rất cao". Cần cẩn trọng khi refactor.
4. **Vòng debate "test-blind":** dưới pytest, `provider.send_prompt` bị mock nên các vòng tranh luận Grok⇄Claude không được kiểm bằng test thật.
5. **Cảnh báo deprecated `websockets.legacy`** — sẽ vỡ ở phiên bản websockets tương lai; nên nâng cấp sang API mới.
6. **Nhiều tài liệu `.md` chồng chéo** (`Genius_Comprehensive_Report.md`, `PROJECT.md`, `HANDOFF_ROADMAP.md`, nhiều `BAO_CAO_*`) — nên hợp nhất để tránh phân kỳ.
7. **File runtime lẫn trong repo:** `genius.db`, `mem_custom_path.db`, `stress_test_temp.db`, `temp_workspace_f4_large` nằm ở root — nên đảm bảo gitignore và dọn định kỳ.

### 15.3 Khuyến nghị ưu tiên
| Ưu tiên | Việc |
|---|---|
| Cao | Chuẩn hoá quy trình format qua `pre-commit` (tránh lệch black 24 vs 26). |
| Cao | Viết tài liệu/script **sinh `.agents/skills/*`** để onboarding chạy được ngay. |
| ~~Trung bình~~ ✅ | ~~Nâng cấp khỏi `websockets.legacy`~~ — đã nâng `websockets>=16` (legacy bị loại bỏ hẳn); uvicorn dùng `ws="auto"` vì ép cứng `websockets-sansio` gây lỗi WS dispatch trên uvicorn 0.41/Windows. |
| ~~Trung bình~~ ✅ | ~~Hợp nhất các file báo cáo `.md` trùng lặp~~ — đã gom về `docs/` + `docs/history/`. |
| Thấp | Đưa flake8 vào CI (hiện chỉ ở pre-commit) để gate lint ở server. |

---

## 16. Phụ lục — Cấu trúc `ag_core/`

```
ag_core/
├── agents/          # 6 tác tử: grok_researcher, claude_architect, codex_reviewer,
│                    #           tester, security_agent, devops_agent
├── providers/       # anthropic_provider, grok_provider, openai_provider
├── interfaces/      # base_agent, base_provider
├── distributed/     # hub (CentralHub), worker (ClientWorker)
├── memory/          # vector_store (Chroma + TF-IDF/SQLite fallback)
├── scanner/         # project_scanner (+ ProjectChunker theo token)
├── utils/           # security, jwt, rate_limiter, message_bus, db, git,
│                    # logger, cli_resolver, cli_runner, code_extract, prompt_templates
├── skill_app.py     # factory dựng FastAPI skill server theo vai trò
├── config.py        # load config.yaml + .env, rewrite URL, port discovery
├── models.py        # DesignPlan / DesignFile (pydantic)
└── diagnostics.py   # preflight doctor
```

---

## 17. Phụ lục — Milestones (M1–M22, đều DONE)

Tổng hợp từ `PROJECT.md` (lưu trong [`history/`](./history/)). Toàn bộ đã hoàn thành.

| # | Tên | Phạm vi |
|---|---|---|
| 1 | Monolith Core & CLI | Framework phân lớp ban đầu, CLI wrappers, rate limiter, tenacity API retries. |
| 2 | FastAPI Skill APIs | Expose skill qua FastAPI với header `X-API-Key` + `X-Payload-SHA256`. |
| 3 | Stateless Payloads | Hỗ trợ `context_data` tùy chọn, bỏ quét/ghi đĩa ở server. |
| 4 | Async Orchestration | Gọi httpx bất đồng bộ, poll task, tenacity retry. |
| 5 | Startup Menu CLI | `serve.py` bootup vai trò động. |
| 6 | Tests & Verification | Phủ test HTTP mock + verification. |
| 7 | Vector Memory (R1) | Vector DB cục bộ (fallback + chroma), tích hợp BaseAgent/Claude/Codex. |
| 8 | DevOps & Security (R2) | Thêm 2 tác tử Security & DevOps vào routing/menu. |
| 9 | CI/CD Pipeline (R3) | GitHub Actions `.github/workflows/ci.yml`. |
| 10 | E2E Testing & Verification | Kiểm chứng toàn bộ năng lực Phase 5 qua integration/E2E. |
| 11–16 | Swarm Upgrades | Skill layer & CLI hang, core distribution/security (HMAC-SHA256, jti JWT), DB write queue + dynamic port, logging, audit gate. |
| 17–22 | Upgrade V2 | Grok/Codex CLI providers, config/rate-limiter, memory (sentence-transformers) + concurrency, streaming/WebSocket dashboard/MCP/Docker, final audit gate. |

---

*Tài liệu tổng hợp từ khảo sát mã nguồn (orchestrator, serve, skill_app, agents, providers, distributed, security, memory, testing). Bản chi tiết theo commit gốc: xem lịch sử git của `docs/OVERVIEW.md`.*
