"""
finbert_label.py — FinBERT tone labeler for tune_tone.py corpus

Reads the TSV produced by tune_tone.py, scores each summary with
FinBERT-tone, adds comparison columns, and writes a new TSV.

No scraping, no DB — just reads the summary column.

Prerequisites:
    pip install transformers torch

    First run downloads ~440MB model to HuggingFace cache.
    Subsequent runs use the cache — no re-download.

Usage:
    python finbert_label.py --input tone_corpus.tsv
    python finbert_label.py --input tone_corpus.tsv --output tone_labeled.tsv
    python finbert_label.py --input tone_corpus.tsv --batch 32   # faster on GPU
    python finbert_label.py --input tone_corpus.tsv --mismatch-only  # print disagreements

Output TSV adds 3 columns to the input:
    finbert_label   Confident | Neutral | Cautious
    finbert_score   0.000–1.000 (model confidence)
    agree           YES | NO  (lexicon vs FinBERT agreement)

After running, sort by agree=NO to find lexicon gaps.
The per-sector mismatch breakdown tells you which sectors need tuning.

Model: yiyanghkust/finbert-tone
    Trained on forward-looking financial analyst statements.
    Labels: Positive → Confident | Negative → Cautious | Neutral → Neutral
    Max input: 512 tokens (~400 words). Summaries are capped at 800 chars
    in tune_tone.py so truncation is rare.
"""

import os
import sys
import csv
import argparse
from collections import Counter, defaultdict


# ── Label mapping ──────────────────────────────────────────────────────────────

_LABEL_MAP = {
    "Positive": "Confident",
    "Negative": "Cautious",
    "Neutral":  "Neutral",
}


# ── FinBERT loader ─────────────────────────────────────────────────────────────

def _load_finbert(model_name: str):
    try:
        from transformers import BertTokenizer, BertForSequenceClassification
        import torch
    except ImportError:
        print("ERROR: transformers / torch not installed.")
        print("Run: pip install transformers torch")
        sys.exit(1)

    # Auto-detect GPU
    if torch.cuda.is_available():
        device            = torch.device("cuda")
        gpu_name          = torch.cuda.get_device_name(0)
        vram_gb           = torch.cuda.get_device_properties(0).total_memory / 1e9
        recommended_batch = 128
        print(f"  GPU detected: {gpu_name} ({vram_gb:.1f}GB VRAM) — using CUDA")
    else:
        device            = torch.device("cpu")
        recommended_batch = 16
        print("  No GPU detected — using CPU")

    print(f"  Loading {model_name}")
    print("  (First run downloads ~440MB — cached after)")
    try:
        tokenizer = BertTokenizer.from_pretrained(model_name)
        model     = BertForSequenceClassification.from_pretrained(model_name)
        model     = model.to(device)
        model.eval()

        class _Bundle:
            pass
        bundle                    = _Bundle()
        bundle.tokenizer          = tokenizer
        bundle.model              = model
        bundle.device             = device
        bundle.id2label           = model.config.id2label  # read directly from model — never hardcode
        bundle._recommended_batch = recommended_batch
        print(f"  Model ready — id2label: {bundle.id2label}")
        return bundle
    except Exception as e:
        print(f"ERROR loading model: {e}")
        sys.exit(1)


# ── Batch scoring ──────────────────────────────────────────────────────────────

def _score_batch(bundle, texts: list[str], batch_size: int) -> list[tuple[str, float]]:
    """
    Score a list of texts using explicit BERT inference.
    Returns list of (mapped_label, confidence_score).
    """
    import torch
    results = []
    total   = len(texts)

    for start in range(0, total, batch_size):
        batch = texts[start:start + batch_size]
        end   = min(start + batch_size, total)
        print(f"  Scoring [{end:>5}/{total}]...", end="\r", flush=True)

        try:
            inputs = bundle.tokenizer(
                batch,
                padding=True,
                truncation=True,
                max_length=512,
                return_tensors="pt",
            ).to(bundle.device)

            with torch.no_grad():
                logits = bundle.model(**inputs).logits
                probs  = torch.softmax(logits, dim=-1)

            for prob_row in probs:
                idx       = prob_row.argmax().item()
                raw_label = bundle.id2label.get(idx, "Neutral")
                mapped    = _LABEL_MAP.get(raw_label, "Neutral")
                score     = round(prob_row[idx].item(), 4)
                results.append((mapped, score))

        except Exception as e:
            print(f"\n  Batch error at [{start}:{end}]: {e}")
            for _ in batch:
                results.append(("Neutral", 0.0))

    print(f"  Scoring [{total:>5}/{total}]... done        ")
    return results


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Add FinBERT tone labels to tune_tone.py TSV"
    )
    parser.add_argument("--input",         required=True,
                        help="Input TSV from tune_tone.py")
    parser.add_argument("--output",        default=None,
                        help="Output TSV (default: <input>_finbert.tsv)")
    parser.add_argument("--model",         default="yiyanghkust/finbert-tone",
                        help="HuggingFace model name")
    parser.add_argument("--batch",         type=int, default=16,
                        help="Batch size (increase to 32+ if you have GPU)")
    parser.add_argument("--mismatch-only", action="store_true",
                        help="Print disagreement rows to console")
    args = parser.parse_args()

    # ── Output path ───────────────────────────────────────────────────────────
    if args.output is None:
        base, ext = os.path.splitext(args.input)
        args.output = f"{base}_finbert{ext or '.tsv'}"

    # ── Read input TSV ────────────────────────────────────────────────────────
    print(f"\nReading {args.input}...")
    if not os.path.exists(args.input):
        print(f"ERROR: {args.input} not found")
        sys.exit(1)

    with open(args.input, newline="", encoding="utf-8") as f:
        reader  = csv.DictReader(f, delimiter="\t")
        rows    = list(reader)
        in_fields = reader.fieldnames or []

    if not rows:
        print("ERROR: input TSV is empty")
        sys.exit(1)

    if "summary" not in in_fields:
        print("ERROR: 'summary' column not found in input TSV")
        print(f"  Found columns: {in_fields}")
        sys.exit(1)

    print(f"  {len(rows)} rows loaded")

    # ── Filter rows with usable summaries ─────────────────────────────────────
    valid_idx = [i for i, r in enumerate(rows) if r.get("summary", "").strip()]
    skipped   = len(rows) - len(valid_idx)
    print(f"  {len(valid_idx)} rows with summaries  |  {skipped} empty (will get Neutral/0.0)\n")

    # ── Load model ────────────────────────────────────────────────────────────
    bundle = _load_finbert(args.model)

    # Use GPU-recommended batch size unless user explicitly passed --batch
    batch_size = args.batch
    if batch_size == 16 and hasattr(bundle, "_recommended_batch"):
        batch_size = bundle._recommended_batch
        if batch_size != 16:
            print(f"  Using batch size {batch_size} (auto from GPU)")

    # ── Score valid rows ──────────────────────────────────────────────────────
    texts  = [rows[i]["summary"].strip() for i in valid_idx]
    scored = _score_batch(bundle, texts, batch_size)

    # ── Attach scores to rows ─────────────────────────────────────────────────
    fb_labels = ["Neutral"] * len(rows)
    fb_scores = [0.0]      * len(rows)
    for idx, (label, score) in zip(valid_idx, scored):
        fb_labels[idx] = label
        fb_scores[idx] = score

    for i, row in enumerate(rows):
        row["finbert_label"] = fb_labels[i]
        row["finbert_score"] = f"{fb_scores[i]:.4f}"
        lex   = row.get("tone_label", "")
        row["agree"] = "YES" if lex == fb_labels[i] else "NO"

    # ── Write output TSV ──────────────────────────────────────────────────────
    out_fields = list(in_fields) + ["finbert_label", "finbert_score", "agree"]
    # Avoid duplicate columns if re-running on already-labeled file
    seen = set()
    out_fields = [f for f in out_fields if not (f in seen or seen.add(f))]

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields,
                                delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nOutput → {args.output}")

    # ── Mismatch console dump ─────────────────────────────────────────────────
    mismatches = [r for r in rows if r.get("agree") == "NO"]
    if args.mismatch_only and mismatches:
        print(f"\nDisagreements ({len(mismatches)}):\n")
        print(f"{'Ticker':<8} {'Sector':<24} {'Lexicon':<12} {'FinBERT':<12} "
              f"{'FB Score':>8}  Summary")
        print("─" * 100)
        for r in mismatches:
            summ = r.get("summary", "")[:60]
            print(f"{r.get('ticker',''):8s} {r.get('sector',''):24s} "
                  f"{r.get('tone_label',''):12s} {r['finbert_label']:12s} "
                  f"{float(r['finbert_score']):>8.4f}  {summ}")

    # ── Summary stats ─────────────────────────────────────────────────────────
    total      = len(rows)
    agree_n    = sum(1 for r in rows if r.get("agree") == "YES")
    disagree_n = total - agree_n
    agree_pct  = agree_n / total * 100 if total else 0

    print(f"\n{'═'*65}")
    print(f"  Agreement rate: {agree_n}/{total} ({agree_pct:.1f}%)")
    print(f"  Disagreements : {disagree_n} ({100-agree_pct:.1f}%)")

    # Overall label distributions
    lex_dist = Counter(r.get("tone_label","") for r in rows)
    fb_dist  = Counter(r.get("finbert_label","") for r in rows)

    print(f"\n  {'Label':<12} {'Lexicon':>10} {'FinBERT':>10}")
    print("  " + "─"*34)
    for label in ["Confident", "Neutral", "Cautious"]:
        l_n = lex_dist.get(label, 0)
        f_n = fb_dist.get(label, 0)
        print(f"  {label:<12} {l_n:>7} ({l_n/total*100:.0f}%)  "
              f"{f_n:>7} ({f_n/total*100:.0f}%)")

    # Per-sector agreement
    sector_stats = defaultdict(lambda: {"agree": 0, "total": 0})
    for r in rows:
        s = r.get("sector", "Unknown")
        sector_stats[s]["total"] += 1
        if r.get("agree") == "YES":
            sector_stats[s]["agree"] += 1

    print(f"\n  Per-sector agreement:")
    print(f"  {'Sector':<28} {'N':>5}  {'Agree':>6}  {'Disagree':>9}")
    print("  " + "─"*52)
    for sector in sorted(sector_stats):
        st  = sector_stats[sector]
        n   = st["total"]
        ag  = st["agree"]
        dis = n - ag
        pct = ag / n * 100 if n else 0
        bar = "█" * int(pct / 5)   # 20 chars = 100%
        print(f"  {sector:<28} {n:>5}  {pct:>5.0f}%  {dis:>8}  {bar}")

    # Disagreement patterns — what does FinBERT say when lexicon says X?
    print(f"\n  Disagreement breakdown (lexicon → FinBERT):")
    confusion: Counter = Counter()
    for r in rows:
        if r.get("agree") == "NO":
            confusion[(r.get("tone_label","?"), r["finbert_label"])] += 1
    for (lex, fb), count in confusion.most_common():
        print(f"    Lexicon={lex:<12} → FinBERT={fb:<12} {count:>5}x")

    # High-confidence FinBERT disagreements — best candidates for lexicon fix
    high_conf_dis = [
        r for r in rows
        if r.get("agree") == "NO" and float(r.get("finbert_score", 0)) >= 0.85
    ]
    if high_conf_dis:
        print(f"\n  High-confidence FinBERT disagreements (score ≥ 0.85): "
              f"{len(high_conf_dis)}")
        print(f"  These are the highest-priority rows for lexicon review.\n")
        print(f"  {'Ticker':<8} {'Sector':<22} {'Lex':<12} {'FB':<12} "
              f"{'Score':>6}  Summary[:80]")
        print("  " + "─"*100)
        for r in sorted(high_conf_dis,
                        key=lambda x: float(x.get("finbert_score", 0)),
                        reverse=True)[:20]:
            summ = r.get("summary", "")[:80]
            print(f"  {r.get('ticker',''):8s} {r.get('sector',''):22s} "
                  f"{r.get('tone_label',''):12s} {r['finbert_label']:12s} "
                  f"{float(r['finbert_score']):>6.4f}  {summ}")

    print(f"\n{'═'*65}")
    print(f"\nNext steps:")
    print(f"  1. Open {args.output} — sort by agree=NO")
    print(f"  2. Focus on finbert_score >= 0.85 rows (high-confidence mismatches)")
    print(f"  3. Check per-sector disagreements above for systematic gaps")
    print(f"  4. Add missing words to _TONE_CONFIDENT / _TONE_CAUTIOUS in agents.py")
    print(f"  5. Re-run finbert_label.py to confirm improvement\n")


if __name__ == "__main__":
    main()
