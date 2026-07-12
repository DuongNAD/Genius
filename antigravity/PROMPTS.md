# Bộ prompt chuẩn cho /genius (Antigravity → Genius pipeline)

Mẫu prompt theo từng cấp độ để điền vào `/genius <prompt>` (hoặc gọi thẳng
`genius_orchestrate`). Đúc kết từ các run thật: prompt càng nêu rõ **hành vi
chính xác + tiêu chí nghiệm thu**, bản kế hoạch của Claude càng chuẩn và coder
gần như không thể hiểu sai.

---

## ⭐ MẪU VÀNG (bản tốt nhất — dùng khi không chắc chọn gì)

> **Bạn không cần tự điền:** workflow `/genius` đã được dạy tự chuyển yêu cầu
> ngôn-ngữ-tự-nhiên (tiếng Việt cũng được) vào đúng mẫu này trước khi gọi
> Genius — cứ gõ `/genius <mô tả tự nhiên>`. Tự điền chỉ khi muốn kiểm soát
> từng chữ.
>
> **Workflow có 2 mode và tự chọn theo tin nhắn của bạn:**
> - **BUILD** — bạn mô tả dự án/tính năng mới → mẫu vàng dưới đây → pipeline
>   build hoàn thiện.
> - **DEBUG** — bạn tự test thấy sai, dán lỗi, hay nói "chưa đúng ý" → KHÔNG
>   chạy lại pipeline; vòng sửa nhanh bằng `gdbg_review`/`gdbg_code` với
>   **mẫu FIX** (xem "Cấp 5 — MODE DEBUG" bên dưới).

**Dạng compact — tiện ích nhỏ (≤3 file, plan nhanh, giữ DƯỚI 600 ký tự):**

```
Build a tiny <language> utility '<name>': <goal in one sentence>, exposing
<exact public API signature(s)>, plus <CLI entry if any>. AT MOST <N> small
files: <list>. Standard library only. Done when: <command> exits 0;
<missing-input case> prints usage to stderr and exits 2; <bad-input case>
prints an error and exits 1.
```

**Dạng detailed — mọi thứ lớn hơn (plan chạy effort max):**

```
Build <what> '<name>': <one-sentence goal>.
FILES (at most <N>): <product files only — KHÔNG liệt kê file test>.
BEHAVIOR (exact): <signatures>; <2-3 ví dụ input -> output cụ thể>;
  <error contract: stderr, exit codes>; <CHỐT semantics edge-case —
  "ASCII-only" hay "Unicode casefold", timezone, float tolerance...>.
CONSTRAINTS: <stdlib-only | deps được phép>, <ngôn ngữ/phiên bản>, <no network>.
ACCEPTANCE (done when): <kiểm chứng quan sát được, kèm lệnh + exit code>.
NON-GOALS: <những gì KHÔNG làm>.
ORIGINAL REQUEST (verbatim): "<yêu cầu gốc của bạn, nguyên văn>"
```

Vì sao mẫu này "vàng": mỗi mục khớp thẳng vào một design-quality gate của
architect (BEHAVIOR/semantics → gate 1, FILES cap → gate 2, ACCEPTANCE → gate
3, ORIGINAL REQUEST → gate 4) — Opus max không phải đoán, chỉ phải thiết kế.

**Quy tắc chung (đọc trước khi điền):**

- Viết phần prompt bằng **tiếng Anh** — các agent cho kết quả ổn định nhất.
- **KHÔNG liệt kê file test trong FILES** — pipeline tự sinh `tests/` + chạy
  chúng + security audit + final review cho bạn. File cap trong prompt chỉ áp
  cho file sản phẩm.
- **Độ dài prompt quyết định effort của stage plan** (adaptive effort đang
  bật): prompt **dưới ~600 ký tự** → plan chạy effort `high` (nhanh); prompt
  dài/chi tiết → plan chạy `max` như config (chậm hơn nhưng xứng đáng với spec
  lớn). Muốn nới ngưỡng: `GENIUS_ADAPTIVE_EFFORT_THRESHOLD` trong
  `mcp_config.json`. `@deep` luôn thắng heuristic này.
- Theo dõi tiến độ: `current_stage` trong `genius_orchestrate_status` cho biết
  pipeline đang làm gì; server cũng **push** thông báo `stage_done`/`status`
  qua MCP log notifications (logger `genius.orchestrate`) nên client hiển thị
  log sẽ thấy tiến độ realtime không cần đợi poll.
- Thêm `@deep` vào đầu prompt khi bài toán khó/nhiều ràng buộc (đẩy effort của
  agent lên cao nhất). Thêm `require_approval: true` khi bạn muốn duyệt từng
  stage (research → design → code) trước khi chạy tiếp.
- Kết quả nằm trong `workspace` mà status trả về
  (`.genius_jobs/<job_id>/projects/<slug>/`); job sống sót qua restart nhờ
  `job.json` (status `interrupted` = server restart giữa chừng, artifact các
  stage đã xong vẫn còn nguyên).

---

## Cấp 0 — Hỏi/tra cứu, KHÔNG build

Đừng dùng orchestrate. Gọi thẳng tool đơn:
- `genius_research` — khảo sát/so sánh công nghệ.
- `genius_review` — dán code vào để được review (không ghi file).
- `genius_code_graph` — hỏi cấu trúc một repo có sẵn.

```
Compare <option A> vs <option B> for <use case>. Criteria: <maturity,
performance, licensing, ecosystem>. Recommend one with reasons.
```

---

## Cấp 1 — Snippet / một hàm / một file nhỏ (≤ 1 phút thiết kế)

Việc quá nhỏ cho cả pipeline → dùng `genius_code` (một agent, nhanh):

```
Write a single Python file <name>.py that <does X>.
Requirements: <input/output exactly>, <edge cases>, standard library only,
include docstring examples runnable with `python -m doctest`.
```

Chỉ dùng `/genius` (orchestrate) ở cấp này khi bạn muốn kèm test tự sinh +
security audit + final review.

---

## Cấp 2 — Tiện ích nhỏ, 1–3 file sản phẩm (mặc định nên dùng)

Hai biến thể, chọn theo mức chi tiết bạn cần:

- **Compact (khuyên dùng cho tiện ích nhỏ)** — một đoạn văn gọn **dưới 600 ký
  tự**, đủ: goal + public API + cap file + "Done when". Plan chạy effort
  `high` → nhanh (đã đo: research+design xong sau ~1 phút thay vì ~3 phút).
- **Detailed** — dùng template đầy đủ bên dưới khi hành vi có nhiều edge case
  cần chốt chính xác; prompt sẽ vượt ngưỡng và plan chạy `max` (chậm hơn
  nhưng spec càng chặt thì càng đáng).

Template detailed (điền vào `<...>`, xóa dòng không cần):

```
Build a small <language> utility '<name>': <one-sentence goal>.

FILES (at most <N>): <file1>, <file2 — e.g. README.md>. Do not add packaging,
setup files, or a separate tests file.

BEHAVIOR (exact):
- <public API: function/CLI signature, arguments, return/exit shape>
- <semantics that must hold, with 2–3 concrete input → output examples>
- <error behavior: what goes to stderr, which exit codes>

CONSTRAINTS: standard library only | Python <ver>+ | no network | <style/limits>.

ACCEPTANCE (done when):
- <observable check 1 — e.g. `python -m doctest <file>` exits 0>
- <observable check 2 — e.g. `python <name>.py <file>` prints {...} and exits 0>
- <negative check — e.g. missing argument prints usage to stderr, exit 2>

NON-GOALS: <what must NOT be built — flags, configs, features to skip>.
```

Ví dụ compact đã điền (cả hai đều là run thật, eval 5/5; txtstats chạy 17
phút khi plan còn ở effort max, linestat chạy **~6 phút** với adaptive
effort — research+design xong sau ~75 giây):

```
Build a tiny Python utility project 'txtstats': a single module txtstats.py
exposing count_stats(text: str) -> dict with keys lines, words, chars, plus a
main() that reads a file path from sys.argv and prints the stats as JSON.
Keep the design to AT MOST 2 small files (txtstats.py and README.md).
No external dependencies.
```

```
Build a tiny Python utility 'linestat': a single module linestat.py exposing
top_words(text: str, n: int = 3) -> list[tuple[str, int]] returning the n most
frequent lowercase words (ties broken alphabetically), plus a main() reading a
file path and optional n from sys.argv and printing one 'word count' line per
result. AT MOST 2 small files: linestat.py and README.md. Standard library
only. Done when: `python -m doctest linestat.py` exits 0; a missing path
prints usage to stderr and exits 2; an unreadable file prints an error and
exits 1.
```

Mẹo: cap số file chặt (như trên) khiến kiến trúc sư tự chuyển test sang
doctest nhúng — gọn và vẫn kiểm chứng được; câu "Done when" liệt kê được cả
exit code là thứ giúp coder không hiểu sai hành vi lỗi.

---

## Cấp 3 — Ứng dụng vừa (API/CLI nhiều module, có cấu hình)

Dùng `@deep`, cân nhắc `require_approval: true` để duyệt design trước khi code.

```
@deep Build a <language> <application type> '<name>': <goal>.

MODULES (at most <N> product files):
- <module1.py — responsibility>
- <module2.py — responsibility>
- <config: how it is provided — env vars / a yaml file>

DATA MODEL: <entities + fields + invariants, or the API endpoints with
request/response JSON shapes and status codes>.

BEHAVIOR (exact): <happy path walkthrough>; <2–3 worked examples>;
<concurrency/ordering guarantees if any>.

ERROR HANDLING: <taxonomy of failures → user-visible message/exit code/HTTP
status; what is logged where>.

CONSTRAINTS: <deps allowed (name exact packages) or stdlib-only>, <version>,
<performance budget — e.g. handles a 100MB input under 10s>, no secrets in
code, read config only from <place>.

ACCEPTANCE (done when): <endpoint/command level checks with exact expected
output>; <negative cases>; all generated tests pass.

NON-GOALS: <auth? UI? persistence? — say explicitly which are out>.
```

---

## Cấp 4 — Dự án lớn / gần production

Đừng nhét cả dự án vào một prompt. Chia theo **milestone, mỗi milestone một
lần /genius** (mỗi run là một workspace độc lập), milestone sau dán "CONTEXT"
là kết quả milestone trước:

```
@deep Build milestone <k> of project '<name>': <goal of THIS milestone only>.

CONTEXT (already built in a previous run — do not rebuild, design to be
compatible): <paste the public API/signatures/file list from the previous
milestone's design.md or README>.

MODULES / DATA MODEL / BEHAVIOR / ERROR HANDLING / CONSTRAINTS / ACCEPTANCE /
NON-GOALS: (như cấp 3)

SECURITY REQUIREMENTS: <input validation rules, authn/z model, secret
handling, injection surfaces to defend>.
```

Luôn bật `require_approval: true` ở cấp này: duyệt research + design trước khi
đốt thời gian vào code; `genius_orchestrate_reject` với `reason` nếu kế hoạch
lệch, pipeline sẽ dừng để bạn chỉnh prompt.

---

## Cấp 5 — MODE DEBUG: bạn tự test thấy sai / chưa đúng ý

Đây là mode thứ hai của `/genius` (workflow tự nhận diện khi bạn dán lỗi,
nói "chưa đúng ý/sai rồi/sửa lại", hay nêu tên file/job đã build). KHÔNG chạy
lại cả pipeline — vòng sửa nhanh bằng tool đơn, ưu tiên server debug
(`gdbg_*`, backend codex gpt-5.6-sol):

1. Đọc file hiện tại từ `workspace` của job (hoặc path bạn nêu).
2. Chưa rõ nguyên nhân → `gdbg_review` (dán file + bằng chứng lỗi).
3. Sửa bằng `gdbg_code` với **mẫu FIX** (mẫu vàng của mode debug):

   ```
   Fix the file '<path>' so that <desired behavior>, WITHOUT changing its
   public API or unrelated behavior.
   OBSERVED: <lỗi/traceback/output sai, nguyên văn>.
   EXPECTED: <hành vi đúng, kèm 1 ví dụ input -> output>.
   EVIDENCE: <lệnh hoặc test đã fail + output của nó>.
   Return the COMPLETE corrected file content.

   <dán toàn bộ nội dung file hiện tại>
   ```

4. Áp file trả về, chạy lại đúng lệnh/test đã fail, báo diff + kết quả. Muốn
   khóa hồi quy: `gdbg_unit_test` sinh test cho hành vi vừa sửa.
5. Chỉ **leo thang về mode BUILD** (orchestrate mới, golden prompt + mục
   CONTEXT mô tả những gì đã có, "must stay drop-in compatible") khi cái sai
   đòi thiết kế lại nhiều file hoặc đổi public contract.

Mẹo viết OBSERVED/EXPECTED: dán nguyên văn, đừng diễn giải — codex sửa trúng
nhất khi thấy đúng bằng chứng bạn thấy.

---

## Muốn bản plan TỐT NHẤT từ Opus (effort max)?

Architect hiện bị ràng buộc bởi **5 design-quality gate** (trong system
prompt) và debate critic soát đúng 5 lỗi đó: (1) nhất quán contract ↔ thuật
toán (phải chốt semantics edge-case: Unicode casing/normalization, float,
timezone...), (2) layout tối giản (không `src/`/`conftest.py`/packaging cho
tiện ích nhỏ), (3) mọi capability đã tuyên bố phải có test khóa — không có
test "optional" cho hành vi thuộc contract, (4) tách "Assumptions" (điều
architect tự thêm) khỏi yêu cầu gốc của bạn, (5) không lặp nội dung. Phía
prompt, để vắt kiệt chất lượng:

1. **Cho plan chạy ở max**: dùng biến thể Detailed (prompt ≥ 600 ký tự) —
   adaptive effort chỉ hạ effort cho prompt ngắn. Prompt ngắn mà vẫn muốn
   max: tắt `GENIUS_ADAPTIVE_EFFORT` hoặc hạ `GENIUS_ADAPTIVE_EFFORT_THRESHOLD`.
2. **Chốt semantics khó ngay trong prompt** — hoặc ủy quyền tường minh:
   "Choose and document EXACT Unicode semantics (casefold vs lower,
   normalization) and lock them with required tests" thay vì để mặc định.
3. **Nêu layout mong muốn** nếu bạn có ý kiến ("two files at repo root; no
   src/, no packaging") — không nêu thì gate Minimal Layout tự chọn nhỏ nhất.
4. **Duyệt plan trước khi đốt tiền code**: `require_approval: true`, đọc
   `design.md` lúc pause, lệch thì `genius_orchestrate_reject` kèm `reason`
   rồi sửa prompt chạy lại.
5. Mục **Assumptions** trong plan là chỗ soi nhanh nhất: mọi thứ architect
   tự bịa thêm nằm ở đó — cắt scope thừa từ đây trước khi approve.

## Tra nhanh: chọn cấp nào?

| Việc | Cấp | Tool | Cờ nên dùng |
|---|---|---|---|
| Hỏi/so sánh/khảo sát | 0 | `genius_research` | — |
| 1 hàm, 1 file, không cần test tự sinh | 1 | `genius_code` | — |
| Tiện ích 1–3 file có test + audit + review | 2 | `/genius` | — |
| App nhiều module, config, edge cases | 3 | `/genius` | `@deep`, cân nhắc approval |
| Dự án lớn, nhiều milestone | 4 | `/genius` × N lần | `@deep` + `require_approval: true` |
| DEBUG: tự test thấy sai / chưa đúng ý | 5 | `gdbg_review` + `gdbg_code` (+`gdbg_unit_test`) | mẫu FIX, không re-orchestrate |
