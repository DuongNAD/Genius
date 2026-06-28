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
