# Báo cáo Kiểm tra Dự án Genius (Antigravity 2.0)

> **Ngày kiểm tra:** 2026-06-30
> **Phạm vi:** `E:\project\Genius` @ commit `0bfb2e8`
> **Phương pháp:** Chạy thực tế bộ test (pytest) + lint (flake8) + rà soát mã nguồn `ag_core/` và các entrypoint + đối chiếu toàn bộ tài liệu.
> **Ngôn ngữ:** Tiếng Việt (theo quy ước giao tiếp với người dùng trong `.agents/AGENTS.md`).

---

> ## 🔄 CẬP NHẬT 2026-06-30 — Đã khắc phục nhóm P0 (commit `1c37ed5`)
>
> Báo cáo gốc bên dưới phản ánh trạng thái tại commit `0bfb2e8`. Sau khi khắc phục P0:
> - ✅ **Test: từ 91 failed → `457 passed, 0 failed`** (toàn bộ suite).
> - ✅ **Tầng skill server đã khôi phục** (`ag_core/skill_app.py` + 12 file `.agents/skills/*`, đã track trong git).
> - ✅ **Secret JWT rỗng đã sửa** (fail-fast/fail-closed ở `worker.py` & `serve.py`).
> - ⚠️ **Cờ sandbox Codex** giữ nguyên mặc định theo yêu cầu minh thị của người dùng (`.agents/ORIGINAL_REQUEST.md:668`), thêm tùy chọn `GENIUS_CODEX_SANDBOX=1` để bật lại sandbox.
>
> **Đính chính so với báo cáo gốc:** mục "bảng provider không nhất quán" (§5) **KHÔNG phải lỗi** — `tests/test_bug_fixes.py:278` cố ý khóa `mcp_server deploy → AnthropicProvider`, khác với worker (`devops → OpenAI`). Đây là thiết kế có chủ đích, không sửa.

---

## 1. Tóm tắt điều hành (Executive Summary)

Genius là một framework đa tác tử ("AI software factory") nối 6 agent LLM theo chuỗi: **research → design → code → test → security → devops**. Phần lõi thư viện (`ag_core/`) được thiết kế khá tốt, có HMAC + JWT + chống replay + ghi DB đơn luồng. Tuy nhiên kết quả kiểm tra cho thấy khoảng cách lớn giữa **tài liệu quảng bá** và **thực trạng mã nguồn**.

**Kết luận tổng quan: ⚠️ Dự án KHÔNG ở trạng thái "100% pass / sẵn sàng production" như tài liệu tuyên bố.**

| Hạng mục | Tài liệu tuyên bố | Thực tế đo được |
|---|---|---|
| Test pass | "All 144 tests passing" / "242 passed" | **91 failed, 366 passed** (80% pass) |
| Pipeline mặc định (HTTP) | Hoạt động | **Không chạy được** — thiếu file skill server |
| Dashboard | "TUI Dashboard" (Terminal UI) | Thực ra là **Web Dashboard** (FastAPI/WebSocket, port 8080) |
| RAG sentence-transformers/Chroma | Tính năng cốt lõi | **Không cài đặt mặc định**, chỉ chạy fallback TF-IDF |
| Bảo mật | JWT + HMAC chặt chẽ | Có, nhưng **secret JWT rỗng** ở cấu hình mặc định |

---

## 2. Kết quả kiểm thử thực tế (Test Results)

Lệnh chạy: `python -m pytest` (đúng như CI). Kết quả:

```
91 failed, 366 passed, 80 warnings in 140.47s (0:02:20)
```

- Tổng cộng **457 test** chạy được; **80% pass**, **20% fail**.
- Phân bố 91 lỗi theo file:

| File test | Số lỗi | Nguyên nhân gốc |
|---|---:|---|
| `test_e2e.py` | 31 | Thiếu `.agents/skills/<agent>/api.py` |
| `test_devops_security_challenger.py` | 26 | Thiếu `security_agent`/`devops_agent` api.py |
| `test_e2e_phase5.py` | 20 | Thiếu skill api.py |
| `test_integration.py` | 9 | Thiếu skill api.py |
| `test_slash_commands.py` | 4 | Wrapper CLI gọi `run.py` không tồn tại (`FileNotFoundError`) |
| `test_adversarial_challenger_m4.py` | 1 | `serve.get_api_app` không nạp được api.py |

### 🔴 Phát hiện then chốt: **100% lỗi đều cùng một nguyên nhân**
Tất cả 91 lỗi đều vì các file **`.agents/skills/<agent>/api.py`** và **`run.py`** KHÔNG tồn tại trong repo. Thư mục `.agents/*/` bị `.gitignore` loại trừ (dòng `.agents/*/`), nên tầng "Skill Server" — vốn là **xương sống của pipeline mặc định** — không có mặt trong bản checkout này.

> Tin tốt: phần lõi `ag_core/` (db, message bus, rate limiter, vector memory, providers, security, distributed hub/worker) **pass sạch**. Lỗi tập trung hoàn toàn ở tầng HTTP skill server bị thiếu.

---

## 3. Kiến trúc thực tế: 3 luồng thực thi

Mã nguồn có **3 luồng thực thi chồng lấn**, nhưng **chỉ 2 luồng thực sự hoạt động**:

### Luồng A — HTTP Skill-Server Pipeline (mặc định, ĐANG HỎNG) ❌
- `serve.py:441` `main_async` → `get_api_app(role)` (`serve.py:377`) nạp `.agents/skills/<role>/api.py` qua `importlib` (`serve.py:395`).
- **Các file này không tồn tại** → `FileNotFoundError` với mọi role.
- `orchestrator.py:796` `run_pipeline` được xây hoàn toàn quanh luồng này (POST tới `http://localhost:8001..8006`, `orchestrator.py:291`). **Không có skill server ⇒ pipeline mặc định không chạy end-to-end ngoài test mock.**

### Luồng B — MCP Server (HOẠT ĐỘNG) ✅
- `mcp_server.py` khởi tạo agent trực tiếp in-process (`mcp_server.py:31` `execute_agent`), hỗ trợ cả HTTP (`/tools/call`) và stdio JSON-RPC. Đây là luồng duy nhất gọi agent+provider mà không cần skill server.

### Luồng C — Distributed Hub/Worker (HOẠT ĐỘNG) ✅
- `CentralHub` (`hub.py:43`) định tuyến task in-memory; `ClientWorker` (`worker.py:291`) kết nối qua WebSocket, khởi tạo agent local qua `ROLE_AGENT_MAP` (`worker.py:158`). Có HMAC ký 2 chiều + JWT + chống spoofing.

### Tầng phân lớp
`BaseAgent` (`interfaces/base_agent.py:6`) chứa `provider` + `VectorMemory` + `GitManager`; `BaseProvider` định nghĩa `send_prompt`. **Cả 3 provider đều shell ra CLI local** (`claude`/`codex`/`grok`), **không gọi HTTP API** — trường `api_key`/`base_url` được set nhưng **không dùng** (cấu hình chết).

---

## 4. Phát hiện nghiêm trọng (Critical Findings)

### 4.1 🔴 Tầng Skill Layer không tồn tại trong repo
`.agents/skills/` không có thư mục agent nào (chỉ có `AGENTS.md` + báo cáo). Nhưng `PROJECT.md`, `Genius_Comprehensive_Report.md`, `verify_devops_security.py`, và các script wrapper `claude`/`codex`/`grok`/`tester` đều tham chiếu tới `.agents/skills/<agent>/{run,api}.py`. **Mọi đường dẫn này sẽ FileNotFoundError.** Đây là nguyên nhân của toàn bộ 91 test fail và khiến Luồng A + Docker (các role agent) không khởi động được.

### 4.2 🔴 Secret JWT rỗng ở cấu hình mặc định
`worker.generate_jwt` (`worker.py:284`) và WS handler (`serve.py:202`) tính `secret = os.getenv("SKILL_API_KEY", "" if pytest else "")` — biểu thức ternary là **no-op, cả 2 nhánh đều `""`**. Không có `SKILL_API_KEY` ⇒ `encode_jwt` ném `ValueError` (`jwt.py:29`) ⇒ distributed mode âm thầm không xác thực được. Secret rỗng/chia sẻ ⇒ xác thực gần như **tắt** ở cấu hình mặc định/test.

### 4.3 🔴 Code agent chạy với sandbox bị TẮT
`OpenAIProvider` truyền `--dangerously-bypass-approvals-and-sandbox` **vô điều kiện** cho codex CLI (`openai_provider.py:70, :78`). Agent sinh code rồi **thực thi code đó qua pytest** với mọi approval/sandbox bị vô hiệu hóa — rủi ro cao.

### 4.4 🟠 Pipeline mặc định không tương thích Docker
Provider dò tìm `codex.exe` trong `%LocalAppData%` / WindowsApps và shell ra `grok` CLI (Windows-only); CI là `windows-latest`. **Không có cái nào chạy trên Docker image Linux** (`python:3.11-slim`) — providers local-CLI và Dockerfile mâu thuẫn nhau.

---

## 5. Chất lượng mã nguồn (Code Health)

### Lint (flake8, ngưỡng mặc định 79 ký tự)
Tổng **1482 cảnh báo**, nhưng phần lớn là **nhiễu style** (flake8 mặc định 79 trong khi black dùng 88):
- 809 × E501 (dòng dài), 449 × W293 (whitespace dòng trống), 113 × E302, 31 × E402.
- **Đáng xử lý thực sự:** 10 × F401 (import thừa), 6 × F841 (biến `e` bắt exception nhưng không dùng), 2 × F824 (`global` thừa), 6 × F541 (f-string thiếu placeholder).
- *Khuyến nghị: thêm file cấu hình flake8 với `max-line-length = 88` để loại bỏ nhiễu E501 và lộ ra lỗi thật.*

### Trùng lặp nặng (Duplication)
- **6 agent copy-paste gần như nguyên khối** chuỗi scan→context→memory→history→prompt→log→write (so sánh `claude_architect.py:36-121` với `codex_reviewer.py:36-91`, `grok_researcher.py:36-91`, v.v.). Không có phần boilerplate nào được đẩy lên `BaseAgent`.
- **2 pipeline ~540 dòng gần trùng nhau:** `run_pipeline` (`orchestrator.py:796-1333`) và `run_e2e_pipeline` (`orchestrator.py:1334+`) — trùng ~90%.
- `_extract_code` lặp ở `codex_reviewer.py:93` và `tester.py:82`.
- **Bảng provider lặp & mâu thuẫn:** `worker.py:158` map devops→OpenAI, nhưng `mcp_server.py:33` map deploy→Anthropic.

### Xử lý lỗi
- **38 khối `except: pass`** trong riêng `ag_core/` — nuốt lỗi âm thầm (hub 5, message_bus 4, vector_store 4, scanner 6, db 3, git 3...).
- **Provider xử lý lỗi không nhất quán:** `OpenAIProvider` kiểm `returncode != 0` và ném lỗi (`openai_provider.py:114`), nhưng `AnthropicProvider`/`GrokProvider` **không** — CLI thất bại âm thầm trả `content=""`.
- `is_transient_error` coi `ChecksumMismatchError` là **có thể retry** (`orchestrator.py:239`) — retry trên lỗi bảo mật thay vì fail nhanh; còn log `DEBUG_ERR:` rò nội dung exception (`orchestrator.py:238`).

### Mã chết & rác
- `base_provider.py:47-78` `wait_retry_after` + toàn bộ máy móc httpx/Retry-After **không dùng** (providers là CLI).
- Nhánh "plain checksum" trong `checksum_middleware` (`security.py:124`) là **dead code** (`is_plain` luôn `False`).
- File rác ở root: `dummy_cli.py`, `temp_invalid.yaml`, `design_spec.json` (19 byte), `run_debug.py`, `genius.db`/`*.db` (bị commit dù `.gitignore` loại trừ), `test_openai_provider_robustness.py` trùng ở cả root và `tests/`.

### Mã phụ thuộc môi trường test
Config, rate limiter, orchestrator, providers đều rẽ nhánh theo `PYTEST_CURRENT_TEST`/`"pytest" in sys.modules` (`config.py:154`, `rate_limiter.py:66`, `orchestrator.py:832`, `codex_reviewer.py:124`). Vòng lặp self-healing mock output linter/pytest ⇒ **hành vi cốt lõi của nó không được kiểm thử thật trong CI**.

---

## 6. Mâu thuẫn & sai lệch tài liệu

Các con số test trong tài liệu **không cái nào khớp nhau** (đều là snapshot từ các thời kỳ khác nhau, chưa bao giờ được hợp nhất):

| Tài liệu | Tuyên bố | Ghi chú |
|---|---|---|
| `HANDOFF_ROADMAP.md` | "32 tests" | Thời kỳ monolith 3-agent — **đã lỗi thời hoàn toàn** |
| `PROJECT.md` | "All 144 tests passing" | 22 milestone đều "DONE" |
| `TEST_INFRA.md` | "exactly 71 test cases" | |
| `test_report.md` | "243 collected, 242 passed" | |
| **Thực tế (2026-06-30)** | **457 chạy, 366 passed, 91 failed** | Đo trực tiếp |

Sai lệch nội dung khác:
- **README sai về Dashboard:** README mô tả "TUI Dashboard / giao diện Terminal UI"; thực tế `dashboard.py` dùng FastAPI + WebSocket + HTMLResponse → là **Web Dashboard port 8080** (không có `rich`/`textual`/`curses`).
- **API key "đã gỡ bỏ" — chưa đúng:** `Genius_Comprehensive_Report.md` nói đã "loại bỏ hoàn toàn" 3 API key, nhưng `conftest.py` vẫn set cả 3 và providers vẫn tham chiếu chúng làm fallback.
- **RAG sentence-transformers/Chroma quảng bá nhưng không cài:** `chromadb` và `sentence-transformers` **không có trong `requirements.txt`**, chỉ nằm sau `try/except` (`vector_store.py:14-21`). Hệ thống thực chất chạy fallback `SimpleTFIDFEmbedding` + SQLite (quét O(n), không có ANN index). Test `test_chroma_store_skip_or_run` chính là test bị skip.

> **Khuyến nghị:** tin `CLAUDE.md` và mã nguồn hơn các tài liệu marketing (README) và milestone tracker.

---

## 7. Bảo mật (điểm mạnh & điểm yếu)

**Điểm mạnh:**
- HMAC-SHA256 trên canonical JSON, dùng `hmac.compare_digest` (`security.py:34`).
- JWT tự cài HS256, **pin `alg`** (chống alg-confusion, `jwt.py:69`), kiểm `exp`, **chống replay** qua bảng `seen_jtis` + jti (`jwt.py:94-117`).
- Hub & worker xác thực API key + checksum 2 chiều; WS phát hiện spoofing danh tính (`serve.py:221`).

**Điểm yếu (ngoài 4.2, 4.3):**
- `hub.verify_auth` so sánh API key bằng `==` (`hub.py:177`) thay vì `compare_digest` — rò side-channel thời gian.
- `hub` `/write_workspace_file` (`hub.py:361`) cho ghi file tùy ý với bộ lọc path sơ sài (`..`/`/`/`:`) — worker/client từ xa có thể ghi file vào CWD của hub.
- **Phụ thuộc nguy hiểm vào conftest:** `conftest.py` monkeypatch `verify_checksum`/`verify_raw_body_checksum` để **cho phép lại plain SHA-256** (production chỉ chấp nhận HMAC). Trạng thái test xanh một phần dựa vào việc **nới lỏng bảo mật production** — trừ các test `test_upgrades*` chạy đường HMAC thật. Người sửa code bảo mật phải hiểu monkeypatch này, nếu không test cho tín hiệu sai.

---

## 8. Phụ thuộc (Dependency Gaps)

`requirements.txt` có 14 gói. Thiếu/sai lệch:
- **Import nhưng KHÔNG có trong requirements:** `chromadb`, `sentence-transformers` (quảng bá là cốt lõi nhưng chỉ là nhánh fallback chết); `starlette` (chỉ kéo gián tiếp qua fastapi).
- **`black`** chỉ có trong `.pre-commit-config.yaml`, không trong requirements (đã ghi trong `CLAUDE.md`).
- **Không có `.env.example`** dù cần `SKILL_API_KEY`, `GENIUS_DB_PATH`, `GENIUS_MEMORY_DB_PATH`, và 3 LLM key. Clone mới **không chạy được** nếu chưa tạo `config.yaml` + DB (đều bị gitignore).
- JWT tự cài, không dùng PyJWT (chấp nhận được, chỉ lưu ý không có thư viện kiểm chứng).

---

## 9. Khuyến nghị theo thứ tự ưu tiên

### 🔴 P0 — Chặn vận hành (phải làm trước)
1. **Khôi phục/sinh tầng skill server:** tạo `.agents/skills/<agent>/api.py` + `run.py` cho cả 6 agent, hoặc bỏ gitignore và commit chúng. Đây là điều kiện để pipeline mặc định và 91 test fail hoạt động lại.
2. **Sửa lỗi secret JWT rỗng** (`worker.py:284`, `serve.py:202`): bỏ ternary no-op, bắt buộc `SKILL_API_KEY` có giá trị (fail-fast nếu rỗng ở chế độ distributed).
3. **Đánh giá lại `--dangerously-bypass-approvals-and-sandbox`** (`openai_provider.py:70,78`): chỉ bật khi có cờ rõ ràng, mặc định bật sandbox.

### 🟠 P1 — Toàn vẹn & độ tin cậy
4. **Đồng bộ tài liệu với thực tế:** cập nhật con số test, sửa "TUI"→"Web" Dashboard, làm rõ RAG là fallback TF-IDF. Đánh dấu `HANDOFF_ROADMAP.md` là lịch sử.
5. **Thêm `.env.example`** và tài liệu hóa biến môi trường + cách tạo `config.yaml`.
6. **Quyết định về RAG:** hoặc thêm `chromadb`/`sentence-transformers` vào requirements, hoặc ngừng quảng bá là tính năng cốt lõi.
7. **Thống nhất bảng provider** giữa `worker.py` và `mcp_server.py` (devops/deploy).
8. **Thống nhất xử lý lỗi provider:** Anthropic/Grok cũng nên ném lỗi khi CLI thất bại (như OpenAI).

### 🟡 P2 — Chất lượng & bảo trì
9. **Khử trùng lặp:** đẩy boilerplate 6 agent lên `BaseAgent`; gộp `run_pipeline`/`run_e2e_pipeline`.
10. **Thêm cấu hình flake8** (`max-line-length=88`) để lộ lỗi thật; xử 10 F401 + 6 F841 + 2 F824.
11. **Dọn rác:** xóa `dummy_cli.py`, `temp_invalid.yaml`, `design_spec.json`, `run_debug.py`; gỡ `genius.db`/`*.db` khỏi git tracking; bỏ test trùng.
12. **Bảo mật phụ:** `hub.verify_auth` dùng `compare_digest`; siết bộ lọc path `/write_workspace_file`; không retry trên `ChecksumMismatchError`.
13. **Tách mã test khỏi production:** giảm rẽ nhánh `PYTEST_CURRENT_TEST`; mock self-healing loop ít hơn để CI kiểm thử thật.

---

## 10. Phụ lục — Số liệu

- **Quy mô:** ~22.2k dòng Python; `ag_core/` ~4.2k dòng; `orchestrator.py` ~1.7k dòng (86 KB); 45 file test (29 ở root + 16 trong `tests/`); 11 commit.
- **Test:** 457 chạy → 366 passed / 91 failed / 80 warnings / 140s. 1 test skip (`chromadb` vắng mặt).
- **Lint:** 1482 cảnh báo flake8 (≈85% là E501/W293 nhiễu style); 24 cảnh báo logic thật (F401/F841/F824/F541).
- **Cổng dịch vụ:** Hub 8000, Grok 8001, Claude 8002, Codex 8003, Tester 8004, Security 8005, DevOps 8006, Dashboard 8080.

---

*Báo cáo được tạo tự động qua kiểm tra động (chạy thật test + lint) và rà soát mã nguồn tĩnh. Mọi tham chiếu `file:line` trỏ tới commit `0bfb2e8`.*
