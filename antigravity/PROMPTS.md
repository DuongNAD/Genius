# Bộ prompt chuẩn cho /genius (Antigravity → Genius pipeline)

Mẫu prompt theo từng cấp độ để điền vào `/genius <prompt>` (hoặc gọi thẳng
`genius_orchestrate`). Đúc kết từ các run thật: prompt càng nêu rõ **hành vi
chính xác + tiêu chí nghiệm thu**, bản kế hoạch của Claude càng chuẩn và coder
gần như không thể hiểu sai.

**Quy tắc chung (đọc trước khi điền):**

- Viết phần prompt bằng **tiếng Anh** — các agent cho kết quả ổn định nhất.
- **KHÔNG liệt kê file test trong FILES** — pipeline tự sinh `tests/` + chạy
  chúng + security audit + final review cho bạn. File cap trong prompt chỉ áp
  cho file sản phẩm.
- Con số thời gian tham khảo (config hiện tại, plan = Claude Opus effort max):
  cấp 2 ≈ 15–20 phút, phần lớn nằm ở stage code+test. `current_stage` trong
  `genius_orchestrate_status` cho biết pipeline đang làm gì.
- Thêm `@deep` vào đầu prompt khi bài toán khó/nhiều ràng buộc (đẩy effort của
  agent lên cao nhất). Thêm `require_approval: true` khi bạn muốn duyệt từng
  stage (research → design → code) trước khi chạy tiếp.
- Kết quả nằm trong `workspace` mà status trả về
  (`.genius_jobs/<job_id>/projects/<slug>/`).

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

Template (điền vào `<...>`, xóa dòng không cần):

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

Ví dụ đã điền (run thật, điểm eval 5/5):

```
Build a tiny Python utility project 'txtstats': a single module txtstats.py
exposing count_stats(text: str) -> dict with keys lines, words, chars, plus a
main() that reads a file path from sys.argv and prints the stats as JSON.
Keep the design to AT MOST 2 small files (txtstats.py and README.md).
No external dependencies.
```

Mẹo: cap số file chặt (như trên) khiến kiến trúc sư tự chuyển test sang
doctest nhúng — gọn và vẫn kiểm chứng được.

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

## Cấp 5 — Sửa/refactor code CÓ SẴN

Pipeline build dự án mới trong jobs dir — nó không sửa in-place repo của bạn.
Hai đường:

- Sửa nhỏ/cục bộ: dùng `genius_review` (dán code, nhận findings) rồi
  `genius_code` với prompt kiểu:

  ```
  Refactor the following file to <goal> while preserving its public API and
  behavior. Return the COMPLETE new file content.
  <paste the file>
  ```

- Viết lại một thành phần: chạy `/genius` cấp 2–3, trong prompt dán phần
  interface hiện tại vào mục CONTEXT + ghi "must stay drop-in compatible with
  the pasted interface", rồi tự copy kết quả từ workspace về repo.

---

## Tra nhanh: chọn cấp nào?

| Việc | Cấp | Tool | Cờ nên dùng |
|---|---|---|---|
| Hỏi/so sánh/khảo sát | 0 | `genius_research` | — |
| 1 hàm, 1 file, không cần test tự sinh | 1 | `genius_code` | — |
| Tiện ích 1–3 file có test + audit + review | 2 | `/genius` | — |
| App nhiều module, config, edge cases | 3 | `/genius` | `@deep`, cân nhắc approval |
| Dự án lớn, nhiều milestone | 4 | `/genius` × N lần | `@deep` + `require_approval: true` |
| Sửa code có sẵn | 5 | `genius_review` + `genius_code` | — |
