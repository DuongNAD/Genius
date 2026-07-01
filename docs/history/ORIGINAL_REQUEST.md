# Original User Request

## Initial Request — 2026-06-28T10:30:22+07:00

# Teamwork Project Prompt — Draft

> Status: Launched
> Goal: Craft prompt → get user approval → delegate to teamwork_preview

Xây dựng một công cụ dòng lệnh (CLI) bằng Python độc lập để kết nối và tương tác với hệ thống Genius API từ một máy tính khác trong mạng LAN.

Working directory: e:\Project\Genius\client_app
Integrity mode: benchmark

## Requirements

### R1. Giao diện dòng lệnh (CLI)
Tạo một script Python độc lập (`genius_client.py`). Khi khởi động, script sẽ yêu cầu người dùng nhập 4 thông tin: Địa chỉ IP của máy chủ, Cổng (Port) của tác nhân muốn gọi, API Key, và Câu lệnh (Prompt).

### R2. Xử lý bảo mật Checksum
Script phải tự động tính toán mã băm SHA-256 của payload JSON gửi đi và đính kèm vào header `X-Payload-SHA256` theo đúng chuẩn giao tiếp của Genius API để tránh lỗi 400 Checksum mismatch.

### R3. Quy trình gọi API bất đồng bộ
Script phải gọi POST tới endpoint `/run`, lấy `task_id`, sau đó dùng vòng lặp liên tục gọi GET tới `/status/{task_id}` (polling) cho đến khi trạng thái trả về là `completed`, cuối cùng in kết quả ra màn hình.

## Acceptance Criteria

### Verification
- [ ] Script `genius_client.py` có thể chạy độc lập, không import bất kỳ file nội bộ nào từ thư mục gốc của Genius (chỉ sử dụng thư viện chuẩn hoặc thư viện bên thứ 3 phổ biến như `requests`).
- [ ] Gửi thành công request mà không bị API server từ chối vì lỗi bảo mật header (`X-Payload-SHA256`).
- [ ] Hiển thị thông báo thân thiện trong lúc chờ (polling) và in ra kết quả cuối cùng rõ ràng.

---
*Next: when approved → delegate via invoke_subagent (see Delegation Protocol)*

## Follow-up — 2026-06-28T03:56:42Z

# Teamwork Project Prompt — Draft

> Status: Launched
> Goal: Craft prompt → get user approval → delegate to teamwork_preview

Xây dựng một hệ thống mạng lưới Agent phân tán (Distributed Agent Network) cho dự án Genius. Hệ thống cho phép Server trung tâm (Orchestrator) nhận prompt từ người dùng và phân phát (dispatch) các lệnh này đến các máy Client (Worker Node) phù hợp để xử lý.

Working directory: e:\Project\Genius
Integrity mode: benchmark

## Requirements

### R1. Máy chủ điều phối (Central Hub)
Cập nhật hoặc xây dựng module trên Server để nó đóng vai trò là trung tâm phân phối. Khi người dùng nhập prompt, Server sẽ không tự chạy mà tìm và giao (dispatch) task cho một máy Client đang rảnh rỗi.

### R2. Máy trạm thực thi (Client Worker)
Nâng cấp hoặc viết một Client Worker app. Khi chạy, Worker này sẽ kết nối với Server, đăng ký Role mà nó đảm nhận (ví dụ: `grok`, `claude`, `codex`), và luôn trong trạng thái chờ nhận lệnh để thực thi cục bộ rồi trả kết quả về.

### R3. Giao thức mạng lưới linh hoạt
Nhóm tác nhân tự do quyết định công nghệ giao tiếp giữa Server và Client (có thể dùng WebSocket, Message Queue, RPC, hoặc HTTP Polling...) miễn là đảm bảo tính hai chiều, độ trễ thấp và độ tin cậy cao.

## Acceptance Criteria

### Verification
- [ ] Server trung tâm có khả năng quản lý danh sách các Worker đang kết nối và trạng thái (rảnh/bận) của chúng.
- [ ] Khi chạy thử một lệnh ở Server, nó phải được đẩy thành công xuống một máy Client Worker tương ứng để xử lý và trả về.
- [ ] Có bài test tự động (test script) chứng minh hệ thống phân tán hoạt động hoàn hảo (ví dụ mô phỏng 1 Hub và 2 Worker).

---
*Next: when approved → delegate via invoke_subagent (see Delegation Protocol)*

## Follow-up — 2026-06-28T07:40:25Z

# Teamwork Project Prompt — Draft

> Status: Launched
> Goal: Craft prompt → get user approval → delegate to teamwork_preview

Xây dựng và tích hợp các nguyên tắc "AI-Native Engineering Rules" (từ tài liệu Workshop 3) vào mã nguồn của dự án Genius, giúp các Agent thông minh hơn, code chính xác hơn và tự động có feedback loop.

Working directory: e:\Project\Genius
Integrity mode: benchmark

## Requirements

### R1. Tích hợp Hệ thống Quy tắc Chung
Tạo module `ag_core/utils/prompt_templates.py` chứa biến hằng `AGENT_CORE_RULES`. Nội dung luật bao gồm: xử lý từng task một, chạy test/linting/build, tạo bằng chứng chạy code, không đọc file .env, và không over-engineer.

### R2. Nâng cấp Claude Architect (Tách Plan & Implement)
Sửa đổi `ag_core/agents/claude_architect.py` để tiêm quy tắc "Tách plan và implement" vào system prompt, ép Agent này chỉ lên kiến trúc (plan), tuyệt đối không tự ý viết code implement.

### R3. Nâng cấp Orchestrator (Quản lý Context)
Sửa đổi `orchestrator.py` để chia nhỏ task (1 task/prompt) và yêu cầu Orchestrator tạo/cập nhật liên tục file `CURRENT_PROG.md` trong thư mục `.agents/` nhằm theo dõi tiến độ.

### R4. Nâng cấp Tester & Reviewer (Feedback Loop & Linter)
Sửa đổi `ag_core/agents/tester.py` và `ag_core/agents/codex_reviewer.py` để bắt buộc agent phải chạy test thực tế (show evidence), tự động chạy linter (flake8) trước khi review, và tự sửa lỗi nếu test fail.

## Acceptance Criteria

### Verification
- [ ] Tồn tại file `ag_core/utils/prompt_templates.py` và các file Agent đã gọi đến `AGENT_CORE_RULES` thành công.
- [ ] Chạy thành công toàn bộ test suite (`test_orchestrator.py`, `test_integration.py`...) mà không phá vỡ logic sẵn có.
- [ ] Có thể gọi thử một luồng cơ bản và xác nhận `CURRENT_PROG.md` được tạo ra trong `.agents/`.

---
*Next: when approved → delegate via invoke_subagent (see Delegation Protocol)*

## Follow-up — 2026-06-28T11:10:31Z

# Teamwork Project Prompt

Nâng cấp lõi của Orchestrator trong dự án Genius để nó có thể tự động điều phối một quy trình End-to-End (Từ đầu đến cuối) không cần sự can thiệp của con người. Nhận một prompt duy nhất từ người dùng và tự động đẩy qua dây chuyền: Lên Kế hoạch (Claude) -> Tranh biện/Bảo mật (Grok) -> Lập trình & Kiểm thử (Codex) -> Nghiệm thu (Orchestrator).

Working directory: e:\Project\Genius
Integrity mode: benchmark

## Requirements

### R1. Xây dựng E2E Pipeline trong Orchestrator
Viết lại luồng thực thi trong `orchestrator.py` (hoặc tạo một class/phương thức mới như `E2EPipeline`). Khi nhận một request lớn (ví dụ: "Tạo trang web giới thiệu biển"), Orchestrator phải tự động chia nhỏ và chạy một vòng lặp tuần tự qua các Agent mà không dừng lại hỏi ý kiến người dùng giữa chừng.

### R2. Áp dụng chuẩn Sơ đồ Vai trò (Role Mapping)
Code Orchestrator phải gọi đích danh các Agent theo đúng sơ đồ:
1. Giao cho `ClaudeArchitectAgent` lên `plan.md`.
2. Giao cho `GrokResearcherAgent` (hoặc Critic) phản biện bản plan đó và ép Claude sửa.
3. Giao cho `CodexReviewer` (hoặc Codex Coder/Tester) gõ code dựa trên plan, tự động chạy Unit Test và Linter. Nếu lỗi tự vòng lại sửa code.
4. Orchestrator tổng hợp toàn bộ kết quả, xác nhận test xanh và báo cáo "Hoàn thành" cho người dùng.

## Acceptance Criteria

### Verification
- [ ] Tồn tại hàm điều phối E2E thực sự gọi liên kết các Agent với nhau theo đúng thứ tự (Claude -> Grok -> Claude -> Codex -> Tester).
- [ ] Viết một bài test (ví dụ: `test_e2e_full_pipeline.py`) chạy mock qua toàn bộ quy trình này từ khi nhập prompt đến khi xuất kết quả cuối cùng.
- [ ] Pipeline có khả năng tự xử lý lỗi (ví dụ Codex báo lỗi test thì Orchestrator biết đường bắt Codex sửa lại) thay vì sập ngang.

## Follow-up — 2026-06-28T11:53:29Z

# Teamwork Project Prompt

[Dự án: Phân tích toàn diện dự án Genius (Multi-agent Orchestrator) và nghiên cứu, thực thi nâng cấp mọi khía cạnh của quy trình làm việc hiện tại.]

Working directory: e:\Project\Genius
Integrity mode: development

## Requirements

### R1. Phân tích và Đề xuất
Thực hiện đánh giá toàn diện mã nguồn dự án Genius. Xác định các điểm nghẽn và cơ hội nâng cấp trong luồng phối hợp giữa các tác tử (Grok, Claude, Codex, Tester), quy trình kiểm thử/giám sát, và luồng CI/CD/DevOps.

### R2. Thực thi Nâng cấp
Triển khai các cải tiến quy trình đã đề xuất vào thực tế dự án, đảm bảo mã nguồn và các tệp cấu hình mới hoạt động trơn tru. Nhóm có quyền tự do quyết định khía cạnh nào mang lại giá trị cao nhất để ưu tiên làm trước.

## Verification Resources
Dự án đã có sẵn bộ test phong phú (`test_e2e.py`, `test_orchestrator.py`, v.v.) và cấu hình `pytest`. Sử dụng chúng để xác minh mã nguồn sau khi nâng cấp.

## Acceptance Criteria

### Đảm bảo chất lượng hệ thống
- [ ] Toàn bộ bộ test hiện tại (chạy qua `pytest`) phải chạy thành công 100% để đảm bảo các nâng cấp không làm hỏng tính năng sẵn có của hệ thống.
- [ ] Nếu có kịch bản/quy trình mới được thêm vào, phải bổ sung test case (ví dụ: integration test) tương ứng.

### Báo cáo và Bàn giao
- [ ] Tạo một tệp `upgrade_report.md` tóm tắt toàn bộ những điểm nghẽn đã tìm thấy, nguyên nhân, và danh sách các thay đổi quy trình đã được áp dụng.

## Follow-up — 2026-06-28T19:31:35+07:00

# Teamwork Project Prompt

[Dự án: Viết một bản báo cáo kỹ thuật toàn diện và chi tiết nhất về dự án Genius, đồng thời lưu tại một đường dẫn chính xác theo yêu cầu.]

Working directory: e:\Project\Genius
Integrity mode: development

## Requirements

### R1. Nội dung Báo cáo Toàn diện
Tiến hành rà soát toàn bộ dự án Genius. Bản báo cáo phải bao gồm đầy đủ 4 khía cạnh: (1) Kiến trúc hệ thống và luồng phối hợp giữa các Agent (Grok, Claude, Codex, Tester), (2) Hướng dẫn triển khai & sử dụng, (3) Chi tiết về các nâng cấp SPO vừa thực hiện, và (4) Cấu trúc mã nguồn lõi & cơ sở dữ liệu.

### R2. Định dạng và Lưu trữ
Tất cả nội dung phải được biên soạn thành một tài liệu Markdown duy nhất, mạch lạc và chuyên nghiệp. **Bắt buộc** lưu tệp báo cáo này tại đường dẫn chính xác sau: `e:\Project\Genius\Genius_Comprehensive_Report.md`. Mọi tệp khác đều không được chấp nhận.

### R3. Ràng buộc Hệ thống (Tránh tràn RAM)
Tuyệt đối không được mở quá nhiều quá trình đọc file hay chạy các tác tử con song song cùng lúc. Mọi quá trình phân tích code phải chạy tuần tự (sequential) để đảm bảo không làm sập (OOM) máy của người dùng.

## Acceptance Criteria

### Báo cáo và Nội dung
- [ ] Tệp báo cáo phải được lưu MỘT CÁCH CHÍNH XÁC tại `e:\Project\Genius\Genius_Comprehensive_Report.md`.
- [ ] Báo cáo phải có đầy đủ 4 mục lớn (Kiến trúc, Hướng dẫn, Nâng cấp SPO, Core & DB) được định dạng bằng thẻ Heading (H1, H2) rõ ràng.
- [ ] Không có bất kỳ lỗi chính tả hay định dạng lộn xộn nào trong file Markdown.

## Follow-up — 2026-06-28T19:45:00+07:00

[Dự án: Tái cấu trúc dự án Genius — Loại bỏ việc kết nối trực tiếp qua API keys của các model, thay vào đó chuyển sang gọi thông qua các công cụ dòng lệnh (CLI) nội bộ (claude cli, grok cli, codex cli) đã được đăng nhập sẵn tài khoản.]

Working directory: e:\Project\Genius
Integrity mode: development

## Requirements

### R1. Thay đổi cơ chế Provider
Loại bỏ việc sử dụng các biến môi trường API_KEY. Viết lại các module cung cấp LLM (`providers/`) để giao tiếp với mô hình thông qua việc thực thi các lệnh CLI cục bộ. Nhóm Agent được tự do lựa chọn phương thức giao tiếp (subprocess, file tạm) sao cho hiệu quả nhất.

### R2. Phân tích CLI cục bộ
Trước khi viết code thay thế, nhóm Agent bắt buộc phải chạy thử và phân tích cấu trúc lệnh của các công cụ CLI hiện có trên máy (claude, grok, codex) để nắm rõ cú pháp và luồng I/O (interactive mode hay tham số command-line).

### R3. Ràng buộc Hệ thống (Tránh tràn RAM)
Tuyệt đối không được mở quá nhiều tiến trình gọi CLI hay chạy các bài test song song cùng lúc. Mọi quá trình phân tích và test code phải chạy tuần tự (sequential) để tránh lỗi OOM (Out-of-Memory) làm sập hệ thống.

## Acceptance Criteria

### Tích hợp CLI
- [ ] Các tệp trong thư mục `providers/` (hoặc tương đương) không còn đòi hỏi các biến môi trường như `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GROK_API_KEY`.
- [ ] Hệ thống có thể gọi thành công các lệnh CLI (claude, grok, codex) để lấy kết quả trả về đúng định dạng mong đợi của hệ thống Genius.
- [ ] Toàn bộ bộ test hiện tại (`pytest`) chạy thành công (100% pass) sau khi thay đổi cơ chế gọi mô hình.

## Follow-up — 2026-06-28T20:55:09+07:00

[Dự án: Phân tích kỹ thuật chuyên sâu ứng dụng Codex Desktop hiện có trên máy để tìm ra các cổng giao tiếp ẩn (API, WebSockets), file CLI đi kèm, hoặc cơ chế automation tốt nhất giúp hệ thống Genius có thể gửi/nhận prompt lập trình với ứng dụng này.]

Working directory: e:\Project\Genius
Integrity mode: development

## Requirements

### R1. Dò tìm và Phân tích Cấu trúc Ứng dụng
Quét thư mục cài đặt của Codex Desktop (thường nằm ở `C:\Program Files` hoặc `%LocalAppData%\Programs`). Phân tích các file thực thi, file cấu hình (JSON/YAML) và các file log để tìm kiếm các cổng mạng (ports) ẩn hoặc tệp CLI đính kèm.

### R2. Trích xuất Cấu trúc Giao tiếp (Communication Hooks)
Thực thi ứng dụng bằng dòng lệnh (ví dụ với cờ `--help`), giám sát qua `netstat` để xem ứng dụng có mở cổng localhost nào không, và/hoặc phân tích các tệp cấu hình (asar, json). Tìm kiếm bất kỳ giao diện nào (API, IPC, CLI phụ) có thể dùng để gửi lệnh lập trình tự động.

### R3. Ràng buộc Hệ thống
Mọi quá trình quét (scan) hay chạy file thực thi phải được giới hạn chặt chẽ, tuyệt đối không được ghi đè hay xóa bất kỳ tệp hệ thống nào ngoài thư mục làm việc của dự án. Không mở đa luồng quá nhiều để tránh OOM.

## Acceptance Criteria

### Báo cáo Phân tích Khả thi (Feasibility Report)
- [ ] Xác định thành công vị trí cài đặt của Codex Desktop.
- [ ] Ghi nhận được có hay không các cổng mạng ẩn, tệp thực thi CLI đính kèm, hoặc cơ chế IPC/WebSockets.
- [ ] Đề xuất được ít nhất một phương án kỹ thuật khả thi để hệ thống Genius gửi prompt và trích xuất kết quả từ Codex Desktop. Đề xuất này phải được lưu vào file `codex_integration_analysis.md`.

## Follow-up — 2026-06-28T14:14:22Z

[Dự án: Cập nhật file `openai_provider.py` để tích hợp gọi trực tiếp Codex CLI thay vì gọi nhầm Claude CLI, dựa trên báo cáo phân tích kỹ thuật trước đó.]

Working directory: e:\Project\Genius
Integrity mode: development

## Requirements

### R1. Định vị tệp thực thi Codex
Thêm logic để tìm đường dẫn của `codex.exe` ưu tiên trong `%LocalAppData%\OpenAI\Codex\bin\*\codex.exe` hoặc thư mục `WindowsApps`.

### R2. Thực thi và bóc tách dữ liệu JSONL
Sử dụng `asyncio.create_subprocess_exec` để gọi `codex.exe exec <prompt> --dangerously-bypass-approvals-and-sandbox --json` với `stdin` được DEVNULL (chặn để không bị treo). Bóc tách luồng `stdout` theo chuẩn JSONL để lấy `item.text` từ event `agent_message` và thống kê tokens từ event `turn.completed`.

## Acceptance Criteria

### Xác minh qua Pytest
- [ ] Code trong `openai_provider.py` hoàn toàn sạch bóng các references tới `claude` CLI.
- [ ] Logic parse JSONL xử lý mượt mà, không bị crash nếu CLI trả về JSON lỗi hoặc không đúng cấu trúc mong đợi.
- [ ] Vượt qua được bài đánh giá kiểm thử độc lập (hoặc chạy test pass).

## Follow-up — 2026-06-28T14:54:47Z

Fix and upgrade the Genius Antigravity 2.0 system based on the GENIUS_SYSTEM_REVIEW_REPORT.md. The goal is to address critical bugs, security vulnerabilities, and architectural gaps to ensure the system is production-ready.

Working directory: e:\Project\Genius
Integrity mode: benchmark

## Requirements

### R1. Resolve Critical Issues
Implement the missing Skill Layer (`api.py` and `run.py` for all 6 agents), fix orphaned worker tasks in `execute_task` on WebSocket disconnect, standardize JSON checksum serialization across components, and resolve the `serve.py` CLI infinite hang issue.

### R2. Resolve High & Medium Issues
Upgrade payload checksums to HMAC-SHA256, add `jti` to JWT for replay protection, implement bounded caches for `pending_tasks` and `central_hub.tasks`, implement a SQLite write queue, and set up dynamic port discovery for local microservices.

### R3. Resolve Low Issues
Add jitter to exponential backoff reconnects, add proper type annotations, and implement structured logging.

## Acceptance Criteria

### Automated Testing
- [ ] All existing test cases in `pytest` must pass without any regressions.
- [ ] The missing skill layer tests (e.g., E2E Phase 5) no longer crash due to `FileNotFoundError`.

### System Stability
- [ ] Running `serve.py` locally and interacting with the orchestrator no longer hangs the process indefinitely upon exit.
- [ ] WebSocket disconnects from workers successfully clean up all associated `running_tasks`.
- [ ] Checksum verification succeeds consistently using the new HMAC-SHA256 standard and identical JSON serialization logic on both Hub and Worker.

## 2026-06-28T15:30:56Z

Refactor the inter-agent communication architecture in Genius Antigravity 2.0 based on the connection evaluation report. Replace markdown/regex contracts with structured Pydantic schemas, unify the communication paths, and implement safe parallel execution in the pipeline.

Working directory: e:\Project\Genius
Integrity mode: benchmark

## Requirements

### R1. Implement Pydantic Contract
Replace the fragile markdown regex parsing (`parse_design_for_files()`) with a shared Pydantic schema contract (e.g., `DesignPlan { files: [...] }`) across all agents.

### R2. Implement Message Bus
Replace the file-based handoff mechanism (`design.md`, `audit.md`, etc. as the primary data transfer) with a lightweight message bus (passing `context` dict and `artifact_id` via memory or SQLite). Files should be retained solely as debug artifacts.

### R3. Unify Network Communication
Establish a single primary connection path: HTTP for local services and WebSocket (WS) strictly for distributed nodes. Remove any duplicated logic for checksums and authentication across these modes to create a unified implementation in `ag_core`.

### R4. Parallelize Pipeline
Optimize the pipeline bottleneck by running independent agent tasks (e.g., Tester and Security) concurrently using `asyncio.gather()` after code generation, while keeping DevOps as the final sequential step.

## Acceptance Criteria

### Automated Testing & Stability
- [ ] All existing integration and E2E test cases in `pytest` must pass without regressions.
- [ ] The orchestrator successfully parses agent outputs using Pydantic without relying on regex markdown extraction.
- [ ] Local microservices run exclusively over HTTP, while WebSocket logic is correctly bypassed unless `--distributed` is specified.

### Pipeline Efficiency
- [ ] The execution logs demonstrate that Tester and Security audits are running concurrently, yielding a measurable reduction in pipeline latency compared to a purely sequential run.

## 2026-06-28T15:35:27Z

# Teamwork Project Prompt — Draft

> Status: Launched.
> Goal: Get user approval → delegate to teamwork_preview

Nâng cấp và tối ưu hóa dự án `ai-devkit` (https://github.com/codeaholicguy/ai-devkit.git) thành một hệ thống đa tác vụ (multi-agent) tập trung vào các coding agent có khả năng giao tiếp, với giao diện dòng lệnh (TUI) và quy trình làm việc chuẩn mực.

Working directory: ~/teamwork_projects/ai_devkit_v2
Integrity mode: benchmark

## Requirements

### R1. Quản lý cấu hình và Workflow
- Sử dụng một file cấu hình duy nhất dùng chung cho mọi AI agent (định dạng do nhóm agent tự quyết định).
- Hỗ trợ workflow chuẩn của senior engineer: Requirements → Design → Planning → Implementation → Testing → Review.

### R2. Hệ thống Agent và Giao tiếp
- Cho phép các agent trao đổi thông tin với nhau.
- Quản lý nhiều agent đồng thời trong cùng một dashboard TUI. Bắt buộc sử dụng thư viện **Ink** (React-based CLI) để xây dựng UI.

### R3. Bộ nhớ và Kiến trúc mở rộng
- Lưu memory cục bộ bằng SQLite để tránh mất ngữ cảnh.
- Hệ thống Skills có thể mở rộng và tái sử dụng.
- Kiến trúc Local-first, ưu tiên quyền riêng tư và dữ liệu nội bộ.

### R4. Triển khai
- Hỗ trợ cài đặt cực nhanh chỉ với một lệnh `npx ai-devkit init`.

## Acceptance Criteria

### Khởi tạo và Cấu hình
- [ ] Chạy lệnh khởi tạo (ví dụ script `init`) thành công, sinh ra file cấu hình mặc định và tạo database SQLite.
- [ ] Tồn tại file mô tả schema hoặc cách lưu trữ memory của agent trong SQLite.

### Giao diện TUI Dashboard
- [ ] Chạy lệnh start/dev hiển thị thành công giao diện TUI xây dựng bằng thư viện Ink.
- [ ] Dashboard hiển thị được danh sách các agent đang hoạt động và trạng thái của chúng.

### Khả năng Giao tiếp
- [ ] Có mã nguồn minh chứng (test case hoặc logic demo) cho việc hai agent có thể gửi/nhận thông điệp qua lại.
- [ ] Hệ thống hoạt động hoàn toàn ở môi trường local (ngoại trừ các API call tới LLM nếu cần).

## 2026-06-28T16:24:53Z

# Teamwork Project Prompt — Draft

> Status: Launched
> Goal: Craft prompt → get user approval → delegate to teamwork_preview

Nâng cấp kiến trúc và sửa các lỗi nghiêm trọng của dự án AI Devkit (Antigravity 2.0). Đặc biệt, cấu hình lại các CLI provider (Grok, Codex) cho chuẩn xác và cải thiện luồng thực thi (parallel execution, streaming, vector memory).

Working directory: e:\Project\Genius
Integrity mode: development

## Requirements

### R1. Sửa lỗi GrokProvider (Bắt buộc dùng CLI)
- Cập nhật `ag_core/providers/grok_provider.py` để sử dụng đúng `grok` CLI (hiện tại code đang tìm nhầm `claude`).
- Cú pháp thực thi: `grok -p "prompt" --output-format json --no-auto-update`.
- Cập nhật Model name thành: `grok-build-0.1`.
- Thêm cờ `--session-id` để lưu session persistence.
- Đảm bảo hỗ trợ xác thực thông qua biến môi trường hoặc bằng lệnh `grok login` như người dùng yêu cầu.

### R2. Sửa lỗi parse JSONL của Codex (OpenAIProvider)
- Parse JSONL event stream nhiều dòng thay vì 1 object JSON duy nhất.
- Lọc event bắt buộc: `item.type == "agent_message"`.
- Thêm cờ `--dangerously-bypass-approvals-and-sandbox` và xử lý Windows-specific stdin redirection (`NUL`).

### R3. Cập nhật Cấu hình & Fix Warnings
- Cập nhật `config.yaml` với tên model chuẩn (vd: `claude-sonnet-4-6`, `grok-build-0.1`).
- Sửa lỗi TokenBucket rate limiter (chuyển sang dùng `asyncio.get_running_loop()`).
- Bỏ hardcode đường dẫn Python (`C:/Users/Admin/...`) trong các script wrapper.
- Thêm `.gitignore` chuẩn chặn các file nhạy cảm (`.env`, `config.yaml`, keys) và thiết lập pre-commit hooks.

### R4. Nâng cấp Bộ nhớ và Kiến trúc
- Nâng cấp `VectorMemory`: Thay `SimpleTFIDFEmbedding` bằng `sentence-transformers` (vd: `all-MiniLM-L6-v2`) or mô hình nhúng tương tự.
- Tối ưu `orchestrator`: Cho phép chạy song song (parallel) các bước độc lập (như Security Audit & DevOps Check) bằng `asyncio.gather`.
- Thêm context history cho các agent để hỗ trợ multi-turn conversation.

### R5. Tính năng mở rộng (Nếu còn thời gian)
- Hỗ trợ streaming response.
- Chuyển đổi Dashboard sang WebSocket.
- Implement MCP Server.
- Dockerize toàn bộ hệ thống.

## Acceptance Criteria

### API Providers
- [ ] `GrokProvider` thực thi thành công lệnh `grok` thực sự và trả về đúng dữ liệu, không bị trỏ nhầm sang `claude`.
- [ ] `OpenAIProvider` parse chính xác JSONL output của codex cli, không gặp lỗi `JSONDecodeError`.

### Code Quality & Architecture
- [ ] Orchestrator chạy nhanh hơn nhờ cơ chế parallel.
- [ ] File `config.yaml` có các model name chính xác.
- [ ] Không còn bất kỳ đường dẫn hardcode Python cục bộ nào trên hệ thống.
- [ ] Có `.gitignore` đầy đủ để tránh lộ key.
