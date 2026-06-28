# Original User Request

## 2026-06-27T13:35:34Z

# Teamwork Project Prompt

Xây dựng bản nâng cấp toàn diện (Giai đoạn 3 - Enterprise Ultimate) cho hệ thống mạng lưới Microservices Antigravity 2.0. Mục tiêu là biến kiến trúc hiện tại thành một hệ thống giám sát thời gian thực, có khả năng chịu tải, bảo mật mạnh mẽ và tự động lưu trữ lịch sử.

Working directory: e:\Project\Genius
Integrity mode: benchmark

## Requirements

### R1. Tối ưu Hiệu năng & Chịu tải (Performance)
Áp dụng cơ chế Caching cho các truy vấn trùng lặp và tối ưu hóa thư viện `httpx` kết nối giữa các máy. Nâng cấp Rate Limiter để chống quá tải khi có hàng nghìn request gửi đến Orchestrator cùng lúc.

### R2. Tăng cường Bảo mật (JWT Authentication)
Thay thế cơ chế xác thực API Key tĩnh hiện tại bằng JSON Web Token (JWT). Các máy trạm (Agent) chỉ cấp quyền xử lý khi Orchestrator gửi đúng token hợp lệ (có thời hạn sử dụng).

### R3. Tích hợp Cơ sở dữ liệu (Database)
Thiết lập một Cơ sở dữ liệu (tùy chọn công nghệ do AI quyết định) để tự động lưu trữ:
- Lịch sử hội thoại và kết quả (prompts, outputs).
- Logs hoạt động của toàn bộ mạng lưới các máy AI.

### R4. Bảng điều khiển Quản trị (Web Dashboard)
Xây dựng một giao diện Web Dashboard trực quan để giám sát trạng thái của toàn bộ hệ thống. Dashboard cần hiển thị: máy nào đang chạy, máy nào đang rảnh, và xem lại lịch sử hội thoại từ CSDL. (Công nghệ frontend do AI tự quyết định nhưng phải hiện đại và tốc độ cao).

### R5. Mở rộng Mạng lưới AI (Tác nhân Tester)
Bổ sung thêm 1 Agent mới vào mạng lưới: **Tester Agent**. Tác nhân này sẽ nhận kết quả sau khi `Codex Reviewer` làm việc xong, và có nhiệm vụ lên kịch bản test hoặc sinh mã unit test tự động. Tích hợp tác nhân này vào menu của `serve.py`.

## Acceptance Criteria

### Security & Performance
- [ ] Gửi request không có JWT hoặc sai JWT tới Agent API phải trả về lỗi `401 Unauthorized`.
- [ ] Vượt qua kịch bản kiểm thử chịu tải (Stress Test) mô phỏng gửi nhiều request.

### Database & Dashboard
- [ ] Có script kiểm tra tự động xác nhận CSDL đã được tạo và có dữ liệu lịch sử hội thoại sau một lần chạy thực tế.
- [ ] Web Dashboard có thể truy cập được qua trình duyệt (HTTP 200 OK) và hiển thị dữ liệu lấy từ Database.

### Pipeline Integration
- [ ] Tệp `serve.py` hiển thị thêm lựa chọn `tester` (Tester Agent).
- [ ] Luồng Orchestrator mới chạy qua 4 trạm: Grok -> Claude -> Codex -> Tester thành công.

## 2026-06-27T14:35:10Z

# Teamwork Project Prompt

Xây dựng hệ thống Lệnh Chuyên biệt (Specialized Slash Commands) cho từng AI Agent trong mạng lưới Antigravity 2.0. Mục tiêu là khai thác tối đa sức mạnh chuyên sâu của từng mô hình (Grok đào sâu, Claude thiết kế, Codex viết code, Tester kiểm thử) qua các câu lệnh định tuyến thông minh.

Working directory: e:\Project\Genius
Integrity mode: benchmark

## Requirements

### R1. Bộ lệnh chuyên biệt cho từng Agent (Specialized Commands)
Nâng cấp logic xử lý trong các tệp API của từng Agent để hỗ trợ (nhưng không giới hạn) các lệnh sau:
- **Grok**: `/research`, `/summarize`, `/fact-check`
- **Claude**: `/plan`, `/design`, `/review-architecture`
- **Codex**: `/code`, `/refactor`, `/security`
- **Tester**: `/unit-test`, `/stress-test`
*Lưu ý: Nhóm AI được phép sáng tạo thêm các lệnh mới cực ngầu và hữu ích.*

### R2. Bộ định tuyến Thông minh (Smart Orchestrator Routing)
Nâng cấp `orchestrator.py` để nhận diện các lệnh Slash. Ví dụ, nếu người dùng gõ `orchestrator /plan "Xây web"`, Orchestrator phải hiểu lệnh `/plan` là thuộc sở trường của Claude và tự động đẩy thẳng/định tuyến việc này cho Claude xử lý.

### R3. Hỗ trợ gọi trực tiếp qua CLI (Direct CLI Execution)
Cập nhật các CLI wrapper hoặc tệp `serve.py` để hỗ trợ cờ/lệnh trực tiếp. Người dùng có thể gọi một lệnh riêng lẻ mà không cần chạy cả quy trình 4 bước (Ví dụ: gõ `codex /refactor "file.py"` trên terminal).

## Acceptance Criteria

### Lệnh độc lập
- [ ] Gửi request HTTP POST tới API của Claude với payload chứa `/plan "Yêu cầu"` sẽ kích hoạt logic lập kế hoạch chuyên sâu, không bị nhầm lẫn với các lệnh khác.
- [ ] Lệnh gọi trực tiếp từ Terminal (VD: qua `serve.py` hoặc CLI scripts) thực thi đúng lệnh chuyên biệt và trả về kết quả.

### Định tuyến thông minh
- [ ] Gửi prompt `/security "kiểm tra hệ thống"` tới Orchestrator sẽ tự động kích hoạt Codex Agent để làm nhiệm vụ thay vì phải chạy lại từ đầu bằng Grok.


## 2026-06-27T15:24:27Z

# Teamwork Project Prompt

Nâng cấp Antigravity 2.0 (Giai đoạn 5 - DevOps & Memory Expansion): Mở rộng hệ sinh thái với Bộ nhớ dài hạn (Vector DB), bổ sung các Đặc vụ bảo mật & vận hành, và thiết lập dây chuyền CI/CD tự động hóa hoàn toàn.

Working directory: e:\Project\Genius
Integrity mode: benchmark

## Requirements

### R1. Hệ thống Bộ nhớ Dài hạn (Long-term Vector Memory)
Tích hợp một Vector Database cục bộ (như FAISS hoặc ChromaDB) vào kiến trúc. Mọi đoạn code và quyết định thiết kế quan trọng từ các Agents phải được nhúng (embed) và lưu trữ. Khi giải quyết bài toán mới, các Agents (Claude, Codex) tự động truy vấn Vector DB để tái sử dụng "ký ức" từ các tác vụ cũ.

### R2. Đội Đặc Vụ Chuyên Trách Mới (DevOps & Security Agents)
Bổ sung thêm 2 Agents mới vào mạng lưới (`serve.py` và Orchestrator):
- **Security Agent**: Đứng sau Codex để rà soát mã độc, kiểm tra các lỗ hổng bảo mật (OWASP, Injection, Hardcoded secrets).
- **DevOps Agent**: Đứng cuối chuỗi cung ứng, chuyên đóng gói sản phẩm bằng cách viết các tệp Dockerfile, docker-compose.yml và scripts triển khai.

### R3. Tự động hóa CI/CD Pipeline
Khởi tạo cấu trúc luồng CI/CD (như `.github/workflows/ci.yml`). Workflow này phải tự động cài đặt môi trường, cài đặt các dependencies và tự động chạy bộ test suite bằng `pytest` mỗi khi có thay đổi mã nguồn.

## Acceptance Criteria

### Bộ nhớ Dài hạn
- [ ] Các Agent có thể ghi (write) và tìm kiếm (search) thông tin từ Vector DB cục bộ mà không sinh ra lỗi kết nối. Dữ liệu ngữ cảnh phải được nhúng vào prompt của Agent.

### Đặc vụ AI & Pipeline
- [ ] Gọi lệnh hoặc gửi request tới `Security Agent` với một đoạn code có chứa hardcoded API Key, Agent phải trả về báo cáo lỗi bảo mật.
- [ ] Orchestrator có khả năng định tuyến thành công các lệnh như `/security` và `/deploy` tới 2 Agents mới.

### CI/CD
- [ ] Tệp YAML của GitHub Actions được tạo ra hoàn chỉnh, tuân thủ đúng cú pháp và bao gồm bước chạy lệnh kiểm thử (test execution).

## 2026-06-27T17:01:45Z

# Teamwork Project Prompt

Nâng cấp Antigravity 2.0 (Giai đoạn 6 - Auto-Pilot): Trở thành Siêu Công xưởng AI (AI Factory). Cho phép người dùng nhập 1 câu lệnh duy nhất để tạo ra các dự án khổng lồ (như Hệ điều hành AI, Web phức tạp). Hệ thống sẽ tự động thiết kế kiến trúc, chia nhỏ thành từng file, viết code, kiểm thử, vá lỗi đệ quy, và đóng gói tự động toàn phần.

Working directory: e:\Project\Genius
Integrity mode: benchmark

## Requirements

### R1. Thuật toán "Chia để trị" (Project Chunking)
Nâng cấp `orchestrator.py` để xử lý dự án lớn. Khi Claude Architect xuất ra bản thiết kế hệ thống nhiều file, Orchestrator sẽ bóc tách nó thành một **Hàng đợi Nhiệm vụ (Task Queue)**. Từng file sẽ được giao lần lượt cho Codex viết mã để đảm bảo độ chi tiết tối đa.

### R2. Vòng lặp Tự vá lỗi (Self-Healing Loop)
Thay vì code xong là kết thúc, thiết lập vòng lặp đệ quy cho từng file: 
`[Codex viết code] -> [Tester/Security kiểm tra] -> [Nếu LỖI -> Gửi log lỗi về lại cho Codex để tự sửa]`. Vòng lặp chỉ kết thúc khi file đạt trạng thái hoàn hảo 100% hoặc đạt giới hạn số vòng lặp (Max Retries).

### R3. Chế độ Điều hành kép (Auto-Pilot vs Interactive)
Bổ sung 2 tham số mới vào CLI (`serve.py`):
- `--interactive`: Dừng lại sau bước thiết kế của Claude để in ra màn hình cho con người duyệt hoặc yêu cầu chỉnh sửa.
- `--auto-pilot`: Cắm máy chạy qua đêm. Hệ thống tự quyết định và tự sửa mọi thứ cho đến khi ra thành phẩm.

### R4. Quản lý Không gian làm việc (Workspace Management)
Khi khởi tạo dự án mới, Orchestrator tự động tạo một thư mục riêng biệt (Ví dụ: `e:\Project\Genius\projects\[ten_du_an]`). Mọi file code, test, docker, log của dự án đó phải được ghi vào đây để không làm rác mã nguồn lõi của Antigravity.

## Acceptance Criteria

### Tính toàn vẹn Vòng lặp
- [ ] Xây dựng test case mô phỏng Tester báo lỗi 1 lần, hệ thống phải tự động retry gọi lại Codex thay vì dừng lại chờ người dùng (Self-healing).

### Quản lý Workspace
- [ ] Chạy lệnh `python serve.py --roles orchestrator --auto-pilot --prompt "Xây dựng AI Agent OS"` phải sinh ra thư mục con `projects/ai_agent_os/` chứa toàn bộ kết quả của 6 Agent.

### Interactive Mode
- [ ] Khi chạy ở chế độ `--interactive`, ứng dụng phải có dòng chờ `input()` yêu cầu con người xác nhận bản thiết kế của Claude trước khi sang bước chia nhỏ cho Codex.

## 2026-06-28T02:33:43Z

Chạy phân tích và kiểm thử toàn diện dự án Genius bằng các bài test hiện có (e2e, stress, performance) để đảm bảo hệ thống hoạt động mượt mà, ổn định và ít lỗi nhất khi vận hành thực tế. Mục tiêu chính là phát hiện lỗi và điểm nghẽn hiệu năng, sau đó xuất báo cáo chi tiết.

Working directory: e:\Project\Genius
Integrity mode: benchmark

## Requirements

### R1. Thực thi kiểm thử
Chạy các bài kiểm thử tự động hiện có trong dự án (bao gồm `test_e2e.py`, `test_stress.py`, `test_performance.py` và các test liên quan khác) bằng pytest.

### R2. Phân tích kết quả
Thu thập kết quả chạy test, phân tích các lỗi (errors), ngoại lệ (exceptions) và các điểm nghẽn hiệu năng (performance bottlenecks) xuất hiện trong quá trình chạy.

### R3. Báo cáo chi tiết
Tạo một báo cáo tổng hợp chi tiết (`test_report.md`) ghi nhận lại trạng thái của hệ thống, liệt kê rõ các vấn đề gặp phải để làm cơ sở tối ưu hóa sau này.

## Acceptance Criteria

### Verification
- [ ] Tệp báo cáo `test_report.md` được tạo thành công trong thư mục làm việc hoặc dưới dạng artifact.
- [ ] Báo cáo phải chứa dữ liệu thực tế từ việc chạy các tệp test (số lượng pass/fail, thời gian chạy).
- [ ] Báo cáo có phần liệt kê chi tiết các lỗi hoặc cảnh báo về hiệu suất phát hiện được trong quá trình chạy thử.

## 2026-06-28T02:58:27Z

# Teamwork Project Prompt — Draft

Thực hiện tối ưu hóa mã nguồn các bài kiểm thử của dự án Genius dựa trên kế hoạch đã đề xuất (`implementation_plan.md`), nhằm loại bỏ các điểm nghẽn hiệu năng và kết nối mạng thực tế không mong muốn.

Working directory: e:\Project\Genius
Integrity mode: development

## Requirements

### R1. Cập nhật `test_e2e.py` và các test orchestrator
Tìm và thêm lệnh mock `patch("httpx.AsyncClient.get")` bên cạnh `patch("httpx.AsyncClient.post")` tại tất cả các bài test đang thiếu (ví dụ: `test_t4_real_world_unauthorized_agent_aborts_pipeline`, `test_f6_...`). Đảm bảo không có bài test E2E/Integration nào gọi HTTP thực tế ra ngoài.

### R2. Cập nhật `test_devops_security_challenger.py`
Giả lập (mock) các phương thức `send_prompt` của `OpenAIProvider` và `AnthropicProvider` (hoặc mock ở cấp độ cao hơn nếu phù hợp) để ngăn chặn các request API thực tế chậm chạp.

### R3. Dọn dẹp thư mục rác
Xóa tất cả các thư mục con bên trong thư mục `projects/` để ngăn chặn pytest quét nhầm các file python giả lập bị lỗi cú pháp.

## Acceptance Criteria

### Verification
- [ ] Chạy lệnh `pytest test_e2e.py test_devops_security_challenger.py -v` phải thành công 100%.
- [ ] Tổng thời gian chạy của các bài test trên phải giảm đáng kể (không còn các bài test bị nghẽn 10 giây hoặc 5 giây).
- [ ] Lệnh `pytest` có thể chạy trên toàn dự án mà không cần cờ `--ignore=projects`.

## 2026-06-28T07:40:25Z

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


## 2026-06-28T10:56:23Z

# Teamwork Project Prompt

Nâng cấp hai tính năng cốt lõi cho hệ thống Genius: (1) Cấp khả năng tự động quản lý mã nguồn qua Git cho tất cả Agent; (2) Tích hợp quy trình "Tranh biện Đa Tác nhân" (Multi-Agent Debate) vào giai đoạn lập kế hoạch (Plan).

Working directory: e:\Project\Genius
Integrity mode: benchmark

## Requirements

### R1. Tích hợp bộ công cụ Git & Tự động Xác thực
Tạo module công cụ Git cung cấp các lệnh `clone`, `status`, `add`, `commit`, `pull`, `push` và tiêm vào tất cả các Agent. Hệ thống phải tự động đọc `GIT_USERNAME` và `GIT_TOKEN` từ `.env` để chèn vào các lệnh remote một cách bảo mật, tuyệt đối không rò rỉ token ra console.

### R2. Thiết lập quy trình Debate (Phản biện Kế hoạch)
Thay đổi luồng của `orchestrator`: 
1. Sau khi `ClaudeArchitectAgent` xuất bản kế hoạch ban đầu (Draft Plan).
2. Orchestrator tự động gửi bản kế hoạch này cho `GrokReviewer` (hoặc một Critic Agent chuyên trách) để dò tìm lỗ hổng kiến trúc, edge-cases.
3. Nếu Grok tìm ra lỗi, feedback được gửi lại cho Claude để sửa (Refine). Nếu Grok trả về `[APPROVED]`, quá trình kết thúc.
4. Đặt giới hạn `MAX_DEBATE_ROUNDS = 2` để tránh lặp vô tận.

## Acceptance Criteria

### Verification
- [ ] Chạy thành công bộ test mới `test_git_tools.py` để chứng minh Agent có thể gọi `clone`/`commit` an toàn.
- [ ] Viết một bài test tích hợp (`test_debate_flow.py` hoặc thêm vào e2e) giả lập một plan có lỗi cố ý, chứng minh Grok có thể bắt được lỗi và ép Claude sinh ra bản plan thứ 2.
- [ ] Vòng lặp Debate phải tự thoát ra (không bị infinite loop) khi đạt giới hạn `MAX_DEBATE_ROUNDS`.

## 2026-06-28T11:10:31Z

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

## 2026-06-28T11:53:29Z

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

## 2026-06-28T12:15:26Z

# Teamwork Project Prompt

[Dự án: Đọc phân tích tài liệu trong C:\Users\Admin\Downloads\AGENTS để nâng cấp hệ thống Genius, đồng thời thiết kế một định dạng (format) chuẩn cho Antigravity khi gửi prompt cho các AI khác.]

Working directory: e:\Project\Genius
Integrity mode: development

## Requirements

### R1. Phân tích Tài liệu Toàn diện
Đọc toàn bộ các tệp trong `C:\Users\Admin\Downloads\AGENTS`. Trích xuất các bài học/nguyên tắc về System Prompt, Instruction, và Workflow luồng phối hợp (Multi-agent orchestration) để nâng cấp cho dự án Genius.

### R2. Thiết kế Format Prompt
Dựa trên kiến thức phân tích được, tạo ra một định dạng (format) chuẩn, tối ưu nhất dành cho Antigravity sử dụng khi thiết lập prompt giao việc cho các AI/tác tử khác. Nhóm được toàn quyền quyết định cấu trúc tốt nhất.

### R3. Quy trình Đề xuất & Phê duyệt (Bắt buộc)
Nhóm phải lập một **bản kế hoạch nâng cấp chi tiết** (gồm kết quả phân tích, đề xuất format, và các bước sẽ thay đổi trong source code). TUYỆT ĐỐI DỪNG LẠI báo cáo cho người dùng sau khi viết xong kế hoạch. KHÔNG tự ý thực thi code hay thay đổi hệ thống cho đến khi người dùng đọc kế hoạch và xác nhận.

## Acceptance Criteria

### Báo cáo và Kế hoạch
- [ ] Tạo một tệp `upgrade_plan.md` chứa chi tiết phân tích từ tài liệu, đề xuất format prompt mới (kèm giải thích lý do), và danh sách các file/luồng cần sửa trong dự án Genius.
- [ ] Định dạng prompt đề xuất phải được minh họa bằng một ví dụ cụ thể để người dùng dễ hình dung.

- [ ] Nhóm agent PHẢI KẾT THÚC CÔNG VIỆC (Dừng execution) ngay sau khi tạo xong `upgrade_plan.md` và yêu cầu người dùng phản hồi. Không được có bất kỳ commit hay sửa đổi source code nào trong lần chạy đầu tiên này.

## 2026-06-28T12:25:04Z

# Phase 2: Implementation of the Standardized Prompt Framework Upgrade Plan

The user has approved `upgrade_plan.md` and authorized the execution phase. Modify the source code files in Genius according to the plan, run the tests to verify correctness, ensure all tests pass (or existing ones behave consistently), and report back.

## 2026-06-28T12:31:35Z

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

## 2026-06-28T12:45:00Z

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



