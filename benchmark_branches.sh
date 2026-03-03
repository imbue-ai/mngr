#!/usr/bin/env bash
# Benchmark: alternate between two commits N times each, timing the default test suite.
# Usage: ./benchmark_branches.sh [iterations]
#
# Alternates to mitigate changing system load effects.

set -euo pipefail

COMMIT_A="b156fc6c460cfc29242894b2ae778ae7efe3168f"  # mng/accelerate-tests-local (no cache)
COMMIT_B="c2501b80cf182a4a9d173eb166448489b0a3aca1"  # mng/whats-up-with-xdist (with entry_points cache)
LABEL_A="no-cache"
LABEL_B="with-cache"
ITERATIONS="${1:-5}"
RESULTS_FILE="benchmark_results.txt"

# Find repo root (works whether run from repo or elsewhere)
REPO="$(git rev-parse --show-toplevel)"
cd "$REPO"

TEST_CMD="uv run pytest -nauto --no-cov -m 'not tmux and not modal and not docker and not acceptance and not release' -q"

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "ERROR: Working tree is not clean. Commit or stash changes first."
    exit 1
fi

ORIGINAL_REF=$(git rev-parse HEAD)

# Store results in a temp file, one number per line, to avoid bash array expansion issues
TIMES_A=$(mktemp)
TIMES_B=$(mktemp)

cleanup() {
    echo ""
    echo "Returning to original ref..."
    git checkout "$ORIGINAL_REF" --quiet --detach 2>/dev/null || git checkout "$ORIGINAL_REF" --quiet
    rm -f "$TIMES_A" "$TIMES_B"
}
trap cleanup EXIT

echo "Benchmark: A=$LABEL_A vs B=$LABEL_B"
echo "Iterations: $ITERATIONS (alternating)"
echo "Test command: $TEST_CMD"
echo "==========================================="
echo ""

for i in $(seq 1 "$ITERATIONS"); do
    echo "--- Round $i / $ITERATIONS ---"

    # Commit A
    git checkout "$COMMIT_A" --quiet --detach
    start=$(python3 -c 'import time; print(time.monotonic())')
    eval "$TEST_CMD" > /dev/null 2>&1 || true
    end=$(python3 -c 'import time; print(time.monotonic())')
    elapsed=$(python3 -c "print(f'{$end - $start:.2f}')")
    echo "$elapsed" >> "$TIMES_A"
    echo "  A: ${elapsed}s"

    # Commit B
    git checkout "$COMMIT_B" --quiet --detach
    start=$(python3 -c 'import time; print(time.monotonic())')
    eval "$TEST_CMD" > /dev/null 2>&1 || true
    end=$(python3 -c 'import time; print(time.monotonic())')
    elapsed=$(python3 -c "print(f'{$end - $start:.2f}')")
    echo "$elapsed" >> "$TIMES_B"
    echo "  B: ${elapsed}s"

    echo ""
done

# Compute stats using the temp files
python3 -c "
import statistics

with open('$TIMES_A') as f:
    a = [float(line.strip()) for line in f if line.strip()]
with open('$TIMES_B') as f:
    b = [float(line.strip()) for line in f if line.strip()]

print('===========================================')
print('Benchmark Results')
print('===========================================')
print()
print('A: $LABEL_A')
print('B: $LABEL_B')
print()
print('Raw times (seconds):')
print()
print(f'{\"Round\":<6}  {\"A\":<12}  {\"B\":<12}')
print(f'{\"-----\":<6}  {\"--------\":<12}  {\"--------\":<12}')
for i, (ta, tb) in enumerate(zip(a, b), 1):
    print(f'{i:<6}  {ta:<12.2f}  {tb:<12.2f}')
print()

mean_a = statistics.mean(a)
mean_b = statistics.mean(b)
median_a = statistics.median(a)
median_b = statistics.median(b)
stdev_a = statistics.stdev(a) if len(a) > 1 else 0
stdev_b = statistics.stdev(b) if len(b) > 1 else 0

print(f'Mean:    A={mean_a:.2f}s  B={mean_b:.2f}s  diff={mean_b - mean_a:+.2f}s ({(mean_b - mean_a) / mean_a * 100:+.1f}%)')
print(f'Median:  A={median_a:.2f}s  B={median_b:.2f}s  diff={median_b - median_a:+.2f}s ({(median_b - median_a) / median_a * 100:+.1f}%)')
print(f'Stdev:   A={stdev_a:.2f}s  B={stdev_b:.2f}s')
print()
print(f'All A: {a}')
print(f'All B: {b}')
" | tee "$RESULTS_FILE"

echo ""
echo "Results saved to $RESULTS_FILE"
