# Genius Multi-Agent Framework 🚀

Genius là một hệ thống siêu tác tử (Agentic Framework) tự trị chuyên dụng cho việc lập trình, refactor mã nguồn và kiểm thử phần mềm tự động. Điểm đột phá lớn nhất của Genius (phiên bản Antigravity 2.0) là **chạy hoàn toàn 100% Offline qua Local CLI**, loại bỏ hoàn toàn sự phụ thuộc vào API Keys hay kết nối mạng ngoài.

## 🌟 Tính năng Cốt lõi

1. **Giao tiếp Local CLI Tốc độ cao:** 
   Giao tiếp trực tiếp với các mô hình thông qua tài khoản đã đăng nhập sẵn trên máy tính của bạn:
   - **Claude CLI & Grok CLI**: Giao tiếp qua `subprocess` và Standard I/O.
   - **Codex Desktop CLI**: Tích hợp cực sâu thông qua tệp thực thi ẩn, bóc tách luồng sự kiện JSONL siêu chuẩn xác.
   
2. **Standardized Prompt Object (SPO):**
   Mọi luồng giao tiếp giữa các Tác tử đều được cấu trúc hóa dưới dạng SPO, giúp tách biệt ngữ cảnh, kế hoạch, lệnh thực thi và vòng lặp lỗi (Feedback Loop).

3. **Vòng lặp Tự phục hồi (Self-Healing):**
   Nếu mã sinh ra bị lỗi, hệ thống tự động bóc tách lỗi từ `pytest` hoặc `flake8` để gửi lại mô hình, yêu cầu sửa lỗi cho tới khi thành công.

4. **Kiến trúc Bền bỉ (Robust Architecture):**
   Xử lý rò rỉ RAM/tiến trình thông qua Central Hub WebSocket Sweeper, giới hạn Task Cache và tự động dọn dẹp các tệp tạm thời sau khi xử lý.

## 🤖 Đội hình Tác tử (Agents)

- **Claude Architect**: Kiến trúc sư hệ thống, thiết kế cấu trúc thư mục và lên kế hoạch tổng thể.
- **Codex Reviewer**: Chuyên gia lập trình và refactor mã nguồn, hoạt động qua Codex Desktop CLI.
- **Tester Agent**: Viết Unit Test và Integration Test tự động.
- **Grok Researcher**: Thu thập và phân tích tài liệu/yêu cầu người dùng thông qua Grok CLI.
- **DevOps & Security Agent**: Chuyên trách triển khai, tối ưu CI/CD và kiểm toán mã nguồn (Victory Auditor).

## 🚀 Hướng dẫn Cài đặt & Sử dụng

### 1. Yêu cầu Hệ thống
- Python 3.10+
- Ứng dụng **Codex Desktop** đã cài đặt và đăng nhập.
- Các công cụ CLI **Claude** và **Grok** đã cài đặt.

### 2. Khởi chạy
Chạy server FastAPI chính để điều phối hệ thống:
```bash
python serve.py
```
Hệ thống sẽ chạy ở port động để tránh xung đột, bạn có thể kiểm tra Dashboard hoặc dùng `client_app` để bắn lệnh (SPO) vào hệ thống.

---
> **Lưu ý**: Dự án không dùng các biến môi trường như `OPENAI_API_KEY` hay `ANTHROPIC_API_KEY` nữa. Hãy đảm bảo bạn đã đăng nhập tài khoản trên các ứng dụng cục bộ trước khi chạy Genius!
