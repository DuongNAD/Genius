# Kế hoạch R4 — CodexGraph qua MCP + trả nợ refactor + cAST + tree-sitter (nhánh `win`)

> 2026-07-04. User chọn cả 4 mục hoãn từ R2/R3. R1/R2/R3 đã nằm trên cả 3 nhánh (win/main = fe892d4, mac = 4cfe5aa).

## Wave 1 — Trả nợ refactor 6 agent + hợp nhất factory
**Hiện trạng khảo sát:** 6 agent trùng ~70% run() (resolve prompt → slash routing → scan → memory block → compose → send → history/store/log/write). 4 điểm khởi tạo (skill_app.build_agent, mcp_server.execute_agent, mcp_server._run_review, worker.execute_task) với 3 bảng role→class trùng lặp.

- `ag_core/interfaces/base_agent.py`: thêm class-knobs (`DEFAULT_TASK`, `SLASH_PREFIXES`, `SYSTEM_PROMPT`, `USES_MEMORY`, `DEFAULT_OUTPUT_FILE`) + helpers (`_resolve_user_prompt`, `_route_slash_command`, `_memory_context_block`, `_compose_full_prompt`, `_log_usage`, `_store_run_memory`) + `_run_standard` (template method). `run` vẫn abstract.
- 4 agent "đơn giản" (researcher/architect/security/devops): `run()` = 1 dòng delegate. Codex/Tester: giữ vòng self-heal riêng, dùng chung helpers phần đầu.
- **Ràng buộc bytes-identical** (test ghim): chuỗi rewrite slash-command; `full_prompt` positional cho `send_prompt`; khối `--- Relevant Historical Memory Context ---`; text memory `Prompt: {p}\nResponse: {c}`; file mặc định research.md/design.md/review.md/test_generated.py; `### Linter Findings (flake8)` / `### Pytest Logs`; `__test__ = False` trên Tester/Security/DevOps; `max_retries` ctor kwarg; signature `run(prompt=None, context_data=None)`.
- `ag_core/agent_factory.py` MỚI: bảng chuẩn `AGENT_CLASSES` (6 role) + alias dài (`claude_architect`→`claude`...) + `build_agent(role, *, config, provider, default_chain, stateless, agent_cls, **kwargs)` với bundle stateless `{output_file="None", use_memory=False, stateless=True}`.
- Delegate: `skill_app.build_agent` (giữ symbol + signature, ROLE_MAP giữ shape cũ derive từ factory); `worker.execute_task` (ROLE_AGENT_MAP giữ shape (mod, cls, factory_role) derive từ factory — id dài vẫn nhận y nguyên); `mcp_server.execute_agent`/`_run_review` (GIỮ `globals()[cls_name]` để `patch("mcp_server.DevOpsAgent")` còn hoạt động — truyền qua `agent_cls=`).
- Test mới `test_agent_factory.py`; suite cũ phải xanh nguyên.

## Wave 2 — Đồ thị code truy vấn được + tree-sitter đa ngôn ngữ
- `ag_core/scanner/code_parse.py` MỚI: `parse_source(path, source)` → defs (name/kind/line/signature-line), refs, imports. Python = ast (giàu hơn `_parse_py` nhưng KHÔNG đụng repo_graph.py hiện có). JS/TS/TSX/Go = tree-sitter qua `tree-sitter-language-pack`, guard kiểu `find_spec` (mirror vector_store.py) → thiếu dep = câm lặng bỏ qua, KHÔNG lỗi.
- `ag_core/scanner/graph_index.py` MỚI: `RepoIndex.build(scanned)` + queries: `find_definition`, `find_references`, `importers_of`, `imports_of`, `file_skeleton`, `repo_map(budget)` (tái dùng `_pagerank`/`_skeleton`/`_module_names` từ repo_graph — không vòng import: repo_graph KHÔNG import graph_index).
- repo_graph._build: thêm cạnh cho file non-Python qua code_parse (chỉ khi tree-sitter có mặt; thiếu dep → hành vi y hệt hiện tại).
- requirements.txt: pin `tree-sitter` + `tree-sitter-language-pack` (kiểm wheel cp311 win+mac trên PyPI trước khi pin). uvloop line của mac giữ nguyên khi merge.
- Test: `test_code_graph_index.py` (py thuần luôn chạy; case JS/Go skipif thiếu tree-sitter).

## Wave 3 — MCP tool `code_graph` (CodexGraph-lite, không cần Neo4j)
- Tool thứ 14 trong `mcp_server.TOOLS`: `code_graph` — args `{op: map|definition|references|importers|imports|skeleton, workspace?, symbol?, file?, budget?, task?}`; handler `_run_code_graph` (scan qua `asyncio.to_thread`, build RepoIndex, trả JSON; op=map dùng ranking + skeleton theo budget). Không ghi file, stdout giữ pure JSON-RPC.
- Cập nhật ĐÚNG các chỗ ghim: `tests/test_realrun_mcp.py::EXPECTED_TOOLS` (13→14), `.claude/rules/architecture.md` câu "13 tools" → 14 + mô tả. Test mới `test_mcp_code_graph.py` (gọi `dispatch_tool` trực tiếp, workspace tmp).

## Wave 4 — cAST chunking cho ProjectChunker
- `ProjectChunker.split_file(filepath, content) -> List[str]` MỚI: split-then-merge theo AST (paper cAST 2025) — chia theo node top-level (đệ quy vào class quá khổ), merge sibling nhỏ, slice theo dòng (lossless); non-Python/SyntaxError → cửa sổ dòng cố định.
- `chunk_files(files, split_oversized=False)`: cờ opt-in — False giữ NGUYÊN hành vi ghim (file quá khổ được cô lập nguyên khối); True → file py quá khổ tách thành `path#chunkN`. KHÔNG đổi `format_file_payload`/`count_tokens`/ctor.
- Test mới trong `test_cast_chunking.py`.

## Wave 5 — Docs + gate + phát hành
- `.claude/rules/architecture.md` (14 tools), `.claude/rules/configuration.md` (bullet tree-sitter optional + code_graph), upgrade_plan giữ local.
- Gate: `py -m pytest` (1 process duy nhất) + flake8 + black. Commit `win` → push → ff `main` → merge `mac` (giữ dòng uvloop) → theo dõi CI 3 nhánh qua API GitHub công khai.

## Ghi chú thực tế khi triển khai (khác plan gốc)
- **KHÔNG dùng `tree-sitter-language-pack`**: bản 1.x đã thay binding py-tree-sitter cổ điển bằng PyO3 API không tài liệu (`builtins.Tree`, `kind()`/`start_position()`, parse nhận str) — cả 1.10.9 lẫn 1.12.2. Chuyển sang grammar chính chủ: `tree-sitter==0.25.2` + `tree-sitter-javascript==0.25.0` + `tree-sitter-typescript==0.23.2` + `tree-sitter-go==0.25.0` (wheel cp311/abi3 đủ win+mac).
- `codex_reviewer` phải GIỮ import module-level `log_transaction` và gọi trực tiếp (không qua `_log_usage`): `tests/test_bug_fixes.py` patch `ag_core.agents.codex_reviewer.log_transaction` để chặn side effect.
- Phát hiện + vá bug tiềm ẩn CÓ SẴN (không do R4, xác minh bằng stash trên fe892d4): `_redirect_all_logging_to_stderr` crash `AttributeError` khi một logger bị gắn handler có property `stream` read-only (vd `_StderrHandler` lastResort) → MCP stdio chết lúc boot; giờ skip qua try/except. Chỉ lộ khi chạy subset test khác thứ tự alphabet.
- Wave 1 còn phát hiện: factory "3 nơi" thực chất là 4 điểm khởi tạo / 3 bảng role→class (skill_app.ROLE_MAP, worker.ROLE_AGENT_MAP, mcp TOOL_AGENTS); hai bảng đầu giờ derive từ `agent_factory.AGENT_CLASSES`, TOOL_AGENTS giữ nguyên (test ghim shape 3-tuple + patch qua globals()).

## Hoãn tiếp (R5)
- CodexGraph full graph-DB (Neo4j/Kuzu) + agent tự viết query — khi cần repo cực lớn.
- pytest-xdist (cần cô lập DB/port per-worker); template-hoá `__init__` agent (giá trị thấp).
- Rust/Java cho code_parse; cAST wiring vào memory/RAG path.
