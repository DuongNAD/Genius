# Original User Request

## Initial Request — 2026-06-26T04:35:44Z

# Teamwork Project Prompt — Draft

> Status: Ready for launch — awaiting user approval
> Goal: Craft prompt → get user approval → delegate to teamwork_preview

Xây dựng một kịch bản Điều phối viên (Orchestrator) bằng Python ở cấp độ POC/Demo. Kịch bản này có nhiệm vụ gọi và kết nối tuần tự 4 công cụ dòng lệnh (CLI) của Grok, Claude, Antigravity, và Codex để tạo thành một luồng làm việc tự động (pipeline) phục vụ việc phát triển phần mềm.

Working directory: e:/tool/Genius
Integrity mode: development

## Requirements

### R1. Pipeline Điều Phối 4 AI
Tạo một file `orchestrator.py` chứa luồng thực thi (pipeline) gọi 4 công cụ CLI theo thứ tự: Grok (nghiên cứu), Claude (thiết kế), Antigravity (lập trình), và Codex (review). Sử dụng `subprocess` hoặc các thư viện Python chuẩn để thực thi các lệnh CLI này.

### R2. Chia Sẻ Ngữ Cảnh (Shared Context)
Dữ liệu đầu ra của một AI phải được lưu trữ dưới dạng các file (ví dụ: `research.md`, `design.md`) để làm đầu vào cho AI tiếp theo trong chuỗi.

### R3. Xử Lý Lỗi (Error Handling)
Kịch bản phải bắt được các mã lỗi (non-zero exit codes) từ CLI, in ra log rõ ràng tại bước nào bị lỗi và dừng chương trình (hoặc cảnh báo) thay vì chạy tiếp các bước sau một cách mù quáng.

### R4. Kịch Bản Kiểm Thử (Testing)
Tạo một file kiểm thử `test_orchestrator.py` sử dụng thư viện `unittest` hoặc `pytest`. Kiểm thử này không được gọi CLI thật (để tránh tốn phí/thời gian) mà phải "mock" (giả lập) lệnh `subprocess` để đảm bảo chuỗi thực thi chạy đúng trình tự và các file ngữ cảnh được tạo/đọc đúng cách.

## Acceptance Criteria

## Chức năng và Mã nguồn
- [ ] Tồn tại file `orchestrator.py` có thể chạy độc lập.
- [ ] Hàm thực thi gọi chính xác 4 lệnh CLI như yêu cầu.
- [ ] Nếu một lệnh CLI (mock) trả về mã lỗi `returncode != 0`, kịch bản phải phát hiện và ghi log lỗi.

### Kiểm thử (Verification)
- [ ] Tồn tại file `test_orchestrator.py`.
- [ ] Chạy `pytest test_orchestrator.py` (hoặc `python -m unittest`) phải pass 100% các bài test giả lập chuỗi 4 AI.
- [ ] Test giả lập phải kiểm tra xem các file `research.md` và `design.md` có được hệ thống tạo ra thông qua luồng chạy mock hay không.

## Follow-up — 2026-06-26T06:10:54Z

# Teamwork Project Prompt — Draft

> Status: Ready for launch — awaiting user approval
> Goal: Craft prompt → get user approval → delegate to teamwork_preview

Xây dựng hệ thống Antigravity 2.0 Core Framework (Phiên bản Enterprise). Đây là một hệ thống Đa Tác Nhân (Multi-Agent) viết bằng Python thuần, áp dụng kiến trúc phân tầng: Kỹ năng (Skill) -> Tác nhân (Agent) -> Bộ cung cấp (Provider) -> API.

Working directory: e:/tool/Genius
Integrity mode: development

## Requirements

### R1. Xây dựng Lõi Core Framework (ag_core)
Tạo thư mục `ag_core/` chứa các thành phần lõi:
- Khai báo Lớp giao diện (ABC) `base_provider.py` và `base_agent.py`.
- Tạo các file `openai_provider.py`, `anthropic_provider.py`, `grok_provider.py` kế thừa từ BaseProvider.
- Áp dụng `asyncio` cho các thao tác gọi mạng.
- Dùng thư viện `tenacity` để bọc các hàm gọi API (Exponential Backoff, tối đa 3 lần thử).
- Sử dụng `pydantic` cho các schema dữ liệu (nếu cần ép kiểu JSON trả về).

### R2. Xây dựng ProjectScanner
Tạo `ag_core/scanner/project_scanner.py` sử dụng thư viện `pathspec` để đọc chuẩn `.gitignore` và lọc các file rác (`node_modules`, `venv`, binary files). Tích hợp logic cắt gộp file thông minh sử dụng `tiktoken` (giả lập hoặc dùng thư viện thật) để không vượt quá giới hạn token.

### R3. Theo dõi chi phí (Token Tracker)
Tạo `ag_core/utils/logger.py` để in ra Console. Log phải bao gồm ước tính chi phí (Token cost) được bóc tách từ kết quả trả về của Provider.

### R4. Lớp Ứng dụng (Custom Skills)
Tạo cấu trúc thư mục `.agents/skills/`:
- `codex_reviewer/SKILL.md` & `run.py`
- `grok_researcher/SKILL.md` & `run.py`
- `claude_architect/SKILL.md` & `run.py`
Các script `run.py` phải cực kỳ ngắn gọn, chỉ làm nhiệm vụ khởi tạo Agent tương ứng và gọi hàm `run()` bằng `asyncio`.

### R5. File Cấu hình (Configuration)
Tạo `config.yaml` chứa cấu hình logic (Model name, chunk size limit) và `.env` (mặc định bị ignore) chứa biến môi trường. Tạo module load cấu hình.

## Acceptance Criteria

### Chức năng và Mã nguồn
- [ ] Tồn tại cấu trúc thư mục đúng như thiết kế (`ag_core` và `.agents/skills`).
- [ ] Code tuân thủ kiến trúc Bất đồng bộ (`async def`, `await`).
- [ ] Lớp Provider kế thừa chuẩn từ lớp ABC.
- [ ] Sử dụng đúng 4 thư viện yêu cầu: `pydantic`, `tiktoken`, `tenacity`, `pathspec` (thể hiện qua `requirements.txt` và `import`).

### Kiểm thử (Verification)
- [ ] Viết bộ test sử dụng `unittest.mock` để giả lập hàm `send_prompt` của API.
- [ ] Chạy `pytest` phải kiểm tra được:
    1. Khi Provider bị lỗi mô phỏng (raise Exception), `tenacity` phải thực hiện tự động gọi lại (retry) đúng số lần cấu hình.
    2. `ProjectScanner` đọc được file rác và bỏ qua chúng thành công.
    3. Luồng từ `run.py` -> `Agent` -> `Scanner` -> `Provider (Mock)` kết thúc trơn tru và trả về kết quả giả lập.
- [ ] Bài kiểm thử phải chạy pass 100% không cần kết nối mạng thật.

## Follow-up — 2026-06-27T02:55:40Z

# Teamwork Project Prompt — Draft

> Status: Ready for launch — awaiting user approval
> Goal: Craft prompt → get user approval → delegate to teamwork_preview

Thực hiện kiểm toán (audit) dự án "Antigravity 2.0 Enterprise Framework" sau sự cố crash (Lỗi 429). Mục tiêu tối thượng: Khám nghiệm hiện trạng mã nguồn và đúc kết toàn bộ ngữ cảnh, thiết kế kiến trúc, và tiến độ vào một file Kế hoạch Chuyển giao (Handoff Plan) cực kỳ chi tiết. File này sẽ được push lên Git để khi clone sang máy tính khác, một AI mới có thể đọc và hiểu ngay lập tức phải làm tiếp những gì mà không bị mất bối cảnh (context loss).

Working directory: e:/tool/Genius
Integrity mode: development

## Requirements

### R1. Phân tích Hiện trạng Mã nguồn (Audit)
Quét toàn bộ mã nguồn trong thư mục `ag_core/` và `.agents/skills/`. Xác định chính xác các file nào đã được code hoàn thiện, file nào code dở dang lúc bị crash, và module nào chưa được tạo.

### R2. Tổng hợp Ngữ Cảnh Kiến Trúc (Architecture Context)
Ghi nhận lại toàn bộ "Luật chơi" và thiết kế của dự án vào file báo cáo để AI ở máy khác hiểu cách code. Bao gồm:
- Mô hình: Skill (Định tuyến) -> Agent (Logic) -> Provider (Gọi API).
- Sử dụng Vanilla Python với 4 thư viện lõi: `pydantic` (JSON), `tiktoken` (đếm token), `tenacity` (kháng lỗi retry), `pathspec` (lọc rác .gitignore).
- Lớp Provider phải kế thừa từ lớp trừu tượng (ABC) `base_provider.py`.
- Tất cả phải sử dụng Bất đồng bộ (`async`/`await`).

### R3. Tạo File Handoff & Lộ trình (Portable Roadmap)
Tạo một file có tên `HANDOFF_ROADMAP.md` tại thư mục gốc. File này phải đóng vai trò như "bộ não sao lưu", chứa:
1. **Dự án là gì & Kiến trúc cốt lõi** (Lấy từ R2).
2. **Tiến độ hiện tại**: Chi tiết những gì đã xong, đang dở, và nguyên nhân crash (Lỗi 429).
3. **Mục tiêu tiếp theo (Next Steps)**: Danh sách công việc (To-do list) cực kỳ chi tiết, từng file một, để AI mới biết chính xác dòng code nào cần viết tiếp.
4. **Cấu trúc thư mục (Tree)**: Vẽ lại cây thư mục hiện tại để dễ hình dung.

### R4. Chuẩn bị Git Commit
Sau khi tạo xong file `HANDOFF_ROADMAP.md`, sử dụng lệnh `git add .` và `git commit -m "chore: save state and generate handoff roadmap for machine transfer"`. Đảm bảo file `.env` đã nằm trong `.gitignore` trước khi commit để không lộ API Key. (Việc push lên remote sẽ do người dùng tự làm hoặc cấu hình sau).

## Acceptance Criteria

### Chức năng và Mã nguồn
- [ ] File `HANDOFF_ROADMAP.md` được tạo ra chứa đầy đủ bối cảnh kiến trúc, hiện trạng và lộ trình tiếp theo.
- [ ] Bất kỳ một AI nào (Claude, GPT-4, hay Antigravity trên máy khác) khi đọc file này đều hiểu 100% luật code của dự án và biết phải code file nào tiếp theo.
- [ ] Trạng thái dự án đã được commit vào Git nội bộ (Local Git) an toàn, không chứa file `.env`.

### Kiểm thử (Verification)
- [ ] Người dùng đọc qua file `HANDOFF_ROADMAP.md` và xác nhận nó đủ chi tiết để mang sang máy khác.
- [ ] Chạy lệnh `git log -1` hiển thị commit lưu trạng thái thành công.
