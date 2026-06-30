# Genius Multi-Agent Framework 🚀
**(Antigravity 2.0 - Microservices & AI Devkit Edition)**

Genius là một hệ thống siêu tác tử (Agentic Framework) tự trị chuyên dụng cho việc lập trình, refactor mã nguồn và kiểm thử phần mềm tự động. Điểm đột phá lớn nhất của Genius là sự kết hợp giữa **môi trường Local CLI 100%** và **Kiến trúc Microservices**, cho phép các AI Agent chạy đa luồng, giao tiếp chéo và bảo mật tuyệt đối.

## 🌟 Tính năng Cốt lõi (V2 Upgrades)

1. **Kiến trúc Phân tán & Đa luồng (Parallel Execution):** 
   - Thay vì chạy tuần tự, `orchestrator.py` giờ đây sử dụng `asyncio.gather` để điều phối các Agent hoạt động song song (ví dụ: Tester và Security cùng chạy một lúc).
   - Kiến trúc Stateless API cho phép scale hệ thống dễ dàng.

2. **Cơ sở dữ liệu Memory chuẩn (SQLite WAL):**
   - Không còn sợ mất ngữ cảnh! Mọi đoạn hội thoại, SPO (Standardized Prompt Object) và trạng thái của Agent đều được lưu trữ qua SQLite với chế độ WAL (Write-Ahead Logging) siêu tốc và an toàn.

3. **Giao tiếp Message Bus & A2A:**
   - Cơ chế nội bộ cho phép các tác tử gửi và nhận tin nhắn chéo cho nhau (Agent-to-Agent) qua Mailbox, được quản lý độc lập.

4. **Vector Store & RAG (Retrieval-Augmented Generation):**
   - Tích hợp `sentence-transformers` (tf-keras) để Agent tự động ghi nhớ và tìm kiếm theo ngữ cảnh semantic.

5. **TUI Dashboard Giám sát theo thời gian thực:**
   - Khởi chạy `dashboard.py` để xem trực tiếp trạng thái, log, CPU/RAM usage và mailbox của từng Agent dưới dạng giao diện Terminal UI.

6. **Bảo mật & Rate Limiting:**
   - Áp dụng thuật toán TokenBucketRateLimiter có lock-safety cho asyncio loop, giới hạn tốc độ call mô hình và chống spam.
   - Hỗ trợ ký điện tử JWT & HMAC-SHA256 Payload Validation giữa Orchestrator và Skill Server.

## 🤖 Đội hình Tác tử (Agents)

Hệ thống có 6 Tác tử cốt lõi, mỗi tác tử chạy trên một API Port độc lập:
- **Grok Researcher** (Port 8001): Phân tích tài liệu, thu thập yêu cầu (Sử dụng Grok CLI, hỗ trợ auto-login qua session-id).
- **Claude Architect** (Port 8002): Kiến trúc sư thiết kế hệ thống, lên bản vẽ thư mục và logic.
- **Codex Reviewer** (Port 8003): Chuyên gia code và refactor, tích hợp siêu sâu qua Codex Desktop CLI JSONL streams.
- **Tester Agent** (Port 8004): QA tự động viết Unit/Integration/E2E test.
- **Security Agent** (Port 8005): Giám định viên rà quét lỗ hổng bảo mật và audit mã nguồn.
- **DevOps Agent** (Port 8006): Chuyên trách Dockerize, CI/CD và deployment.

## 🚀 Hướng dẫn Cài đặt & Sử dụng

### 1. Yêu cầu Hệ thống
- Python 3.10+
- (Khuyên dùng) `uv` hoặc `pip`
- Docker (Tuỳ chọn)

Cài đặt thư viện:
```bash
pip install -r requirements.txt
```

### 2. Khởi chạy Hệ thống

**Cách 1: Khởi động Menu tương tác (Interactive Boot)**
Bạn có thể tự do chọn Agent nào muốn khởi chạy thông qua CLI Menu:
```bash
python serve.py
```

**Cách 2: Khởi động Trưởng nhóm (Orchestrator)**
Đánh thức người quản lý chính để phân phối việc tự động:
```bash
python orchestrator.py
```

**Cách 3: Khởi động Bảng điều khiển (TUI Dashboard)**
Mở Terminal mới và chạy để theo dõi toàn bộ hệ thống đang làm việc:
```bash
python dashboard.py
```

### 3. Cấu hình (`config.yaml`)
Toàn bộ hệ thống giờ đây được gom về một cấu hình chung tại `config.yaml`. Bạn có thể tuỳ chỉnh:
- Giới hạn memory/log
- Secret keys cho JWT/HMAC
- Thông số của Vector Store

---

## 🔌 Tích hợp vào Antigravity 2.0 (MCP — Điều phối viên)

Genius có thể gắn trực tiếp vào **Google Antigravity 2.0** như một **điều phối viên (orchestrator)** thông qua giao thức **MCP (Model Context Protocol)**. Khi đó Antigravity gọi được toàn bộ pipeline đa tác tử của Genius như những "skill" gốc.

### MCP Server (`mcp_server.py`)
Khởi chạy ở chế độ stdio (giao thức Antigravity dùng):
```bash
python mcp_server.py stdio      # chế độ MCP stdio cho Antigravity
python mcp_server.py            # (tuỳ chọn) chế độ HTTP, cổng 8000
```

Server hỗ trợ đầy đủ handshake MCP (`initialize` / `notifications/initialized` / `ping`) và expose **8 tool**:

| Tool | Chức năng |
|------|-----------|
| `research`, `design`, `code`, `unit_test`, `security_audit`, `deploy` | Gọi từng tác tử đơn lẻ (in-process) |
| `orchestrate` | **Chạy TOÀN BỘ pipeline** (research → design → code → test + security + deploy). Trả về `job_id` ngay lập tức |
| `orchestrate_status` | Poll trạng thái job (`running` / `completed` / `failed`) và lấy artifacts khi xong |

> `orchestrate` chạy pipeline dưới dạng tác vụ nền (async background job) nên không làm Antigravity bị treo chờ. Vì nó định tuyến qua các Skill Server FastAPI, **cần chạy `python serve.py` trước** (các cổng 8001–8006).

### Đăng ký vào Antigravity
Thêm Genius vào file cấu hình MCP của Antigravity tại `~/.gemini/antigravity/mcp_config.json` (giữ nguyên các server sẵn có, chỉ **merge** thêm khoá `genius`):

```json
{
  "mcpServers": {
    "genius": {
      "command": "C:\\Users\\<bạn>\\AppData\\Local\\Programs\\Python\\Python311\\python.exe",
      "args": ["E:\\Project\\Genius\\mcp_server.py", "stdio"]
    }
  }
}
```

- Dùng **đường dẫn tuyệt đối tới `python.exe`** (do `python` thường không nằm trong PATH; có thể lấy bằng `py -c "import sys; print(sys.executable)"`).
- **Không cần khối `env` chứa API key**: các CLI tự xác thực (`grok login`, app desktop của Codex/Claude), còn `skill_api_key` được đọc nhất quán từ `config.yaml` ở cả hai phía.
- Sau khi sửa file, **khởi động lại Antigravity** để nó nạp MCP server `genius`.

### Quy trình dùng từ Antigravity
1. `python serve.py` — bật các Skill Server (8001–8006).
2. Trong Antigravity, gọi tool `orchestrate` với `prompt` (mô tả việc cần build) → nhận `job_id`.
3. Gọi `orchestrate_status` với `job_id` để theo dõi, đến khi `status = completed` thì nhận artifacts (research/design/code/review/tests/security/deploy).

---
> **Lưu ý V2**: Hệ thống có khả năng fallback thông minh. Nếu thiếu API Key, GrokProvider sẽ tự động mở login prompt; nếu mã sinh ra bị độc hại, vòng lặp `Self-Healing` sẽ tự động vá lỗi thông qua phản hồi từ `pytest` và `flake8`.
