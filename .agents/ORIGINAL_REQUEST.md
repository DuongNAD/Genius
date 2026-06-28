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
