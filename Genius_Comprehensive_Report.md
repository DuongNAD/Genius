# Genius: Enterprise Distributed Agent Orchestration Framework
## Báo cáo Kỹ thuật Toàn diện

---

## 1. Kiến trúc Hệ thống & Luồng Phối hợp Agent

Framework Genius đã phát triển từ một kiến trúc khối (monolith) điều khiển qua CLI thành một nền tảng điều phối đa tác tử dạng vi dịch vụ (microservices) phân tán và có khả năng mở rộng cao. Kiến trúc lõi tổ chức các AI agent chuyên biệt thành một pipeline tuần tự, đồng thời cho phép giao tiếp mạng không trạng thái (stateless), xác thực, kiểm tra toàn vẹn (checksum) và cung cấp phản hồi thực thi theo thời gian thực.

### 1.1 Tổng quan về Pipeline Đa tác tử

Vòng đời cốt lõi của một dự án phát triển trong Genius hoạt động như một chuỗi các tác tử chuyên biệt, độc lập. Mỗi tác tử đóng vai trò như một giai đoạn riêng biệt trong quy trình kỹ thuật, tiếp nhận ngữ cảnh có cấu trúc và chuyển kết quả xuống các giai đoạn tiếp theo:

```
[Prompt] -> Grok Researcher -> Claude Architect -> Codex Reviewer -> Tester Agent -> Security Agent -> DevOps Agent -> [Deployment]
```

1. **Grok Researcher Agent (`grok`)**
   - **Vai trò**: Phân tích yêu cầu ban đầu, tìm kiếm công nghệ, kiểm tra tính xác thực và tổng hợp ngữ cảnh thô.
   - **Entrypoint**: lệnh `grok` / `.agents/skills/grok_researcher/run.py`
   - **Service Endpoint**: `http://localhost:8001` (FastAPI wrapper `api.py`)
   - **Đầu vào / Đầu ra**: Nhận mô tả tổng quan từ người dùng và tạo ra một tài liệu nghiên cứu có cấu trúc (`research.md`).

2. **Claude Architect Agent (`claude`)**
   - **Vai trò**: Tiếp nhận yêu cầu và đặc tả thiết kế. Xây dựng kiến trúc hệ thống cấp cao, schema cơ sở dữ liệu, giao diện API và bố cục kế hoạch.
   - **Entrypoint**: lệnh `claude` / `.agents/skills/claude_architect/run.py`
   - **Service Endpoint**: `http://localhost:8002` (FastAPI wrapper `api.py`)
   - **Đầu vào / Đầu ra**: Tiêu thụ `research.md` và tạo ra bản kế hoạch hệ thống chi tiết (`design.md`) chứa danh sách các tệp mã nguồn mục tiêu cần triển khai.

3. **Codex Reviewer Agent (`codex`)**
   - **Vai trò**: Tác tử lập trình chính. Viết mã nguồn sạch, mạnh mẽ khớp với các đặc tả được xác định trong `design.md`.
   - **Entrypoint**: lệnh `codex` / `.agents/skills/codex_reviewer/run.py`
   - **Service Endpoint**: `http://localhost:8003` (FastAPI wrapper `api.py`)
   - **Đầu vào / Đầu ra**: Nhận các tác vụ triển khai tệp và tạo mã Python thô, lưu trực tiếp vào các đường dẫn nguồn được chỉ định (ví dụ: `app.py` hoặc các module cụ thể trong `src/`).

4. **Tester Agent (`tester`)**
   - **Vai trò**: Tạo các bài kiểm thử đơn vị (unit test) và các kịch bản chức năng toàn diện sử dụng `pytest` để kiểm tra mã nguồn.
   - **Entrypoint**: lệnh `tester` / `.agents/skills/tester_agent/run.py`
   - **Service Endpoint**: `http://localhost:8004` (FastAPI wrapper `api.py`)
   - **Đầu vào / Đầu ra**: Quét mã nguồn triển khai và xuất ra các tệp kiểm thử (ví dụ: `test_generated.py` hoặc các tệp kiểm thử nằm cùng cấp trong `tests/`).

5. **Security Agent (`security`)**
   - **Vai trò**: Thực hiện kiểm toán bảo mật tự động trên các mã đã triển khai, kiểm tra các lỗ hổng, các thực hành không an toàn, rò rỉ dữ liệu hoặc các nguy cơ khai thác tiềm ẩn.
   - **Entrypoint**: lệnh `security` / `.agents/skills/security_agent/run.py`
   - **Service Endpoint**: `http://localhost:8005` (FastAPI wrapper `api.py`)
   - **Đầu vào / Đầu ra**: Kiểm tra các tệp nguồn và tạo báo cáo bảo mật markdown (`audit.md`).

6. **DevOps Agent (`devops`)**
   - **Vai trò**: Kiểm tra phần triển khai mã và báo cáo bảo mật để viết các bộ mô tả triển khai (ví dụ: Dockerfiles, cấu hình docker-compose và kịch bản khởi động).
   - **Entrypoint**: lệnh `devops` / `.agents/skills/devops_agent/run.py`
   - **Service Endpoint**: `http://localhost:8006` (FastAPI wrapper `api.py`)
   - **Đầu vào / Đầu ra**: Tiêu thụ `audit.md` và xuất ra các mô tả triển khai (`deploy.md`).

#### Vòng lặp Tinh chỉnh Tranh luận Đa tác tử (Multi-Agent Debate Refinement Loop)
Để nâng cao chất lượng quy hoạch kiến trúc, `orchestrator.py` tích hợp một **Vòng lặp Tranh luận Đa tác tử** giữa Grok và Claude trước khi chốt `design.md`:
* **Vòng lặp**: Grok đóng vai trò là nhà phê bình (`GrokReviewer`) và Claude đóng vai trò kiến trúc sư. Trong một số vòng nhất định (`max_debate_rounds`), Grok phân tích kế hoạch của Claude để tìm ra các khiếm khuyết, yêu cầu bị thiếu hoặc vấn đề bảo mật. Claude sau đó sẽ tinh chỉnh bản thiết kế để phản hồi.
* **Phê duyệt sớm**: Nếu Grok xác định rằng kế hoạch của Claude là tối ưu và hoàn chỉnh, nó sẽ gắn thêm token `[APPROVED]` vào phản hồi của mình. Tác tử điều phối (orchestrator) nhận diện token này và kết thúc sớm vòng lặp tranh luận, lưu bản thiết kế đã tinh chỉnh vào `design.md`.

---

### 1.2 Giao thức Giao tiếp (Communication Protocol)

Framework Genius hỗ trợ hai mô hình giao tiếp mạng: microservices HTTP không trạng thái và mạng lưới tác tử WebSocket phân tán thời gian thực.

#### Chế độ HTTP Không trạng thái
Trong chế độ HTTP, tác tử điều phối tương tác với các máy chủ API qua các endpoint REST (`/run` và `/status/{task_id}`). Bảo mật và tính toàn vẹn dữ liệu được thực thi ở cấp độ mạng thông qua các header tùy chỉnh:
1. **Xác thực JWT**: Orchestrator tạo ra một JSON Web Token (JWT) ngắn hạn sử dụng `SKILL_API_KEY` (bí mật chia sẻ). Token payload chứa đối tượng (`sub`: "orchestrator") và mốc thời gian hết hạn (`exp` đặt trước 5 phút). JWT này được truyền qua các header `Authorization: Bearer <token>` và `X-API-Key`.
2. **Chữ ký Toàn vẹn Dữ liệu**: Để ngăn chặn tấn công xen giữa (middleman) hoặc yêu cầu bị hỏng, tất cả payload POST và GET phải được ký. Người gửi tính toán mã băm SHA-256 của JSON payload và đặt nó vào header `X-Payload-SHA256`. Người nhận tuần tự hóa lại payload nhận được, tính toán băm SHA-256 và so sánh với giá trị trong header. Nếu không khớp sẽ lập tức trả về lỗi `400 Bad Checksum`.

#### Chế độ WebSocket Phân tán
Trong chế độ phân tán, kiến trúc hoạt động theo mô hình hub-and-spoke, nơi một trung tâm đăng ký định tuyến các tác vụ một cách linh hoạt:
* **Kết nối CentralHub & ClientWorker**: Các worker phía client (`genius_worker.py`) kết nối tới `CentralHub` (`serve.py`) qua WebSockets tại `ws://<hub-ip>:<hub-port>/ws/connect?token=<jwt-token>`.
* **Đăng ký & Chống giả mạo danh tính**: Khi kết nối, worker gửi thông báo `register` chứa `worker_id` duy nhất và danh sách các vai trò mà nó hỗ trợ. Hub xác thực rằng `worker_id` được báo cáo trong payload đăng ký khớp với `sub` trong token JWT. Sự không khớp sẽ gây ra lỗi giả mạo danh tính (`4003`) và ngắt kết nối.
* **Điều phối Tác vụ Bất đồng bộ**: Orchestrator gửi yêu cầu tác vụ tới hub. Hub tìm kiếm trong registry một worker đang `idle` (rảnh) hỗ trợ vai trò mục tiêu. Khi được chọn, hub đánh dấu trạng thái worker là `busy`, ghi nhận tác vụ là `running` dưới một `task_id` duy nhất, và chuyển JSON tác vụ tới kết nối WebSocket của worker (`type: "dispatch"`). Orchestrator chờ đợi cho đến khi worker báo cáo kết quả về.
* **Nhịp tim (Heartbeats) và Quét liveness**: Các worker truyền các khung nhịp tim định kỳ tới hub. Hub chạy một vòng lặp `sweeper` dưới nền. Nếu worker không gửi nhịp tim trong thời gian chờ, sweeper sẽ loại bỏ worker, đánh dấu các tác vụ đang chạy của nó là `failed` với lỗi `"Worker disconnected"`, và dọn dẹp kết nối WebSocket bị treo.
* **Checksum kết quả**: Kết quả được các worker báo cáo về chứa một checksum SHA-256 được tính trên JSON kết quả. Hub xác minh checksum này để đảm bảo toàn vẹn dữ liệu trước khi cập nhật trạng thái tác vụ thành `completed` và giải phóng worker trở lại trạng thái `idle`.

---

### 1.3 Định tuyến và Các Thành phần Máy chủ Cốt lõi

Logic điều phối và định tuyến được chia ra trong một số thành phần quan trọng:

* **`serve.py`**: Tập lệnh entrypoint hợp nhất cho tất cả các dịch vụ.
  - *Khởi động Vai trò Động*: Khởi chạy các máy chủ API tác tử cụ thể, bảng điều khiển (dashboard), Central Hub, hoặc orchestrator dựa trên các cờ CLI.
  - *Bảng Định tuyến Lệnh*: Ánh xạ các lệnh (vd: `/research`, `/plan`, `/code`, `/unit-test`, `/audit`, `/deploy`) tới các vai trò tác tử mục tiêu và cổng mặc định của chúng. Nếu cung cấp một prompt chứa lệnh, `serve.py` tự động kích hoạt tác tử được yêu cầu.
* **`orchestrator.py`**: Bộ điều phối trung tâm của pipeline phát triển.
  - *Song song Tác vụ*: Cài đặt một `asyncio.Semaphore(3)` để điều tiết các tác vụ tạo tệp đồng thời. Việc này cho phép tối đa ba tệp được triển khai, kiểm thử và kiểm toán song song, tối đa hóa thông lượng mà không làm quá tải tài nguyên cục bộ.
  - *Cơ chế Caching*: Đánh chặn các yêu cầu API và cache kết quả dựa trên mã băm SHA-256 của URL, prompt, và ngữ cảnh đã sắp xếp. Tránh việc gọi API LLM lặp lại đắt đỏ trong quá trình retry hoặc test suite.
* **`CentralHub`**: Quản lý bộ đăng ký (registry) các worker (`self.workers`), các tác vụ đang chạy (`self.tasks`), cơ chế xếp hàng đợi cho các tác vụ chờ worker, trạng thái cấu hình và xác thực.
* **`ClientWorker`**: Quản lý việc thực thi tác vụ phía worker. Thiết lập kết nối WebSocket, quản lý nhịp tim, ánh xạ các vai trò tác vụ tới module tác tử Python tương ứng (vd `CodexReviewerAgent`), chạy tác tử và báo cáo kết quả lại cho hub.

---

## 2. Hướng dẫn Triển khai & Sử dụng

Genius được thiết kế để dễ dàng triển khai ở chế độ cục bộ (single-node) hoặc phân tán qua nhiều máy ảo (VM nodes).

### 2.1 Bản đồ Cổng Dịch vụ & Bảng Vai trò

Bảng dưới đây tóm tắt việc phân bổ cổng và các vai trò tương ứng:

| Cổng | Tên Dịch vụ | Vai trò | Endpoint / Tệp Chính |
|---|---|---|---|
| **8000** | Central Hub | WebSocket Gateway & Coordinator | `/ws/connect`, `serve.py` |
| **8001** | Grok Researcher API | Tìm kiếm & phân tích yêu cầu | `/run`, `skills/grok_researcher/api.py` |
| **8002** | Claude Architect API | Trình tạo kế hoạch kiến trúc | `/run`, `skills/claude_architect/api.py` |
| **8003** | Codex Reviewer API | Trình tạo mã nguồn | `/run`, `skills/codex_reviewer/api.py` |
| **8004** | Tester Agent API | Trình tạo bộ kiểm thử | `/run`, `skills/tester_agent/api.py` |
| **8005** | Security Agent API | Trình kiểm toán lỗ hổng mã | `/run`, `skills/security_agent/api.py` |
| **8006** | DevOps Agent API | Trình viết mô tả triển khai | `/run`, `skills/devops_agent/api.py` |
| **8080** | Web Dashboard | GUI hiệu suất & Log | `dashboard.py` |

---

### 2.2 Các Lệnh CLI & Khởi động Động

Genius cung cấp các tùy chọn để khởi động thủ công, tương tác và hoàn toàn tự động:

#### 1. Khởi động Tương tác
Để chạy menu tương tác, chỉ cần thực thi:
```bash
python serve.py
```
Thao tác này sẽ nhắc người dùng chọn vai trò nào cần kích hoạt trên host này (vd: `grok,claude` hoặc `1,2,3`).

#### 2. Khởi động Dịch vụ API Cụ thể
Để chỉ chạy các microservices cụ thể (ví dụ, API Grok và Codex):
```bash
python serve.py --roles grok,codex
```

#### 3. Chế độ Tự động (Auto-Pilot Mode)
Để khởi chạy toàn bộ microservices và tự động thực thi toàn bộ pipeline chỉ với một lệnh:
```bash
python serve.py --auto-pilot --prompt "Build a fast URL shortener in python"
```

#### 4. Khởi động Central Hub
Để bật Central Hub chuyên điều phối các worker phân tán:
```bash
python serve.py --distributed --hub-port 8000
```

#### 5. Kết nối Client Workers
Để kết nối các worker riêng biệt tới Central Hub, chạy:
```bash
python client_app/genius_worker.py --hub-ip 127.0.0.1 --hub-port 8000 --roles grok,codex --worker-id node_01
```

---

### 2.3 Quy trình Xác thực JWT & Bắt tay (Handshake)

Chuỗi bắt tay WebSocket và quá trình ủy quyền diễn ra như sau:

```
Client Worker                                                  Central Hub
     |                                                              |
     | -- 1. Tạo JWT (ký bằng SKILL_API_KEY) ----------------------> |
     | -- 2. HTTP GET /ws/connect?token=<JWT> (Yêu cầu Handshake) -> |
     |                                                              |
     |                                                      [Xác minh Chữ ký]
     |                                                      [Xác minh Hết hạn]
     |                                                              |
     | <============== 3. Chấp nhận Kết nối ========================> |
     |                                                              |
     | -- 4. Gửi thông báo register {"worker_id": "...", ...} =====> |
     |                                                              |
     |                                                      [Xác minh Danh tính]
     |                                                      [worker_id == JWT sub]
     |                                                              |
     | <============== 5. Gửi thông báo đã đăng ký ================> |
```

1. **Tạo Token**: Client Worker mã hóa JWT bằng cách sử dụng `SKILL_API_KEY` làm khóa bí mật.
2. **Yêu cầu WebSocket**: Client yêu cầu kết nối tới `ws://<hub-ip>:<hub-port>/ws/connect?token=<jwt-token>`.
3. **Hub Xác minh**: Hub giải mã token, xác thực chữ ký và tính hợp lệ. Nếu không hợp lệ, Hub ngắt kết nối với mã lỗi `4001`.
4. **Đăng ký**: Client gửi JSON đăng ký. Hub kiểm tra xem worker có đang giả mạo node khác không bằng cách đối chiếu `worker_id` với mục `sub` trong token. Nếu khớp, worker được lưu vào registry và vào trạng thái `idle` (rảnh).

---

### 2.4 Quản lý Cấu hình & Biến Môi trường

**Lưu ý quan trọng (Bản nâng cấp mới nhất):** Hệ thống đã **loại bỏ hoàn toàn** việc sử dụng các biến môi trường API Key (như `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GROK_API_KEY`). Thay vào đó, toàn bộ giao tiếp LLM được xử lý thông qua các **Local CLI Providers** (các công cụ dòng lệnh cục bộ như `claude`, `grok`, `codex`) đã được định cấu hình đăng nhập tài khoản sẵn trên máy tính. Điều này đảm bảo an toàn, tối ưu chi phí và không phụ thuộc vào hạ tầng mạng ngoài.

Các biến môi trường hiện tại đang được sử dụng:
* **`SKILL_API_KEY`**: Khóa bí mật chia sẻ dùng để ký và xác thực token JWT cùng với payload checksum.
* **`GENIUS_DB_PATH`**: Đường dẫn tuyệt đối tới cơ sở dữ liệu SQLite (mặc định là `genius.db`).
* **`GENIUS_MEMORY_DB_PATH`**: Đường dẫn tới cơ sở dữ liệu dự phòng cho vector memory (mặc định bằng giá trị `GENIUS_DB_PATH`).

---

## 3. Nâng cấp SPO & Vòng lặp Phản hồi Tự phục hồi

Framework Đối tượng Prompt Chuẩn hóa (SPO - Standardized Prompt Object) áp dụng các Quy tắc AI-Native Engineering một cách lập trình, biến các prompt thô thành các payload có cấu trúc và khả năng tự sửa lỗi (self-healing).

### 3.1 Thiết kế Schema của Đối tượng Prompt Chuẩn hóa (SPO)

Thay vì truyền các chuỗi vô cấu trúc, các tác tử nhận được một SPO có cấu trúc với 4 khối chính:

1. **`meta`**: Theo dõi ngữ cảnh thực thi. Chứa `task_id` (định danh tác vụ xuyên suốt nhiều agent), `caller_agent` (tác tử gọi nó), `target_role`, và `attempt` / `max_attempts`.
2. **`instructions`**: Thực thi các ràng buộc hệ thống. Chứa `system_rules` (gồm `AGENT_CORE_RULES`), `role_specific_instructions`, `constraints` (vd: "Giữ hàm dưới 50 dòng", "Không truy vấn file .env trực tiếp"), và `verification_command` (vd: `pytest tests/test_jwt.py`).
3. **`payload`**: Xác định mô tả bài toán. Chứa `problem_description`, `slash_command` (vd: `/code`), `target_file`, và mã tham chiếu tùy chọn `reference_code`.
4. **`feedback_loop`**: Lưu trữ nhật ký kiểm định. Chứa `previous_errors`, `test_logs`, và `criticism` (vd: lỗi linter hoặc phát hiện bảo mật).

#### Biên dịch Prompt (PromptBuilder)
SPO có cấu trúc được biên dịch một cách lập trình thành các Prompt System và User:
* **System Prompt**: Xây dựng bằng cách kết hợp các quy tắc hệ thống, hướng dẫn cụ thể theo vai trò, các ràng buộc dựa trên mảng, và lệnh kiểm chứng (verification). Điều này đảm bảo model nhận thức được chính xác các quy tắc phong cách mã và lệnh sẽ dùng để test đầu ra của nó.
* **User Prompt**: Kết hợp slash command, tệp mục tiêu, mô tả bài toán, mã tham chiếu, và các nhật ký vòng lặp phản hồi (số lần thử, lỗi, compiler thất bại và lời phê bình).

Schema này áp đặt việc **Tách biệt giữa Lên kế hoạch và Triển khai** (bằng cách hướng dẫn model quy hoạch rõ ràng trong instructions), **Kỷ luật Ngữ cảnh** (tách biệt instruction và mã nguồn để tránh suy giảm ngữ cảnh), và **Tua lại và Re-Prompt** (cho phép xóa lịch sử cũ và chỉ đưa code mới nhất và lỗi mới nhất vào khi `attempt > 1`).

---

### 3.2 Cơ chế Tích hợp & Giao tiếp Đa phương thức (CLI & Web)

Để loại bỏ hoàn toàn sự phụ thuộc vào mạng internet và các API Keys, Genius (Antigravity 2.0) tương tác với các model thông qua hai phương thức song song: **Local CLI Wrappers** (cho Claude, Grok) và **Headless Browser Automation** (cho Codex). Cách thức kết nối và truyền dữ liệu diễn ra như sau:

1. **Sinh Payload (Payload Generation)**: `PromptBuilder` biên dịch SPO thành hai thành phần văn bản riêng biệt: System Prompt và User Prompt.

2. **Truyền Dữ liệu qua Local CLI (Dành cho Claude, Grok)**:
   - *Kích hoạt*: Module `providers/` sử dụng `subprocess` của Python để khởi tạo tiến trình cục bộ, gọi thẳng vào các công cụ CLI đã đăng nhập.
   - *Input*: System Prompt và User Prompt được truyền vào thông qua luồng **Standard Input (stdin)** hoặc file `.txt` tạm thời để vượt giới hạn độ dài ký tự của command-line.
   - *Output*: Lắng nghe **Standard Output (stdout)** để lấy mã nguồn. Lỗi được bắt qua **Standard Error (stderr)**.

3. **Truyền Dữ liệu qua Desktop GUI Automation (Dành cho Codex)**:
   - *Ngữ cảnh*: Codex được cung cấp dưới dạng một **Desktop App** chính thức (Ứng dụng máy tính) dành cho phát triển phần mềm bằng AI. Do đó, hệ thống sử dụng các thư viện tự động hóa giao diện hệ điều hành (ví dụ: `pywinauto` hoặc UI Automation) để tương tác trực tiếp với cửa sổ ứng dụng Codex đã đăng nhập tài khoản.
   - *Input*: Script tự động xác định vùng nhập liệu trên giao diện ứng dụng (UI Elements), sau đó giả lập thao tác mô phỏng người dùng (điền text hoặc dán từ clipboard) để gửi System Prompt và User Prompt vào một luồng (thread) hoặc dự án mới trên Codex.
   - *Output*: Hệ thống giám sát trạng thái UI của ứng dụng. Khi Codex hoàn thành việc sinh mã hoặc thực thi agent, script sẽ tự động trích xuất (extract) nội dung text từ giao diện trả về hoặc đọc trực tiếp các thay đổi mà ứng dụng vừa ghi xuống thư mục làm việc cục bộ (worktree) để báo cáo lại cho Orchestrator.

4. **Dọn dẹp An toàn**: Mọi tệp tin tạm chứa prompt hoặc các luồng thread tạm thời sinh ra trên giao diện Desktop App sẽ được script tự động đóng và dọn dẹp sạch sẽ sau mỗi chu kỳ.

---

### 3.3 Vòng lặp Tự phục hồi của Codex & Tester

Genius tích hợp các vòng lặp tự phục hồi ở cả cấp độ pipeline và cấp độ từng tác tử độc lập:

#### Vòng lặp Phản hồi của CodexReviewerAgent
Khi thực thi chỉ thị `/code` hoặc `/refactor`:
1. Agent biên dịch SPO và gửi đến provider. Mã sinh ra được ghi vào tệp mục tiêu.
2. Agent chạy linter (`flake8`) và bộ kiểm thử (`pytest`) trên tệp mục tiêu.
3. Nếu `pytest` trả về mã lỗi, agent bước vào vòng lặp tự phục hồi với tối đa `max_retries` lần (mặc định: 3):
   - Nó re-prompt model, nhồi khối `feedback_loop` của SPO bằng các lỗi pytest, lỗi linter và kiểm toán bảo mật.
   - Nó ghi đè tệp mục tiêu bằng mã đã sửa và chạy lại suite kiểm chứng.
   - Nếu test pass, vòng lặp ngắt sớm.
4. Nó gắn thêm linter và test log vào cuối Markdown phản hồi được trả về.

#### Vòng lặp Phản hồi của TesterAgent
Khi thực thi chỉ thị `/unit-test`:
1. Agent biên dịch SPO để tạo test suite và ghi vào tệp kiểm thử mục tiêu.
2. Agent chạy `pytest` trên tệp đó.
3. Nếu test thất bại (do lỗi assertion hoặc import), agent lặp lại tới `max_retries`:
   - Nó gửi traceback và lỗi pytest về cho model, yêu cầu sửa mã test.
   - Nó ghi đè tệp kiểm thử với mã đã sửa và chạy lại `pytest`.
   - Một khi test pass thành công, vòng lặp kết thúc.
4. Agent đính kèm nhật ký pytest cuối cùng như một bằng chứng thực thi trong Markdown phản hồi.

---

### 3.4 Tính ổn định Hạ tầng & Khắc phục Rò rỉ Tài nguyên

Để hỗ trợ độ ổn định cấp độ production và tính đồng thời cao, một số nâng cấp ổn định đã được triển khai:

* **Ràng buộc Port 0 Động**: Trong môi trường testing, các microservices được cấu hình trỏ tới cổng `0`. Việc này ủy quyền phân bổ cổng cho OS host, ngăn chặn xung đột "port in use" và cho phép thực thi test sandbox song song.
* **WebSocket Sweeper Cleanup**: Vòng lặp sweeper của Central Hub chạy liên tục độc lập với khóa DB. Nó phát hiện các worker không hoạt động, đóng WebSocket một cách sạch sẽ, fail các task đang chạy của chúng, và loại bỏ khỏi registry để tránh rò rỉ RAM và kết nối treo.
* **Hủy Tác vụ (Task Cancellation)**: Nếu một worker mất kết nối khi đang chạy task, một tín hiệu hủy sẽ được gửi, task đó bị chấm dứt ngay lập tức để giải phóng tài nguyên.
* **Task Eviction**: FastAPI cache các task đang chạy trên RAM bằng dictionary. Để tránh cạn kiệt bộ nhớ (OOM/DoS), danh sách bị giới hạn ở 100 entries. Khi có task mới, các task cũ nhất đã completed/failed sẽ bị loại bỏ.

---

## 4. Cấu trúc Thư mục Lõi & Cơ sở Dữ liệu

Dự án Genius sử dụng bố cục thư mục mô-đun và cơ sở dữ liệu SQLite cho lưu trữ bền vững và vector memory.

### 4.1 Cấu trúc Thư mục Framework

```
Genius/
│
├── ag_core/                              # SDK Library Lõi
│   ├── agents/                           # Định nghĩa Agent (Grok, Claude, Codex, Tester, v.v.)
│   ├── distributed/                      # Điều phối phân tán (hub.py, worker.py)
│   ├── interfaces/                       # Lớp trừu tượng (base_agent.py, base_provider.py)
│   ├── memory/                           # Cơ sở dữ liệu Vector (vector_store.py)
│   ├── providers/                        # Local CLI Providers (claude_cli, grok_cli, codex_cli)
│   ├── scanner/                          # Trình quét dự án (project_scanner.py)
│   └── utils/                            # Tiện ích (db.py, jwt.py, logger.py, rate_limiter.py)
│
├── .agents/
│   └── skills/                           # Các API tùy chỉnh
│       ├── grok_researcher/              # Grok api.py & run.py
│       ├── claude_architect/             # Claude api.py & run.py
│       ├── codex_reviewer/               # Codex api.py & run.py
│       ├── tester_agent/                 # Tester api.py & run.py
│       ├── security_agent/               # Security api.py & run.py
│       └── devops_agent/                 # DevOps api.py & run.py
│
├── client_app/                           # Các module client application (genius_worker.py, genius_client.py)
├── tests/                                # Test suites
├── serve.py                              # Script khởi động động (Dynamic bootup)
├── orchestrator.py                       # Script điều phối (Pipeline orchestrator)
├── dashboard.py                          # Bảng điều khiển Web (Frontend & Backend)
├── requirements.txt                      # Các thư viện phụ thuộc
└── config.yaml                           # Các cấu hình hệ thống
```

---

### 4.2 Schema Cơ sở dữ liệu SQLite

Genius dựa trên SQLite (`genius.db`) được cấu hình tính năng **Write-Ahead Logging (WAL)** và tự động dọn dẹp (auto-vacuuming) để có hiệu suất ghi và an toàn tốt nhất. Có 3 bảng chính:

#### 1. `conversations`
Lưu trữ log tổng quát prompt-result từ pipeline:
* `id`: INTEGER PRIMARY KEY AUTOINCREMENT
* `timestamp`: DATETIME DEFAULT CURRENT_TIMESTAMP
* `prompt`: TEXT
* `result`: TEXT

#### 2. `agent_logs`
Theo dõi trạng thái thực thi của từng sub-task được chuyển tới các agents:
* `id`: INTEGER PRIMARY KEY AUTOINCREMENT
* `timestamp`: DATETIME DEFAULT CURRENT_TIMESTAMP
* `task_id`: TEXT
* `agent_name`: TEXT
* `prompt`: TEXT
* `result`: TEXT
* `status`: TEXT (vd: `started`, `success`, `failure`)
* `error`: TEXT (lưu trữ tracebacks hoặc thông báo lỗi nếu status là `failure`)

#### 3. `agent_vector_memory_fallback`
Hoạt động như bảng fallback dự phòng cho module VectorMemory:
* `id`: TEXT PRIMARY KEY (chuỗi UUID)
* `collection_name`: TEXT (tên tác tử)
* `text`: TEXT (nội dung gốc)
* `metadata`: TEXT (từ điển metadata được định dạng JSON)
* `embedding`: TEXT (mảng số nguyên chuẩn hóa TF-IDF được lưu dạng JSON)
* `timestamp`: DATETIME DEFAULT CURRENT_TIMESTAMP
* Khóa phụ (Index): `idx_collection` index trên cột `collection_name`.

---

### 4.3 Nhúng Vector SQLite/Chroma & Khả năng Tìm kiếm Ngoại tuyến (Offline-Safe)

Để bật lưu trữ bộ nhớ và truy hồi của tác tử mà không phụ thuộc vào API nhúng của bên thứ 3 (như OpenAI), Genius cài đặt một công cụ vector memory tùy chỉnh:

#### Lớp SimpleTFIDFEmbedding
- **Mục đích**: Sinh ra biểu diễn vector ngoại tuyến nhất quán cho đoạn văn bản.
- **Số chiều (Dimensionality)**: Cấu hình mặc định ở 128 chiều.
- **Mã hóa (Tokenization)**: Chuẩn hóa text thành viết thường và tách từ bằng regex `\w+`.
- **Tính toán Tần suất (TF)**: Đếm số lần xuất hiện của token chia cho tổng token trong tài liệu.
- **Băm Nhất quán (Deterministic Hashing)**: Để map vô hạn từ vào 128 chiều, hệ thống tính toán mã băm MD5 của từng từ, phân tích thành chuỗi base-16 và thực hiện chia lấy dư:
  $$\text{Index} = \text{MD5}(\text{word}) \pmod{\text{vector\_dim}}$$
- **Chuẩn hóa Vector L2**: Để tính toán Cosine Similarity, vector được chuẩn hóa về độ dài 1 (unit length):
  $$\vec{v}_{\text{normalized}} = \frac{\vec{v}}{\|\vec{v}\|_2}$$

#### Tìm kiếm Độ Tương đồng và Logic Fallback
* **Lưu trữ Chroma DB**: Nếu thư viện `chromadb` được cài và `use_chroma` được bật, hệ thống sẽ kết nối với một Chroma database tại local.
* **Fallback SQLite**: Nếu Chroma bị thiếu hoặc bị lỗi khởi tạo, `VectorMemory` lập tức chuyển qua bảng `agent_vector_memory_fallback` trên SQLite.
* **Cosine Similarity qua Tích Vô Hướng (Dot Product)**: Trong chế độ fallback, hệ thống truy vấn toàn bộ bộ nhớ SQLite của một tác tử. Hệ thống tính vector TF-IDF chuẩn hóa cho câu query. Do cả vector query và vector memory đều chuẩn hóa L2, nên phép toán **cosine similarity** được đơn giản hóa thành **tích vô hướng**:
  $$\text{Cosine Similarity} = \vec{q} \cdot \vec{d}$$
  Hệ thống thực hiện dot product, sắp xếp theo thứ tự giảm dần và trả về top `n_results` văn bản khớp nhất.
