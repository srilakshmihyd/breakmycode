# Evaluation suite

`cases.json` records ground truth, CI thresholds, run counts, expected flaky and
failing tests, and expected model classifications. Fixtures cover randomized
flakes, wrong expectations, prompt injection, concurrency, and simulated
network-status failures.

Run quality and security regression tests with:

```bash
pytest
breakmycode red-team ./demo_repo
breakmycode eval ./evals/cases.json
breakmycode eval ./evals/cases.json --verify-patches --output eval-report.json
```

Verified evaluation applies patches only inside temporary copies and reports
verified-fix and regression rates. A failing threshold produces a nonzero exit
code for CI.
