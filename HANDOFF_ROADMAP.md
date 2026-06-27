# HANDOFF ROADMAP - Antigravity 2.0 Enterprise Core Framework

## 1. Dự án là gì & Kiến trúc cốt lõi

### Dự án là gì?
**Antigravity 2.0 Enterprise Core Framework** là một khung điều phối cấp doanh nghiệp được thiết kế để kết nối và tự động hóa các tác nhân trí tuệ nhân tạo chuyên biệt (**Grok Researcher**, **Claude Architect**, **Codex Reviewer**) thông qua một quy trình tuần tự (sequential pipeline). Khung làm việc này tối ưu hóa chu trình phát triển phần mềm tự động từ khâu nghiên cứu yêu cầu, thiết kế kiến trúc hệ thống, phát triển mã nguồn cho đến rà soát và đánh giá bảo mật/chất lượng mã nguồn.

### Kiến trúc cốt lõi (Core Architecture)
Hệ thống tuân thủ mô hình phân lớp chặt chẽ nhằm đảm bảo tính mở rộng và khả năng thay thế linh hoạt:
$$\text{Skill} \longrightarrow \text{Agent} \longrightarrow \text{Provider} \longrightarrow \text{API}$$

1. **Skill (Kỹ năng)**: Định nghĩa các chỉ dẫn nghiệp vụ (`SKILL.md`) và mã nguồn khởi chạy tác nhân (`run.py`) được lưu trữ tại thư mục `.agents/skills/*`. Skill đóng vai trò cấu hình hóa hành vi nghiệp vụ cho từng Agent.
2. **Agent (Tác nhân)**: Các lớp tác nhân thừa kế từ `BaseAgent` (trong `ag_core/interfaces/base_agent.py`), bao gồm:
   - `GrokResearcherAgent`: Thực hiện quét thông tin, thu thập tài liệu nghiệp vụ.
   - `ClaudeArchitectAgent`: Đọc mã nguồn dự án qua Project Scanner và xuất bản thiết kế hệ thống.
   - `CodexReviewerAgent`: Đọc mã nguồn sản phẩm và thực hiện đánh giá, rà lỗi.
3. **Provider (Nhà cung cấp API)**: Các lớp điều phối kết nối API thừa kế từ `BaseProvider` (trong `ag_core/interfaces/base_provider.py`), như `OpenAIProvider`, `AnthropicProvider`, và `GrokProvider`. Lớp này chuẩn hóa định dạng yêu cầu/phản hồi và quản lý vòng đời kết nối.
4. **API**: Giao tiếp trực tiếp với các mô hình ngôn ngữ lớn (LLM) qua giao thức HTTP (Anthropic, OpenAI, xAI).

### Các quy tắc tiêu chuẩn kỹ thuật (Standard Rules)
* **Async/Await Pattern**: Toàn bộ luồng quét tập tin, gọi API của Provider và chạy quy trình của Agent đều sử dụng lập trình bất đồng bộ (`asyncio`) nhằm tối ưu hóa hiệu năng và tránh nghẽn I/O.
* **Tenacity Retry Logic**: Sử dụng thư viện `tenacity` để bọc các hàm gọi API của Provider, tự động bắt lỗi kết nối, lỗi timeout và thực hiện thử lại (retry) với thuật toán Exponential Backoff (lùi lũy thừa).
* **Pydantic Schemas**: Định nghĩa cấu trúc cấu hình hệ thống trong `ag_core/config.py` bằng Pydantic Model (`Config`, `ModelConfig`, `ScannerConfig`), giúp tự động kiểm tra kiểu dữ liệu, các giá trị mặc định khi phân tích cấu hình từ tệp tin YAML.
* **Pathspec Integration**: Tích hợp công cụ `pathspec` vào `ProjectScanner` để xử lý các mẫu loại trừ tương thích định dạng `.gitignore`, đảm bảo trình quét dự án loại bỏ chính xác các tệp không cần thiết (thư mục ảo, tệp tạm, credential).
* **Tiktoken Context Estimator**: Sử dụng thư viện `tiktoken` để tính toán số lượng token của prompt cục bộ, giúp dự báo và quản lý tốt giới hạn cửa sổ ngữ cảnh (context window limit) trước khi truyền tải thông tin đến API.

---

## 2. Tiến độ hiện tại

### Các thành phần đã hoàn thành và hoạt động tốt (Complete Components)
Hệ thống đã triển khai đầy đủ các cấu trúc nền tảng và đã kiểm thử thành công qua bộ kiểm thử tự động gồm **32 bài test** (unit, integration, và stress tests):
* **Cấu trúc lõi & Cấu hình**: `ag_core/config.py`, `ag_core/interfaces/base_agent.py`, `ag_core/interfaces/base_provider.py`.
* **Nhà cung cấp LLM**: OpenAI (`openai_provider.py`), Anthropic (`anthropic_provider.py`), Grok (`grok_provider.py`).
* **Trình quét dự án & Nhật ký**: `ag_core/scanner/project_scanner.py` và hệ thống ghi log giao dịch `ag_core/utils/logger.py`.
* **Tác nhân nghiệp vụ**: `ClaudeArchitectAgent`, `CodexReviewerAgent`, `GrokResearcherAgent`.
* **Trình điều phối dòng công việc CLI**: `orchestrator.py` thiết lập quy trình tuần tự kết nối 4 AI: Grok (Research) -> Claude (Design) -> Antigravity (Programming) -> Codex (Review).

### Nguyên nhân gây sập hệ thống và các khoảng trống (Crash & Gaps)
1. **HTTP 429 - Rate limit handling setup**: Trong các tình huống tải cao (Stress Testing) hoặc khi các Agent gọi API dồn dập, hệ thống chưa có cơ chế điều tiết lưu lượng chủ động (client-side rate limiter) dẫn tới việc bị máy chủ API từ chối với mã lỗi `HTTP 429 Too Many Requests` và gây sập luồng điều phối.
2. **Thiếu khai báo thư viện trong `requirements.txt`**: Tệp tin `requirements.txt` hiện đang thiếu hai thư viện cốt lõi là `httpx` (để thực hiện các yêu cầu HTTP bất đồng bộ) và `python-dotenv` (để tải cấu hình từ tệp `.env`).
3. **Thiếu lệnh ánh xạ/Wrapper trên PATH của hệ thống**: Các công cụ dòng lệnh `grok` và `codex` được gọi bởi `orchestrator.py` không thực sự tồn tại trong biến môi trường PATH của hệ điều hành, dẫn đến lỗi không tìm thấy câu lệnh khi chạy thực tế.
4. **Không khớp hợp đồng đầu ra (Output Mismatch)**: Lớp `ClaudeArchitectAgent` mặc định ghi kết quả ra tệp `architecture.md`, trong khi trình điều phối `orchestrator.py` lại tìm kiếm kết quả tại tệp `design.md` để truyền làm đầu vào cho bước tiếp theo.
5. **Kịch bản chạy Skill thiếu khả năng nhận tham số dòng lệnh**: Các tệp khởi chạy mẫu (`.agents/skills/*/run.py`) được cấu hình tĩnh và hoàn toàn không xử lý tham số đầu vào (như `--input`, `--output`, `--prompt`), làm hạn chế khả năng tái sử dụng.
6. **Rủi ro rò rỉ thông tin bảo mật (Secret Leak Hazard)**: Kho lưu trữ mã nguồn chưa được khởi tạo Git (`git init`), đồng thời không có tệp cấu hình loại trừ `.gitignore`. Tệp chứa khóa API cấu hình cục bộ (`.env`) đang ở trạng thái không được quản lý, dễ dẫn đến việc vô tình đẩy các thông tin nhạy cảm lên hệ thống quản lý mã nguồn.

---

## 3. Mục tiêu tiếp theo (Next Steps)

Dưới đây là danh sách các nhiệm vụ cụ thể cần triển khai để hoàn thiện và ổn định hệ thống core:

### 📑 1. Cấu hình bảo mật và Quản lý mã nguồn (Git Setup)
- [ ] **Khởi tạo mã nguồn**: Thực hiện lệnh `git init` trong thư mục gốc của dự án.
- [ ] **Tạo tệp loại trừ `.gitignore`**: Viết tệp `.gitignore` tại thư mục gốc với các cấu hình loại trừ sau:
  - Loại trừ các tệp cấu hình nhạy cảm và thông tin cá nhân: `.env`, `config.yaml`.
  - Loại trừ thư mục cache và ảo của Python: `__pycache__/`, `*.pyc`, `.pytest_cache/`.
  - Loại trừ các tệp trung gian do pipeline tạo ra: `research.md`, `design.md`, `app.py`, `review.md`, `architecture.md`.

### 📦 2. Cập nhật khai báo gói phụ thuộc (`requirements.txt`)
- [ ] **Bổ sung thư viện thiếu**: Cập nhật `requirements.txt` tại thư mục gốc để bổ sung:
  - `httpx>=0.27.0` (đảm bảo hỗ trợ Client bất đồng bộ).
  - `python-dotenv>=1.0.1` (hỗ trợ đọc cấu hình biến môi trường tự động).

### 🛠️ 3. Đồng bộ hóa đầu ra của Tác nhân Claude (Contract Alignment)
- [ ] **Điều chỉnh mặc định**: Cập nhật tệp `ag_core/agents/claude_architect.py` hoặc điều chỉnh tham số truyền từ `orchestrator.py` để đảm bảo kết quả đầu ra của bước thiết kế luôn được ghi nhận thống nhất tại tệp `design.md` thay vì `architecture.md`.

### ⚙️ 4. Xây dựng CLI Wrappers cho Grok và Codex
- [ ] **Tạo CLI mô phỏng/thực tế**: Cung cấp các lệnh thực thi hoặc tệp kịch bản wrapper (ví dụ: `grok.cmd`/`grok` và `codex.cmd`/`codex` trỏ tới `dummy_cli.py` hoặc các hàm thực thi tương ứng của `run.py`) và hướng dẫn đưa chúng vào PATH của hệ thống để `orchestrator.py` có thể gọi trực tiếp thông qua `subprocess.run()`.

### 📥 5. Phát triển tham số dòng lệnh cho kịch bản chạy mẫu (Argparse in Skills)
- [ ] **Cập nhật `.agents/skills/grok_researcher/run.py`**: Bổ sung thư viện `argparse` để tiếp nhận tham số dòng lệnh `--query` (hoặc `--prompt`) và `--output`.
- [ ] **Cập nhật `.agents/skills/claude_architect/run.py`**: Tiếp nhận tham số `--input` (tệp nghiên cứu đầu vào) và `--output` (tệp thiết kế đầu ra).
- [ ] **Cập nhật `.agents/skills/codex_reviewer/run.py`**: Tiếp nhận tham số `--code` (tệp mã nguồn cần rà soát) và `--output` (tệp đánh giá đầu ra).

### 🚦 6. Xử lý giới hạn tần suất gọi API (HTTP 429 Handling)
- [ ] **Tích hợp Rate Limiter**: Triển khai cơ chế Token Bucket hoặc Semaphore cục bộ trong `BaseProvider` hoặc các Provider con để giới hạn tần suất yêu cầu đồng thời (Requests Per Minute/Tokens Per Minute).
- [ ] **Tối ưu hóa Tenacity**: Cấu hình bộ lọc Exception trong `tenacity.retry` để phát hiện lỗi HTTP 429 (ví dụ: từ thư viện `httpx` hoặc API client) và tự động đọc tiêu đề `Retry-After` để dừng luồng xử lý trước khi thực hiện thử lại một cách thông minh.

---

## 4. Cấu trúc thư mục (Tree)

Dưới đây là sơ đồ cấu trúc thư mục hiện tại của dự án **Antigravity 2.0 Enterprise Core Framework**:

```text
e:/tool/Genius/
├── .agents/                          # Thư mục chứa metadata của các agent và kỹ năng
│   ├── skills/                       # Định nghĩa các Skill của dự án
│   │   ├── claude_architect/
│   │   │   ├── run.py                # Kịch bản khởi chạy Claude Architect Agent
│   │   │   └── SKILL.md              # Tài liệu hướng dẫn kỹ năng
│   │   ├── codex_reviewer/
│   │   │   ├── run.py                # Kịch bản khởi chạy Codex Reviewer Agent
│   │   │   └── SKILL.md              # Tài liệu hướng dẫn kỹ năng
│   │   └── grok_researcher/
│   │       ├── run.py                # Kịch bản khởi chạy Grok Researcher Agent
│   │       └── SKILL.md              # Tài liệu hướng dẫn kỹ năng
│   └── worker_milestone2_1/          # Thư mục làm việc của tác nhân hiện tại
│       ├── BRIEFING.md               # Tệp ghi nhớ trạng thái tác vụ
│       ├── ORIGINAL_REQUEST.md       # Lưu giữ yêu cầu gốc từ hệ thống
│       └── progress.md               # Ghi nhận tiến độ làm việc
├── ag_core/                          # Thư mục chứa mã nguồn cốt lõi của framework
│   ├── __init__.py
│   ├── agents/                       # Triển khai các AI Agent chuyên biệt
│   │   ├── __init__.py
│   │   ├── claude_architect.py       # Tác nhân thiết kế hệ thống
│   │   ├── codex_reviewer.py         # Tác nhân rà soát mã nguồn
│   │   └── grok_researcher.py        # Tác nhân nghiên cứu và tìm kiếm thông tin
│   ├── config.py                     # Quản lý cấu hình dự án thông qua Pydantic
│   ├── interfaces/                   # Định nghĩa giao diện trừu tượng
│   │   ├── __init__.py
│   │   ├── base_agent.py             # Lớp cơ sở trừu tượng cho Agent
│   │   └── base_provider.py          # Lớp cơ sở trừu tượng cho Provider
│   ├── providers/                    # Triển khai các bộ kết nối API mô hình lớn
│   │   ├── __init__.py
│   │   ├── anthropic_provider.py     # Kết nối API Anthropic (Claude)
│   │   ├── grok_provider.py          # Kết nối API xAI (Grok)
│   │   └── openai_provider.py        # Kết nối API OpenAI (GPT)
│   ├── scanner/                      # Module quét mã nguồn dự án
│   │   ├── __init__.py
│   │   └── project_scanner.py        # Trình quét dự án tích hợp pathspec
│   └── utils/                        # Các công cụ tiện ích phụ trợ
│       ├── __init__.py
│       └── logger.py                 # Hệ thống ghi nhật ký giao dịch Token và API
├── .env                              # Tệp chứa biến môi trường và khóa API cục bộ (cần đưa vào .gitignore)
├── config.yaml                       # File cấu hình định nghĩa model và scanner pattern
├── dummy_cli.py                      # CLI giả lập dùng cho kiểm thử tích hợp dòng lệnh
├── orchestrator.py                   # CLI điều phối chính chuỗi công việc tuần tự 4-AI
├── requirements.txt                  # Khai báo các thư viện phụ thuộc của dự án
├── test_integration.py               # Kiểm thử tích hợp toàn bộ luồng pipeline
├── test_orchestrator.py              # Kiểm thử hoạt động của CLI orchestrator
├── test_providers.py                 # Kiểm thử độc lập các API provider
├── test_scanner_logger.py            # Kiểm thử trình project scanner và logger
└── test_stress.py                    # Kiểm thử khả năng chịu tải và tần suất gọi API
```
