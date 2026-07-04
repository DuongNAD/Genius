# TỔNG QUAN DỰ ÁN — GENIUS (Antigravity 2.0)

> **Tài liệu canonical** mô tả kiến trúc, thành phần, chất lượng, rủi ro và khuyến nghị của toàn bộ codebase.
> **Cập nhật gần nhất:** 2026-07-04 (sau round R4; nhánh `win` = `main`, nhánh `mac` = cùng nội dung + uvloop).
> Tài liệu này hợp nhất các báo cáo tổng-quan trước đây; bản gốc được lưu trong [`history/`](./history/)
> (`Genius_Comprehensive_Report.md`, `BAO_CAO_KIEM_TRA_DU_AN_2026-06-30.md`, `PROJECT.md`, `HANDOFF_ROADMAP.md`).
> Xem thêm: [`TESTING.md`](./TESTING.md) · [`../PIPELINE_COMPARISON.md`](../PIPELINE_COMPARISON.md) · [`../TEST_INFRA.md`](../TEST_INFRA.md)

---

## 1. Tóm tắt điều hành

**Genius** (tên nội bộ *Antigravity 2.0*) là một **framework đa tác tử (multi-agent) phân tán** dùng để **tự động hoá lập trình, refactor và kiểm thử phần mềm**. Hệ thống mô phỏng một đội kỹ sư phần mềm: sáu tác tử AI chuyên biệt (Nghiên cứu → Kiến trúc → Lập trình → Kiểm thử → Bảo mật → DevOps), mỗi tác tử chạy như một **microservice FastAPI độc lập**, được điều phối bởi một **orchestrator bất đồng bộ (asyncio)**.

Điểm đặc trưng:
- **Local-CLI-first:** các tác tử gọi CLI cục bộ (`agy` — Antigravity/Gemini, `claude`, `codex`; `grok` opt-in) qua chuỗi fallback per-role, thay vì gọi thẳng API đám mây; API key chỉ là phương án dự phòng.
- **Hai chế độ vận hành:** HTTP trực tiếp tới từng server vai trò, hoặc **phân tán** qua một Central Hub điều phối worker qua WebSocket (đang chạy thật: hub trên máy Mac + worker grok trên máy thứ hai).
- **Vòng lặp tự chữa (self-healing):** code sinh ra được chạy `pytest`/`flake8` thật, log lỗi được đưa ngược lại cho tác tử Codex để tự vá.
- **Ngữ cảnh theo đồ thị code:** mỗi call agent nhận workspace đã được xếp hạng PageRank + cắt theo ngân sách token (`GENIUS_CONTEXT_TOKEN_BUDGET`, kiểu aider repo-map/CodexGraph) thay vì nguyên cả repo.
- **Tích hợp MCP:** cắm trực tiếp vào Google Antigravity 2.0 như một bộ điều phối (14 tool + resources), gồm cả tool `code_graph` để agent tự truy vấn cấu trúc repo.

**Quy mô & sức khỏe hiện tại (2026-07-04, commit `c8e1ea4`):**

| Chỉ số | Giá trị |
|---|---|
| Ngôn ngữ | Python 3.10+ (CI chạy 3.11: `windows-latest` cho `win`/`main`, `macos-latest` cho `mac`/`main`) |
| Số file `.py` (không tính scratch) | 128 |
| Module trong `ag_core/` | 45 |
| File test (root + `tests/`) | 70 |
| Tổng số test thu thập | **786 (784 pass, 2 skip)** |
| Lint `flake8` | ✅ 0 lỗi |
| Format `black 24.4.2` | ✅ sạch |
| Số commit | 113 (khởi tạo 2026-06-27) |

---

## 2. Mục tiêu & bối cảnh

Genius đã tiến hoá **từ kiến trúc khối (monolith) điều khiển qua CLI** thành **nền tảng vi dịch vụ phân tán**. Mục tiêu: nhận một *prompt* mô tả phần mềm cần xây, rồi tự động chạy qua toàn bộ vòng đời phát triển và trả về các *artifact* (tài liệu nghiên cứu, thiết kế, mã nguồn, test, báo cáo bảo mật, script triển khai).

Dự án chạy **đa nền tảng theo nhánh**: nhánh `win` (máy dev Windows — nhiều đoạn xử lý CLI đặc thù Windows: `.cmd`/`.bat` bọc qua `cmd.exe /c`, dò `%APPDATA%`/`%LOCALAPPDATA%`) và nhánh `mac` (máy hub macOS — thêm uvloop), cùng chức năng, hội tụ về trục tích hợp `main`. CI chạy per-OS theo nhánh (`windows-latest` cho `win`/`main`, `macos-latest` cho `mac`/`main`).

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

| Tác tử (role id) | Cổng | Chuỗi provider mặc định | Vai trò |
|---|---|---|---|
| **Researcher** (`researcher`; alias cũ `grok`/`grok_researcher`) | 8001 | `agy → claude → codex` | Phân tích yêu cầu, nghiên cứu → `research.md` |
| **Claude Architect** (`claude`) | 8002 | `claude → agy → codex` | Thiết kế kiến trúc (JSON `DesignPlan`) → `design.md` |
| **Codex Reviewer** (`codex`) | 8003 | `codex → claude → agy` | Sinh/review code + self-healing → `review.md` |
| **Tester Agent** (`tester`) | 8004 | `codex → claude → agy` | Sinh pytest chạy được → `test_generated.py` |
| **Security Agent** (`security`) | 8005 | `codex → claude → agy` | Audit lỗ hổng (JSON verdict) → `audit.md` |
| **DevOps Agent** (`devops`) | 8006 | `codex → claude → agy` | Dockerfile/CI-CD/deploy → `deploy.md` |
| **Central Hub** | 8000 | — | Điều phối worker (chế độ phân tán) |
| **Dashboard** | 8080 | — | Giám sát realtime (FastAPI + WebSocket) |

Chuỗi provider ghi đè được per-role qua `GENIUS_PROVIDER_<ROLE>`; backend `grok` chỉ opt-in. Chi tiết ở `ag_core/provider_factory.py`.

### 3.3 Các điểm vào (entrypoints)

- **`serve.py`** — menu tương tác/CLI khởi chạy các server vai trò. Cờ: `--roles`, `--prompt`, `--auto-pilot`, `--distributed`, `--pipeline {sequential,e2e}`, `--hub-port`, `--doctor`. Ghi cổng thực đã bind vào `.agents/service_registry.json` để phát hiện động.
- **`orchestrator.py`** — bộ điều phối pipeline bất đồng bộ (gọi service qua `httpx`, poll `/status`, retry bằng `tenacity`).
- **`mcp_server.py`** — MCP server (stdio/HTTP) expose 14 tool + MCP resources cho Antigravity (xem §11).
- **`dashboard.py`** — bảng giám sát port 8080.

### 3.4 Skill Server factory (`ag_core/skill_app.py`)

`create_skill_app(role)` dựng một app FastAPI có 2 endpoint:
- **`POST /run`** — xác thực JWT, kiểm HMAC checksum, rate-limit, hỗ trợ **idempotency** (`X-Idempotency-Key`), chạy tác tử dưới dạng background task, trả `{task_id, status:"processing"}`.
- **`GET /status/{task_id}`** — trả `{status, result|error}`.

Việc dựng agent + provider đi qua **một đường duy nhất: `ag_core/agent_factory.py`** (`build_agent(role, ...)` với bundle stateless `output_file="None"` / `use_memory=False` / `stateless=True`); `skill_app.ROLE_MAP` và `ROLE_AGENT_MAP` của distributed worker đều derive từ bảng `AGENT_CLASSES` của factory (mcp_server cũng dựng qua factory nhưng giữ tra class bằng `globals()` để test patch được). Ở chế độ server, `stateless=True` tắt ghi file và vector memory để mỗi request không để lại dấu vết.

---

## 4. Đội hình tác tử (Agents)

Tất cả kế thừa **`BaseAgent`** (`ag_core/interfaces/base_agent.py`). Từ R4, vòng đời chung nằm hẳn trong template method **`BaseAgent._run_standard`**:
`resolve prompt` → `rewrite slash-command` (bảng `SLASH_PREFIXES` per-class) → `scan_context_async()` → khối memory context (nếu `USES_MEMORY`) → dựng `full_prompt` → `provider.send_prompt(..., system=SYSTEM_PROMPT)` → history/memory → `log_transaction` → `write_output(DEFAULT_OUTPUT_FILE)`.

Bốn tác tử "chuẩn" (Researcher/Architect/Security/DevOps) chỉ khai báo bộ knob class-level và delegate `run()` một dòng; Codex/Tester giữ `run()` riêng (vòng verify/self-heal) nhưng dùng chung toàn bộ helper. BaseAgent cung cấp: `scan_context(_async)`, `format_history`, `store_memory`/`retrieve_memory` (+ biến thể `_async` chạy ngoài event loop; no-op nếu tắt memory), `resolve_output_file` (sentinel `"None"` = không ghi), `write_output` (ghi UTF-8, lỗi không fatal).

**Đặc thù từng tác tử:**
- **Codex Reviewer** — có **vòng lặp tự chữa**: sau khi LLM sinh code, chạy `flake8` + `pytest` thật; nếu fail thì lặp tối đa `max_retries` (mặc định 3), đưa log test vào lại prompt, trích code bằng `extract_code`, ghi lại file (có chặn path-traversal) rồi chạy lại.
- **Tester** — sinh module pytest, tự chạy `pytest <file>` với `PYTHONPATH` trỏ vào project + `src`, lặp tự chữa khi fail. (`__test__ = False` để pytest không thu thập nhầm.)
- **Security** — ép LLM trả JSON `{blocking, findings[]}`; audit rỗng bị coi là **fail-closed** (chặn).
- **DevOps** — sinh nhiều artifact, mỗi cái trong block riêng có tiền tố `# filepath:`.

---

## 5. Tầng Provider (Local-CLI-first)

`BaseProvider` (ABC) tích hợp **TokenBucket rate limiter** (10 req/s) + `asyncio.Semaphore(5)`, validate phản hồi qua pydantic, xử lý backoff 429 theo `Retry-After`.

Cả 4 backend đều bọc một **CLI vendor cục bộ**; API key đọc trong `__init__` chủ yếu để quyết định hành vi đăng nhập, còn request thật đi qua subprocess CLI. Mọi role mặc định chạy trên `FallbackProvider` (chuỗi per-role ở §3.2, backend lỗi tự fall-through, backend thành công được "ghi nhớ" trong tiến trình):

- **AgyProvider** → `agy` CLI (Antigravity 2.0 / Gemini; primary mặc định của Researcher). Không cần API key — dùng chung phiên đăng nhập Antigravity IDE; chạy `--sandbox` mặc định (`GENIUS_AGY_SANDBOX=0` để tắt), đường dẫn ghi đè bằng `GENIUS_AGY_PATH`.
- **AnthropicProvider** → `claude` CLI (print mode, output JSON; prompt dài ghi ra file tạm; hỗ trợ `GENIUS_MODEL_CLAUDE`).
- **OpenAIProvider** → `codex exec` CLI (Codex Desktop), nạp prompt qua **stdin**, parse **JSONL event stream** (chịu nhiễu tốt), thu thập `agent_message` + token usage. Sandbox điều khiển bằng `GENIUS_CODEX_SANDBOX` (`read-only` mặc định / `workspace-write` / `danger`).
- **GrokProvider** (opt-in, không nằm trong chuỗi mặc định nào) → `grok` CLI. Không có API key thì tự chạy `grok login` phi tương tác — chỉ khi thực sự được gọi. Dùng `--prompt-file`, `--session-id`, `--system-prompt-override`.

**Hạ tầng CLI chống lỗi:**
- `cli_resolver.py` — `which_external()` **loại bỏ** mọi match nằm trong repo (tránh vòng lặp fork-bomb wrapper→run.py→agent→provider→wrapper).
- `cli_runner.py` — `communicate_with_timeout` giới hạn thời gian (mặc định 600s, cấu hình qua `GENIUS_CLI_TIMEOUT`), kill process khi quá hạn; `explain_cli_failure` ánh xạ stderr → gợi ý (hết credit, cần login, thiếu binary → gợi ý chạy `--doctor`).
- `code_extract.py` — trả về **block code lớn nhất** (không nối tất cả block, tránh gộp ví dụ/usage thành file hỏng).

---

## 6. Pipeline điều phối

Định nghĩa trong `orchestrator.py`. Tài liệu `PIPELINE_COMPARISON.md` **cố ý giữ hai pipeline tách biệt** (chỉ dùng chung scaffolding).

### 6.1 `run_pipeline` — tuần tự, đầy đủ 7 giai đoạn
1. **Fast-path slash-command:** nếu prompt bắt đầu bằng lệnh được route → gọi 1 tác tử rồi trả về.
2. **Researcher** (agy-first) research → `research.md`.
3. **Claude** design → `design.md`, có vòng **tranh luận critic⇄Claude** tuỳ chọn (critic = role researcher; early-exit khi `[APPROVED]`).
4. Vòng phản hồi **tương tác** qua stdin (tuỳ chọn).
5. **Parse design → danh sách file**, fan-out từng file dưới `Semaphore(3)`: Codex `/code` → Tester + Security song song → ghi file, chạy pytest, quét lỗ hổng → **self-healing retry**.
6. Tổng hợp `review.md` + `audit.md`, rồi **DevOps** `/deploy` → `deploy.md`.
7. **Fallback** một-file nếu không parse được file nào.

### 6.2 `run_e2e_pipeline` — nhẹ, 4 tác tử
Claude `/plan` → Researcher critique → parse file → mỗi file chạy **hai vòng self-heal tuần tự** (Codex implement gated bởi flake8+pytest, rồi Tester gated bởi pytest). Không MessageBus, không Security/DevOps, không tương tác.

### 6.3 Ngữ cảnh gửi cho agent (R3/R4)
Cả hai pipeline **không còn đổ nguyên workspace** vào mỗi call: mọi điểm scan đi qua `build_budgeted_context` (`ag_core/scanner/repo_graph.py`, chạy off-loop qua `asyncio.to_thread`) — personalized PageRank trên đồ thị import/tham chiếu (Python qua `ast`; JS/TS/Go qua tree-sitter khi có, qua `code_parse.py`), seed = file task nhắc tên (×50) / file định nghĩa identifier trong đề bài (×10), rồi tiêu `GENIUS_CONTEXT_TOKEN_BUDGET` (mặc định 32000, `0` = tắt) theo thứ hạng: seed nguyên văn → full khi còn chỗ → skeleton chữ ký → bỏ. Workspace dưới ngân sách đi qua **nguyên trạng** (identity), lỗi nội bộ bất kỳ cũng trả nguyên input.

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
- **Đồ thị code & chunking (`ag_core/scanner/`)** — `repo_graph.py` (ngữ cảnh budgeted, §6.3); `code_parse.py` (parse cấu trúc per-language: `ast` cho Python, tree-sitter grammar chính chủ cho JS/TS/TSX/Go — fail-soft, thiếu dep = không có info, không lỗi); `graph_index.py` (`RepoIndex` — chỉ mục definition/reference/import + `repo_map` xếp hạng kiểu aider, engine của tool MCP `code_graph`); `project_scanner.py` (`ProjectScanner` quét denylist; `ProjectChunker` đếm token tiktoken + `chunk_files` greedy, và từ R4 có `split_file` **cAST**: chia file quá khổ theo ranh giới AST, lossless, opt-in qua `chunk_files(split_oversized=True)`).

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

`mcp_server.py` (JSON-RPC qua stdio hoặc HTTP) expose **14 tool** + **MCP resources**:
- Đơn tác tử: `research`, `design`, `code`, `unit_test`, `security_audit`, `deploy` (dựng in-process qua `ag_core/agent_factory.py`, bundle stateless).
- Pipeline: **`orchestrate`** (chạy toàn bộ pipeline nền, trả `job_id` ngay; `require_approval: true` → tạm dừng `awaiting_approval` sau research/design/code) + **`orchestrate_status`** (poll → stages/artifacts/awaiting_stage) + **`orchestrate_approve`**/**`orchestrate_reject`**.
- Tiện ích: **`doctor`** (preflight, READY/NOT READY), **`debate`** (critic researcher ⇄ Claude, max 3 vòng, `[APPROVED]` dừng sớm), **`review`** (review code dán vào, không ghi file), **`code_graph`** (truy vấn đồ thị code read-only: `map`/`definition`/`references`/`importers`/`imports`/`skeleton`, JSON out — thêm tool mới phải cập nhật `tests/test_realrun_mcp.py::EXPECTED_TOOLS` vì danh sách tên bị ghim chính xác).
- Resources: artifact pipeline (`research/design/review/audit/deploy/plan.md` + `.bak`) dưới URI `genius://artifacts/<tên>` — whitelist cứng, tên sai → `-32002`. stdout ở chế độ stdio phải là JSON-RPC thuần (log đi stderr).

Đăng ký trong `~/.gemini/antigravity/mcp_config.json` bằng đường dẫn tuyệt đối tới `python.exe`. Cần chạy `python serve.py` (cổng 8001–8006) trước khi dùng `orchestrate`; các tool còn lại chạy in-process.

---

## 12. Kiểm thử & CI

- **Bố cục:** test ở **hai nơi** — `test_*.py` ở root (~45 file) và `tests/` (~15 file). `verify_*.py` là **script thủ công**, không phải gate. `pytest.ini` đặt `norecursedirs = projects .agents`.
- **`conftest.py`:** seed mock key; fixture autouse đổi `SKILL_API_KEY` theo tên file (`valid-api-key` cho `*distributed*`/`*robustness*`/`*milestone3_adversarial*`, còn lại `mock-skill-key`); monkeypatch cho phép plain-SHA256 cho test legacy (trừ `test_upgrades`); tắt debate & cache để xác định.
- **Triết lý "Challenger / Forensic Auditor"** (`TEST_INFRA.md`): test opaque-box theo yêu cầu, dùng **`MockNetworkProtocol`** tiêm lỗi (latency, drop packet, HTTP 429/503/401/400) vào **hub/worker production thật**. Mandate liêm chính: **cấm hardcode kết quả / stub facade** — có auditor độc lập kiểm tra.
- **Số lượng:** **786 test thu thập (784 pass, 2 skip)**, ~2 phút. Các cụm lớn: `test_e2e.py` 74, `tests/test_distributed.py` 71, `test_e2e_phase5.py` 40; R2–R4 bổ sung `tests/test_upgrades.py`, `test_repo_graph.py`, `test_agent_factory.py`, `test_code_graph_index.py`, `test_mcp_code_graph.py`, `test_cast_chunking.py`, `tests/test_realrun_mcp.py` (stdio subprocess thật, ghim chính xác danh sách 14 tool), `tests/test_realrun_hmac.py`.
- **CI (`.github/workflows/ci.yml`):** ma trận per-OS theo nhánh — `windows-latest` cho `win`/`main`, `macos-latest` cho `mac`/`main` (bản mac chạy kèm uvloop) — Python 3.11, `pip install -r requirements.txt` → `python -m pytest` (`pytest-timeout` 300s/test làm backstop treo). Lint **không** nằm trong CI mà ở **pre-commit** (pin black 24.4.2; flake8 nay có trong `requirements.txt` @7.3.0).
- **Không bao giờ chạy 2 tiến trình pytest cùng lúc** trên một máy (dùng chung `genius.db` + service registry).

---

## 13. Tech stack & triển khai

- **Runtime:** pydantic 2.12, FastAPI, uvicorn, httpx, websockets 16, tenacity, PyYAML, python-dotenv, tiktoken, pathspec, tree-sitter (+ grammar js/ts/go — dùng grammar chính chủ, KHÔNG dùng `tree-sitter-language-pack` 1.x vì đã đổi sang binding PyO3 không tài liệu), pytest + pytest-asyncio + pytest-timeout. `requirements.txt` **pin đầy đủ** (3 máy + CI cài y hệt nhau); nhánh `mac` thêm đúng một dòng `uvloop` (không bao giờ merge dòng này sang `win`/`main`).
- **Docker:** `Dockerfile` (python:3.11-slim, expose 8000–8006 + 8080, `CMD python serve.py`); `docker-compose.yml` dựng 8 service trên volume chung `genius_data` (hub, dashboard, và 1 container/vai trò).
- **Cấu hình:** `config.yaml` (metadata, model per-provider, scanner excludes, service URL map) + `.env` (secrets, `ag_core/config.py` đi ngược cây thư mục để tìm). Dưới pytest, URL service được thêm hậu tố `/role` (test và prod giải URL khác nhau). `.agents/service_registry.json` override cổng động.

---

## 14. Tình trạng hiện tại (đã kiểm chứng 2026-07-04, sau R4)

Bốn round nâng cấp liên tiếp (R1 hardening → R2 survey ~34 fix → R3 graph-context → R4 factory/codegraph/tree-sitter/cAST) đã nằm trên cả ba nhánh:

| Gate | Kết quả |
|---|---|
| `python -m pytest` (lệnh CI) | ✅ **784 passed, 2 skipped** (~2 phút) |
| `flake8` | ✅ 0 lỗi |
| `black 24.4.2` (bản pin) | ✅ sạch |
| CI GitHub Actions | ✅ xanh cả 3 nhánh: `win`/`main` @ `c8e1ea4` (windows-latest), `mac` @ `4f5bcba` (macos-latest, chạy kèm uvloop) |
| Distributed thật | ✅ hub trên máy Mac + worker grok trên máy thứ hai (đã chạy chéo máy thành công) |

**Cảnh báo (không chặn):** ~73 warning trong pytest (Deprecation của thư viện ngoài). Sau khi pull nhánh mới, các máy phải `pip install -r requirements.txt` lại (R4 thêm 4 pin tree-sitter).

---

## 15. Đánh giá — Điểm mạnh & Rủi ro

### 15.1 Điểm mạnh
- **Kiến trúc rõ ràng, tách bạch:** vai trò → provider → CLI phân lớp gọn; skill server stateless dễ scale.
- **Bảo mật nghiêm túc cho một dự án nội bộ:** HMAC-only ở production, JWT có chống replay (`jti`), che secret trong log git, chặn path-traversal ở nhiều lớp.
- **Chống chịu tốt:** self-healing loop chạy test thật, degraded mode, doctor, idempotency, timeout CLI, `gather_or_raise`.
- **Văn hoá test mạnh:** 786 test với tiêm lỗi mạng thật (MockNetworkProtocol) + smoke test subprocess stdio MCP thật, mandate cấm giả lập kết quả.

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
├── agents/            # 6 tác tử: researcher (shim grok_researcher), claude_architect,
│                      #           codex_reviewer, tester, security_agent, devops_agent
├── providers/         # agy_provider, anthropic_provider, openai_provider, grok_provider
├── interfaces/        # base_agent (+ template _run_standard), base_provider
├── distributed/       # hub (CentralHub), worker (ClientWorker)
├── memory/            # vector_store (Chroma + TF-IDF/SQLite fallback)
├── scanner/           # project_scanner (ProjectChunker + cAST split_file),
│                      # repo_graph (budgeted context), code_parse (ast + tree-sitter),
│                      # graph_index (RepoIndex — engine của MCP code_graph)
├── utils/             # security, jwt, rate_limiter, message_bus, db, git,
│                      # logger, cli_resolver, cli_runner, code_extract, prompt_templates
├── skill_app.py       # factory dựng FastAPI skill server theo vai trò
├── agent_factory.py   # đường dựng agent duy nhất (build_agent, bundle stateless)
├── provider_factory.py# chuỗi backend per-role + canonical_role + FallbackProvider
├── config.py          # load config.yaml + .env, rewrite URL, port discovery
├── models.py          # DesignPlan / DesignFile (pydantic)
└── diagnostics.py     # preflight doctor
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
| R1 (2026-07-03) | Hardening | Auth fixes, safe binds, hot-path caching, CLI-spawn dedup (`1574733`). |
| R2 (2026-07-03) | Survey-driven | 4 agent khảo sát song song → ~34 fix: event-loop offload, distributed (BoundedTasks/queue caps), MCP (workspace map, approval timeout), dashboard, per-OS CI, pin requirements (`60d8861`). Tách nhánh per-OS `win`/`mac`. |
| R3 (2026-07-04) | Graph context | `repo_graph.py` — ngữ cảnh budgeted theo PageRank + `GENIUS_CONTEXT_TOKEN_BUDGET` (`fe892d4`); mac thêm uvloop. |
| R4 (2026-07-04) | Factory + CodexGraph | Template-method 6 agent + `agent_factory.py`; `code_parse`/`graph_index` + tool MCP `code_graph` (14 tool); tree-sitter JS/TS/Go; cAST `split_file`; vá crash tiềm ẩn stdio-boot (`c8e1ea4`). |

---

*Tài liệu tổng hợp từ khảo sát mã nguồn (orchestrator, serve, skill_app, agents, providers, distributed, security, memory, testing). Bản chi tiết theo commit gốc: xem lịch sử git của `docs/OVERVIEW.md`.*
