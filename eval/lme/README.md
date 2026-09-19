# LongMemEval evaluation data

Full-retrieval evaluation on [LongMemEval](https://huggingface.co/datasets/xiaowu0162/LongMemEval) (official MIT-licensed dataset), 500-question corpus built by `build_lme_corpus.py`.

## What is here

| File | What it is |
|---|---|
| `questions.jsonl` | the 500-question corpus (sampled from LongMemEval-S with seed 42) |
| `lme_retrieval_results.json` | per-question retrieval metrics → headline R@5 |
| `lme_qa_results.json` | end-to-end QA subset (n=100): local Qwen3-4B reader + official ans-check prompt, same model for judge |
| `qa_log.jsonl` | full per-question QA trace (question, retrieved ids, generated answer) |
| `corpus_stats.json` / `flat_by_type.json` | corpus composition and per-question-type breakdown |
| `build_lme_corpus.py` → `ingest_turns.py` → `eval_retrieval.py` / `eval_qa.py` | the reproduction pipeline, in run order |
| `run_eval.sh` | one command for the whole chain above: docker compose PG → schema+migrations → engine → ingest → 500-question eval → R@5 (`--dry-run` to preview, `--with-qa` for the QA subset) |

## Honesty notes

- The QA subset is **self-judged** (same local model generates and grades), so its numbers are not comparable to official GPT-4o-judged leaderboards — it is an internal consistency check only.
- The retrieval numbers (R@5) are dataset-only scoring with no LLM in the loop, so they are directly comparable.
- The haystack is the benchmark's synthetic ShareGPT-derived corpus. **No real production memories are included in any file here** — retrieved ids are synthetic corpus ids, verifiable in `qa_log.jsonl`.
- `turns.jsonl` (the 48 MB haystack transcript) is not committed; regenerate it with `build_lme_corpus.py` from the official dataset.
