# EQUITY ANALYST PIPELINE — SESSION 3 ADDENDUM (2026-07-01)

Follow-up to "HANDOFF 2026-06-30 (Session 2)". Covers P5 validation and a
re-prioritization of remaining pending items.

---

## P5 — RESOLVED: Full 519-ticker validation re-run

Ran `run_insider_validation.py` against the full universe with the
footnote-resolution and stale-date fixes in place. Output: `insider_summary.txt`.

**Result: clean baseline, no regressions.**

| Check | Result |
|---|---|
| Total tickers | 519 |
| Errors | 0 |
| Activity found | 366 |
| No activity | 153 |
| Cluster buying signals | 14 (was 15 pre-fix — one fewer is expected: the dedup/stale-date fix removing a false positive, not a regression) |
| NVDA | n=7, 3 sellers, $410,580,151 — matches last session's validated $410.6M figure ✓ |
| DDOG | n=240 (was n=241 pre-fix) — one fewer txn consistent with stale-date/dedup fix working as intended ✓ |
| MMM | n=0 — matches confirmed stale-DRIP filter result from last session ✓ |
| UNH | n=1 real sell, no buy cluster — consistent with last session's finding that the reported "buy cluster" was actually $0-cost grants, not purchases ✓ |

**One result investigated and confirmed correct (not a bug):**
NKE's cluster-buy signal listed "Timothy D Cook, Director" as the top
buyer. This looked like a name-resolution bug at first glance (Apple's
CEO showing up as a Nike insider) but is in fact accurate — Tim Cook has
sat on Nike's board since 2005 and has served as lead independent
director since 2016. **Action: added a one-line note below so future
reviewers don't re-investigate the same false alarm.**

> **Note for future reviewers:** insider boards can legitimately include
> executives of unrelated companies (e.g. Apple's CEO has sat on Nike's
> board since 2005). A cross-company name match in the insider table is
> not inherently a name-resolution bug — verify against the real board
> roster before assuming it's a data error.

**P5 is closed.** `insider_summary.txt` (519-ticker, 0 errors) is the
current trustworthy baseline going forward.

---

## Updated pending list

- **P1 — CLOSED, confirmed non-issue.** Visually inspected the actual
  rendered PDF (rasterized `MU_equity_brief_2026-07-01.pdf` page 5, not just
  extracted text) — the insider-summary table renders cleanly with proper
  cell separation: `90d | 0 | 5 | $0 | $78,568,092 | -$78,568,092 | $652.48`,
  each value in its own gridlined cell. The "90d05"-style concatenation is
  confirmed to be a text-extraction artifact only (whatever tool flattens
  the PDF back to text ignores cell/grid structure), not a real rendering
  defect. **Correction to the prior session-3 entry:** the "root cause"
  diagnosed there (55mm default column squeezing dollar values) does not
  hold up — the original bug report showed the same merge pattern even when
  the Window column had a generous 55mm width, which rules out a
  width-squeeze explanation. That diagnosis was made from reading the code
  only, without a rendered page to check against, and was stated with more
  confidence than the evidence supported. The col_widths change made to
  `renderer.py` is harmless (dollar columns look fine either way) but
  wasn't fixing a real bug. No further action needed on P1.
- **P2 — deprioritized per user direction.** Face-amount parsing beyond
  Format J doesn't affect the final output materially enough to justify
  the effort right now. Leaving as-is; revisit only if it comes up in
  review.
- **P3, P4 — unchanged from Session 2.** Speculative refinements, no
  immediate action.
