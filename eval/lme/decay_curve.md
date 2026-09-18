# Corpus-size decay curve (LongMemEval-S, synthetic corpus)

Same 100-question set scored at every bucket; bank `eval_decay` rebuilt per bucket;
bank-wide top-k (global mode); official LongMemEval scorer. R@5 = recall_any@5.

| corpus size (turns) | R@5 (turn) | R@5 (session) | R@1 (turn) | R@10 (turn) |
|---|---|---|---|---|
| 500 | 0.97 | 0.97 | 0.97 | 0.97 |
| 1000 | 0.96 | 0.96 | 0.96 | 0.96 |
| 2000 | 0.94 | 0.94 | 0.94 | 0.94 |
| 4000 | 0.91 | 0.91 | 0.91 | 0.91 |
| 6000 | 0.88 | 0.88 | 0.88 | 0.88 |

*Corpus = LongMemEval synthetic haystack turns only (no production memories). Gold turns of the scored set are present in every bucket; distractor fill grows with bucket size (nested).*
