from agent import run_agent

TASKS = [
  "Implement a rate limiter class (token bucket) with tests",
  "Parse and evaluate arithmetic expressions with + - * / and parentheses",
  "Implement a function that merges overlapping intervals",
  "Build a Trie with insert/search/startswith",
  "Write a CSV-to-JSON converter handling quoted commas",
]

results = []
for task in TASKS:
    try:
        _, stats = run_agent(task, on_event=lambda event: None)
    except Exception as exc:
        stats = {
            "passed": False,
            "steps": 0,
            "seconds": 0,
            "tokens": 0,
            "failure_reason": str(exc) or type(exc).__name__,
        }
    results.append((task, stats))

print("| Task | Result | Iterations | Seconds | Tokens |")
print("| --- | --- | ---: | ---: | ---: |")
for task, stats in results:
    result = "PASS" if stats.get("passed") else "FAIL"
    label = task.replace("|", "\\|")
    print(
        f"| {label} | {result} | {stats.get('steps', 0)} "
        f"| {stats.get('seconds', 0):.1f} | {stats.get('tokens', 0)} |"
    )

passed = sum(bool(stats.get("passed")) for _, stats in results)
total = len(results)
print(f"\nPass rate: {passed}/{total}")
if results:
    print(f"Average iterations: {sum(stats.get('steps', 0) for _, stats in results) / total:.1f}")
    print(f"Average time: {sum(stats.get('seconds', 0) for _, stats in results) / total:.1f}s")
    print(f"Total tokens: {sum(stats.get('tokens', 0) for _, stats in results):,}")

if passed != total:
    raise SystemExit(1)