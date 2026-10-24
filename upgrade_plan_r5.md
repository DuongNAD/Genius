# Kế hoạch R5 — Eval Flywheel (chấm điểm + chống regression + sửa phẫu thuật) — nhánh `win`

> 2026-07-10. Nguồn cảm hứng: `google/agents-cli` (lớp eval: dataset→generate→grade→analyze→optimize→compare, LLM-as-judge rubric thích ứng). Mang **pattern**, KHÔNG mang phụ thuộc Google Cloud/ADK. Local-first giữ nguyên.
>
> Gói R5 = A1 (tầng chấm điểm) + A2 (rubric judge) + A3 (compare/gate chống regression) + A4 (code preservation). A5 (dataset synthesis) và tích hợp B1 (agents-cli deploy backend) hoãn R6.

## Nguyên tắc bất biến (đọc trước khi code)
Genius đã có sẵn ~80% hạ tầng eval — chỉ thiếu tầng **grade**. R5 tái dùng, KHÔNG dựng lại:
- **Traces đã có**: `orchestrator.save_raw_response(project_dir, name, content)` (orchestrator.py:125) ghi `projects/<slug>/logs/raw/<name>.md` mỗi stage/attempt. Đây chính là "traces" của agents-cli.
- **Judge in-process đã có mẫu**: MCP `debate`/`review`/`code_graph` dựng agent qua `agent_factory.build_agent(...)` in-process, KHÔNG cần service/port. Evaluator R5 đi theo đúng lối này.
- **Điểm nối self-heal đã có**: `codex_reviewer` compose feedback qua `### Linter Findings (flake8)` + `### Pytest Logs` (codex_reviewer.py:265). Eval score chèn thêm 1 section MỚI cạnh đó, KHÔNG sửa 2 chuỗi ghim này.
- **Cổng đã có**: `run_pipeline(stage_gate=...)` (orchestrator.py:1695) — callback async sau research/design/code. Eval-gate đi cùng cơ chế.
- **Convention off-under-pytest**: mọi tính năng mới mặc định TẮT dưới pytest (mirror `max_debate_rounds=0` và `design_selfheal_enabled()` orchestrator.py:140) để 784 test mock-cố-định giữ xanh.

## Quyết định kiến trúc chốt
- **KHÔNG thêm role thứ 7 vào `agent_factory.AGENT_CLASSES` trong R5.** Thêm role sẽ lan sang `skill_app.ROLE_MAP` + `worker.ROLE_AGENT_MAP` (derive) và đòi 1 port mới → phá hợp đồng "ports frozen" (architecture.md). Judge = persona in-process: `build_agent("claude", stateless=True, ...)` + rubric prompt, giống `_run_review`/`_run_debate`. Promote Evaluator lên FastAPI role trên **8007** để dành R6.
- **KHÔNG kéo dep nặng / GCP / ADK.** Metrics engine = Python thuần + gọi provider sẵn có.
- **stdout thuần JSON-RPC** cho tool `eval`; log ra stderr (như mọi tool MCP).

---

## Wave 1 — Metrics engine + grader (nền A1)
Thư mục MỚI `ag_core/eval/`:
- `metrics.py` — định nghĩa metric. Hai loại (mirror agents-cli):
  - **CodeExecutionMetric** (deterministic, Python thuần): `pytest_pass_rate`, `flake8_clean` — Genius ĐÃ có tín hiệu này từ self-heal, chỉ bọc lại thành metric.
  - **LLMMetric** (judge): `prompt_template` + placeholder `{prompt}` / `{response}` / `{trace}`; trả JSON `{score: 1-5, explanation}`. Rubric **thích ứng** (judge tự sinh tiêu chí từ trace thật, không so khớp cứng).
  - Built-in judge metric mặc định: `task_success` (output có thỏa prompt gốc?), `grounding` (code/design bám research + workspace, không bịa API?), `design_quality`, `final_response_quality`.
- `grader.py` — `grade(workspace, metrics_to_run, judge) -> ScoreSet`: đọc `logs/raw/*.md` + artifact gốc (research/design/review.md), chạy metric. Phần deterministic qua `asyncio.to_thread`; phần judge qua agent in-process. Trả dict `{metrics: {name: {score, explanation}}, overall, generated_at? }`.
- `config`: thêm section `eval:` vào `config.yaml` qua `ag_core/config.py` (`metrics_to_run`, `custom_metrics`, `thresholds`) — mirror `eval_config.yaml` của agents-cli. Không đổi schema config cũ (thêm field optional).
- **Ràng buộc**: grader KHÔNG ghi file trong Wave 1 (chỉ trả object); judge dùng bundle stateless (`output_file="None", use_memory=False`). Timestamp lấy từ tham số truyền vào (KHÔNG gọi `time.time()` ở tầng thuần để dễ test).
- Test MỚI `tests/test_eval_metrics.py`: metric deterministic chạy luôn; case judge dùng provider giả (mock) trả JSON cố định.

## Wave 2 — MCP tool `eval` (tool thứ 18, surface của A1)
- `mcp_server.TOOLS`: thêm schema `eval` — `{op: grade|compare|list_metrics, workspace?, metrics?, baseline?}` (mẫu y `code_graph` mcp_server.py:573).
- `dispatch_tool`: thêm `if name == "eval": return await _run_eval(arguments)` (cạnh `code_graph` mcp_server.py:1129).
- `_run_eval(arguments)`: mirror `_run_code_graph` — validate op, scan/grade off event loop, trả `json.dumps({...})`. **KHÔNG ghi file** (như `review`/`code_graph`), giữ stdout thuần.
- **Pins phải cập nhật ĐÚNG** (nếu quên → đỏ ngay):
  - `tests/test_realrun_mcp.py::EXPECTED_TOOLS`: thêm `"eval"` (17→18).
  - `.claude/rules/architecture.md`: "17 tools" → 18 + mô tả `eval`.
  - `CLAUDE.md`: "17 tools" (2 chỗ: mô tả mcp_server + lệnh) → 18.
- Test MỚI `tests/test_mcp_eval.py`: gọi `dispatch_tool("eval", ...)` trực tiếp, workspace tmp có sẵn vài trace giả.

## Wave 3 — Rubric judge cho design (A2, thuần additive)
Debate hiện là nhị phân `[APPROVED]`; các chuỗi prompt critic + `[APPROVED]` **bị test ghim** (orchestrator.py:1932/1965, mcp `_run_debate`). Vì vậy A2 KHÔNG viết lại debate.
- Đóng gói "rubric design judge" thành 1 metric trong Wave 1 (`design_quality`) chấm điểm thích ứng trên `design.md`.
- Phơi qua tool `eval` (op=grade) và (opt-in) như tín hiệu bổ sung khi debate kết thúc — `[APPROVED]` giữ nguyên, điểm rubric chỉ là dữ liệu thêm.
- **Ràng buộc**: KHÔNG đụng chuỗi critic prompt / `[APPROVED]` / prompt refine (orchestrator.py:1975). Chỉ thêm, không sửa.

## Wave 4 — Compare + gate chống regression (A3)
- `ag_core/eval/compare.py` — `compare(baseline: ScoreSet, current: ScoreSet) -> Diff`: chênh lệch per-metric, cờ regression (điểm tụt quá `thresholds`).
- Lưu điểm sau mỗi orchestrate: ghi `projects/<slug>/logs/eval/score-<ts>.json` + `baseline.json` (dùng `save_raw_response`-style: try/except non-fatal, trong workspace của job, KHÔNG vỡ run). Đây là chỗ DUY NHẤT R5 ghi file, và nằm trong workspace job.
- Gate: thêm tham số opt-in `eval_gate` (async, mirror `stage_gate`) vào `run_pipeline`, chạy grade+compare ở cuối, cảnh báo/chặn nếu regression. **Mặc định OFF dưới pytest** (mirror `max_debate_rounds`); production bật qua config/env `GENIUS_EVAL_GATE`.
- MCP: `eval` op=compare + `orchestrate` nhận `eval_gate: true` (optional, chỉ pipeline sequential — như `require_approval`).
- Test MỚI `tests/test_eval_compare.py`: baseline vs current, xác nhận cờ regression đúng chiều.

## Wave 5 — Code preservation / sửa phẫu thuật (A4)
- Thêm khối "surgical-edit" vào `SYSTEM_PROMPT` của `CodexReviewerAgent` khi ở **chế độ fix** (attempt>1 trong self-heal): "chỉ sửa vùng được chỉ định; giữ nguyên phần còn lại từng byte kể cả comment/format/giá trị config; TUYỆT ĐỐI không đổi model name/API key/config ngoài phạm vi; kiểm tra vùng xung quanh còn nguyên trước khi kết thúc" (bê nguyên nguyên tắc từ skill workflow của agents-cli).
- **Ràng buộc bytes-identical**: chuỗi `### Linter Findings (flake8)` + `### Pytest Logs` (codex_reviewer.py:265) là pin — chèn khối preservation thành **section MỚI riêng**, KHÔNG chèn vào giữa 2 chuỗi này. Bật opt-in qua `GENIUS_SURGICAL_EDITS` (mặc định off để không đổi hành vi/pin hiện tại).
- Test MỚI trong `tests/test_bug_fixes.py` hoặc `tests/test_surgical_edit.py`: bật cờ → prompt fix chứa khối preservation; tắt cờ → prompt bytes-identical như cũ.

## Wave 6 — Docs + gate + phát hành
- `.claude/rules/architecture.md` (18 tools + `ag_core/eval/` + note Evaluator in-process), `.claude/rules/testing.md` (eval-gate/surgical/rubric OFF dưới pytest), `.claude/rules/configuration.md` (section `eval:` + `GENIUS_EVAL_GATE`/`GENIUS_SURGICAL_EDITS`), `CLAUDE.md` (đếm tool + lệnh).
- Gate: `py -m pytest` (1 process duy nhất) + flake8 + black. Commit `win` → push → ff `main` → merge `mac` (giữ dòng uvloop) → theo dõi CI 3 nhánh.

---

## Bảng đối chiếu pattern agents-cli → Genius (để nhớ nguồn gốc)
| agents-cli | Genius R5 |
|---|---|
| traces (`eval generate`) | `save_raw_response` → `logs/raw/*.md` (đã có) |
| `eval grade` + metrics | `ag_core/eval/grader.py` + `metrics.py` (W1) |
| built-in metrics (task_success, tool_use_quality, hallucination, safety) | task_success/grounding/design_quality/final_response_quality + SecurityAgent làm safety-metric |
| LLMMetric (prompt_template + judge) | `LLMMetric` in-process judge qua `build_agent("claude")` |
| CodeExecutionMetric | pytest_pass_rate / flake8_clean (bọc tín hiệu self-heal sẵn có) |
| `eval compare` (gate) | `compare.py` + `eval_gate` param (W4) |
| adaptive rubric | `design_quality` judge (W3) |
| `eval dataset synthesize` (user simulator) | **HOÃN R6** |
| `eval optimize` (GEPA) | **HOÃN R6** |
| deploy GCP/ADK | ngoài phạm vi (Genius local-first); B1 opt-in R6 |

## Rủi ro / bẫy đã lường
- **Đừng thêm role vào AGENT_CLASSES** (ripple sang 2 bảng derive + đòi port) — judge in-process.
- **Đừng sửa chuỗi ghim**: critic prompt/`[APPROVED]`, `### Linter Findings`/`### Pytest Logs`, file mặc định — chỉ thêm section mới.
- **Mọi thứ mới OFF dưới pytest** trừ metric deterministic được test bằng mock provider.
- **stdout thuần JSON-RPC** cho tool `eval`.
- Judge tốn token → mặc định `metrics_to_run` gọn (task_success + 1 deterministic); metric đắt là opt-in.

## Đã triển khai (2026-07-10) — 856 passed, 2 skipped; flake8 + black sạch
- **Wave 1 ✅** `ag_core/eval/` MỚI: `metrics.py` (2 loại metric + `BUILTIN_METRICS` 7 metric + `parse_verdict`), `grader.py` (`collect_case`/`grade_case`/`grade`), `judge.py` (`default_judge` in-process qua `make_provider("claude")` — KHÔNG thêm role/port), `compare.py`.
- **Wave 2 ✅** MCP tool `eval` (thứ 18): handler `_run_eval` (mẫu `_run_code_graph`, read-only, JSON out, no file writes), schema + dispatch branch; pin `EXPECTED_TOOLS` 17→18. Test `test_mcp_eval.py`.
- **Wave 3 ✅ (gộp)** rubric design judge = metric `design_quality` (LLM-judge thích ứng) trong Wave 1 — KHÔNG đụng debate/`[APPROVED]` (giữ pin).
- **Wave 4 ✅** `ag_core/eval/gate.py::run_eval_gate` (grade → ghi `logs/eval/score-<ts>.json` → so `baseline.json`, giữ baseline khi regressed) + wire `orchestrator._maybe_run_eval_gate` trước `return devops_content`; guard `eval_gate_enabled()` (env `GENIUS_EVAL_GATE`, **off under pytest**), non-fatal. Test `test_eval_gate.py`.
- **Wave 5 ✅** `codex_reviewer.SURGICAL_EDIT_GUIDANCE` + `_surgical_edits_enabled()` (env `GENIUS_SURGICAL_EDITS`); chèn vào system prompt CHỈ khi generation_mode + flag bật → mặc định byte-identical. Test `test_surgical_edits.py`.
- **Wave 6 ✅** docs: architecture.md (18 tools + bullet `ag_core/eval/`), configuration.md (2 env mới), testing.md (eval off-under-pytest), CLAUDE.md (18 tools).

### Ghi chú thực tế (khác/bổ sung so với plan gốc)
- **Xung đột tên `compare`**: `ag_core/eval/__init__.py` re-export hàm `compare`, che submodule `ag_core.eval.compare`. Trong `mcp_server._run_eval` phải import thẳng `from ag_core.eval.compare import compare as compare_grades`, KHÔNG `from ag_core.eval import compare`.
- **Judge độc lập agent-class**: dùng `make_provider` + `send_prompt(system=JUDGE_SYSTEM_PROMPT)` thay vì `build_agent` — tránh kế thừa SYSTEM_PROMPT/history/memory của ClaudeArchitectAgent làm nhiễu điểm.
- **grade mặc định chỉ metric deterministic** (`artifacts_present`/`design_wellformed`/`code_syntax_valid`) → chạy offline, không cần CLI/judge → test MCP + gate chạy được dưới pytest không cần mock provider. LLM-judge opt-in qua `metrics`.
- **Gate giữ baseline khi regressed** (không ghi đè) để regression còn hiện ở lần so kế tiếp cho tới khi sửa.
- Metric an toàn: `code_syntax_valid` chỉ `ast.parse` (KHÔNG execute) — không mở bề mặt RCE mà `STATELESS_KWARGS` đang chặn.

## Hoãn tiếp (R6)
- A5 — dataset synthesis / LLM-backed user simulator (multi-turn eval).
- Promote Evaluator → FastAPI role port 8007 + grading phân tán qua hub.
- `eval optimize` kiểu GEPA (tune prompt tự động — đắt).
- Tracing OpenTelemetry có cấu trúc (hiện raw log + dashboard là đủ).
- B1 — agents-cli làm deploy backend opt-in cho DevOps agent (chỉ khi cần đẩy ADK lên GCP), theo pattern NotebookLM (env-gated, opt-in-only).
- B2 — Genius lái `agents-cli` qua provider `agy` (Antigravity là coding-agent agents-cli hỗ trợ).
