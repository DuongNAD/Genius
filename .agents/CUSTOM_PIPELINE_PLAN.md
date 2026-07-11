# Kế hoạch triển khai: Pipeline tùy biến 8 bước

> **TRẠNG THÁI: ĐÃ BUILD XONG (Phase 1-6).** `--pipeline custom` / `run_pipeline(flow="custom")`.
> Phase 1 (per-role model) + Phase 2 (config .env) + Phase 3 (scaffold) + Phase 4 (plan-first + codex critic) + Phase 5 (Claude-diagnose self-heal) + Phase 6 (final review + per-stage gate). Full suite 982 pass, default byte-identical.
> Chạy: `python orchestrator.py --pipeline custom --prompt "..."`.
> Nguyên tắc lõi: **opt-in `flow="custom"`** → mọi thay đổi sau `if flow=="custom":`, pipeline mặc định & test **giữ nguyên byte-identical**.
> Còn hoãn: re-code tự động đầy đủ từ final-review (hiện chỉ ghi fix-plan vào review.md); director LLM call per-file (không cần — DesignPlan đã decompose task).

## 1. Luồng mong muốn (của bạn) → ánh xạ Genius

| # | Bước mong muốn | Model | Genius hiện tại | Cần |
|---|----------------|-------|-----------------|-----|
| 1 | Input | — | prompt | — |
| 2 | **Plan TRƯỚC** | Claude Max | research→design (design cần research.md) | 🔧 code (đảo thứ tự) |
| 3 | Research | grok / gemini-pro | role researcher | ⚙️ config |
| 4 | **Codex debate** plan+info → Claude sửa | codex gpt-5.6 | debate critic↔Claude (critic=researcher) | 🔧 đổi critic→codex + nạp research |
| 5 | **Claude điều hướng** gemini code từng phần (+test) | claude-high ra lệnh, gemini-flash code | code gọi thẳng coder | 🔧 thêm lớp director |
| 6 | **Claude chẩn đoán lỗi** → gemini sửa → lặp | — | self-heal feed lỗi thẳng coder | 🔧 chèn bước claude-diagnose |
| 7 | Review cuối (Fable/codex) → Claude sửa | codex gpt-5.6 / fable | stage security/review | 🔧 review loop route→claude |
| 8 | **Duyệt sau MỖI bước** | — | gate 3 điểm (research/design/code) | 🔧 gate mịn hơn |

**Blocker duy nhất bạn cảm nhận là THẬT:** model resolve **theo BACKEND** (`provider_factory.py:183-185`), nên research & code không thể có 2 model gemini khác nhau trên cùng backend agy. → **Phase 1 fix trước.** (Hoặc: research dùng **grok** thay vì agy → tránh xung đột hoàn toàn bằng config.)

## 2. Bảy phase (opt-in, an toàn dần)

| Phase | Mục tiêu | File | Risk |
|-------|----------|------|------|
| **1. Per-role model** | `GENIUS_MODEL_<ROLE>` / `config.models.roles.<role>` resolve trong `build_backend` (4-level precedence, blank=unset → byte-identical khi không set) | `config.py`, `provider_factory.py` | 🟢 low |
| **2. Config-only .env** | Dựng nửa "routable" của luồng KHÔNG code: research→grok, plan→Claude-Max max-effort, code+test→agy/gemini-flash, review→codex gpt-5.6 | `.env`, `config.yaml` | 🟢 low |
| **3. Scaffold `flow="custom"`** | Thêm biến thể pipeline thứ 3, ban đầu **giống hệt sequential** (vỏ rỗng chứng minh default không đổi). `run_pipeline(flow=...)` + `--pipeline custom` | `orchestrator.py`, `mcp_server.py` | 🟡 med |
| **4. Plan-first + codex debate** | Chỉ trong `flow==custom`: Claude plan trước → research → codex debate plan+research → Claude revise | `orchestrator.py`, `mcp_server.py`, `mcp_tool_schemas.py` | 🔴 high |
| **5. Director + claude-diagnose** | `flow==custom`: trước mỗi code call, Claude(High) biến spec thành chỉ dẫn coder; khi fail, Claude-Max chẩn đoán từ log → chỉ dẫn gemini sửa | `orchestrator.py` | 🟡 med |
| **6. Review cuối + gate mịn** | `flow==custom`: sau code fan-out, codex/fable review → route lỗi về Claude; gate fire sau MỖI bước | `orchestrator.py`, `mcp_server.py` | 🟡 med |
| **7. Docs + doctor** | Tài liệu precedence per-role, `--pipeline custom`, doctor in model per-role | `configuration.md`, `architecture.md`, `diagnostics.py`, `CLAUDE.md` | 🟢 low |

## 3. Config-only .env (đạt ~5/8 mong muốn, ZERO code — làm ngay được sau Phase 1)

```bash
# (3) Research trên grok CLI — né xung đột model agy hoàn toàn
GENIUS_PROVIDER_RESEARCHER=grok,agy,claude
GENIUS_GROK_MODEL=grok-composer-2.5-fast     # grok đọc var RIÊNG, không phải GENIUS_MODEL_GROK

# (2) Plan/Design = Claude Max, max effort (per-role effort đã chạy được hôm nay)
GENIUS_PROVIDER_CLAUDE=claude,agy,codex
GENIUS_MODEL_CLAUDE=claude-opus-4-8
GENIUS_CLAUDE_EFFORT_CLAUDE=max              # scale low/medium/high/xhigh/max (không có 'ultra')

# (5) Code + Test = gemini-3.5-flash qua agy
GENIUS_PROVIDER_CODEX=agy,codex,claude
GENIUS_PROVIDER_TESTER=agy,codex,claude
GENIUS_MODEL_AGY=gemini-3.5-flash

# (7) Review/Audit cuối = codex gpt-5.6
GENIUS_PROVIDER_SECURITY=codex,claude,agy
GENIUS_PROVIDER_DEVOPS=codex,claude,agy
GENIUS_MODEL_CODEX=gpt-5.6
GENIUS_CODEX_EFFORT=high

# (chỉ khi muốn research = gemini-pro thay vì grok — CẦN Phase 1)
# GENIUS_MODEL_RESEARCHER=gemini-3.1-pro-preview
```

## 4. Giữ 971 test XANH (guardrails từ review — đã verify với file test)

- **Đảo plan-first** phá `test_mcp_orchestrate.py:271` (assert `stages_run==['research','design','code']`) + awaiting_stage + message hardcode `mcp_server.py:600-607`. → chỉ đảo trong nhánh `flow==custom`; nhánh else nguyên vẹn.
- **Codex làm critic** phá `test_mcp_orchestrate.py:593` (`execute_agent==['research','design']*2`) + nếu thay bằng build_agent thì **bypass mock** → vỡ nhiều test debate. → giữ debate mặc định, chỉ đổi trong custom.
- **Per-role model** đụng `test_provider_fallback.py:199-206` (khóa precedence hiện tại). → chain `or` + blank-as-unset để không-set = y hệt hôm nay; canonicalize role (bẫy grok/researcher).
- **Thread `claude_url` vào process_single_file** đổi signature mà `test_e2e.py:589/1265` + `test_milestone4_gaps.py:138-140` introspect. → thêm param **keyword-optional có default**.
- **Thêm MCP tool** phá `test_realrun_mcp.py` EXPECTED_TOOLS (thêm *property* thì OK, thêm *tool* thì phải update pin).
- **Dưới pytest** `max_debate_rounds=0` + `design_selfheal off`. → mọi loop mới (debate/review/diagnose) **phải tôn trọng toggle pytest** nếu không sẽ gọi call_api thật → treo (đúng failure mode memory đã ghi).

## 5. Quyết định cần chốt trước khi code (open questions)

1. **Research: grok hay agy/gemini-pro?** grok = zero code (khuyến nghị). agy/gemini-pro = cần Phase 1 + `GENIUS_MODEL_RESEARCHER`.
2. **Reviewer cuối: codex gpt-5.6 hay Fable?** codex = pure config (khuyến nghị). Fable (claude-fable-5) **đụng model của design role** → cần Phase 1 để tách.
3. **"Claude High điều hướng mỗi phần" = LLM call thật per-file?** (thêm latency/cost qua Semaphore(3)) hay điều phối tất định? Hiện orchestration là Python thuần, không có claude call per-subtask.
4. **Duyệt "sau MỖI bước" = per-STAGE hay per-FILE?** Per-file gate sẽ **tuần tự hóa** fan-out đang chạy song song (đánh đổi throughput lấy độ mịn).
5. **Cách dựng: 1 hàm với param `flow`** (khuyến nghị — default provably unchanged) hay `run_custom_pipeline()` clone ~600 dòng (dễ drift)?

## 5b. QUYẾT ĐỊNH ĐÃ CHỐT (user, phiên này)

1. **Research** = grok ưu tiên → **agy/gemini-3.1-pro** nếu grok fail. Chain `grok,agy` (bỏ claude vì per-role gemini-pro invalid trên claude).
2. **Reviewer** = codex gpt-5.6 ưu tiên → claude → agy nếu fail. (Fable-fallback riêng cần per-role-per-backend; hiện fallback là claude/agy hợp lệ.)
3. **"Claude điều hướng mỗi phần" = Claude chia thành task nhỏ (chính là DesignPlan decomposition đã có) + gemini code từng file.** → **KHÔNG cần per-file Claude LLM call riêng.** Phase 5 rút gọn: chỉ còn "Claude chẩn đoán lỗi" trong self-heal (bỏ director layer).
4. Duyệt "sau mỗi bước": tạm hiểu **per-STAGE** (chưa chốt per-file; per-file sẽ tuần tự hóa fan-out).
5. Dựng: **1 hàm với param `flow`** (khuyến nghị).

### ⚠️ CAVEAT quan trọng (phát hiện khi verify): per-role model + fallback
Per-role model (`GENIUS_MODEL_ROLE_<ROLE>`) áp cho **MỌI backend trong chuỗi fallback**, mà model lại đặc thù backend → nếu role có fallback sang backend khác, model per-role sẽ **invalid** ở đó (vd design opus → agy fallback nhận `claude-opus-4-8` invalid). **Quy tắc:**
- Dùng **per-BACKEND** (`GENIUS_MODEL_<BACKEND>`) khi role có fallback đa-backend (design→claude-opus, review→codex-gpt5.6): mọi backend dùng model của chính nó, fallback hợp lệ.
- Dùng **per-ROLE** (`GENIUS_MODEL_ROLE_<ROLE>`) CHỈ khi chuỗi ở lại backend tương thích, hoặc để tách 2 role trên cùng 1 backend (research agy-fallback=gemini-pro vs code agy=gemini-flash — chain research là grok,agy nên không có claude để lỗi).
- Nâng cấp tương lai nếu cần "model khác nhau mỗi backend mỗi role": thêm `GENIUS_MODEL_ROLE_<ROLE>_<BACKEND>`.

## 6. Slice đầu tiên nên làm (khuyến nghị của review)

**Phase 1 + Phase 2** = per-role model (low risk) + config .env. Đạt ngay: research=grok, plan=Claude-Max, code/test=gemini-flash, review=codex. **Zero rủi ro suite.** Chứng minh plumbing trước khi đụng orchestration (Phase 3-6).
