# mlops-task — Batch Rolling-Mean Signal Job

A minimal, reproducible MLOps-style batch job that reads OHLCV data, computes a
rolling mean on `close`, generates a binary trading signal, and writes
structured metrics (JSON) plus detailed logs. Built for local execution and
one-command Docker deployment.

---

## 1. What it does

1. Loads and validates `config.yaml` (`seed`, `window`, `version`).
2. Loads and validates `data.csv` (must contain a `close` column, non-empty,
   valid CSV).
3. Sets `numpy.random.seed(seed)` so the run is deterministic.
4. Computes a rolling mean of `close` over `window` rows.
5. Generates `signal = 1 if close > rolling_mean else 0`.
6. Computes metrics: `rows_processed`, `signal_rate`, `latency_ms`.
7. Writes `metrics.json` (always — success **or** error) and `run.log`.

### Handling of the first `window - 1` rows
The rolling mean needs `window` observations before it is meaningful, so the
first `window - 1` rows have **no rolling mean** (`min_periods=window` →
`NaN`), and therefore **no signal** (also `NaN`) for those rows. This is a
deliberate, consistent choice:

- `rows_processed` in `metrics.json` = **total rows loaded** (e.g. 10,000).
- `signal_rate` = mean of `signal` computed **only over rows with a valid
  (non-NaN) signal** (e.g. 9,996 of 10,000 rows when `window=5`).

This avoids treating "not yet enough history" as a fake `0` signal, which
would silently bias `signal_rate` downward.

---

## 2. Repo structure

```
.
├── run.py             # main batch job
├── config.yaml         # seed / window / version
├── data.csv            # provided OHLCV dataset (10,000 rows)
├── requirements.txt    # pinned Python dependencies
├── Dockerfile           # one-command containerized run
├── README.md            # this file
├── metrics.json          # sample output from a successful run
└── run.log               # sample log from a successful run
```

---

## 3. Local run instructions

### Requirements
- Python 3.9+
- `pip install -r requirements.txt`

### Setup
```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Run
```bash
python run.py --input data.csv --config config.yaml \
              --output metrics.json --log-file run.log
```

No paths are hard-coded anywhere in `run.py` — every path is supplied via
CLI flags (`--input`, `--config`, `--output`, `--log-file`), so the job can
be pointed at any dataset/config/output location.

The final metrics are printed to stdout as well as written to
`metrics.json`, e.g.:

```json
{
  "version": "v1",
  "rows_processed": 10000,
  "metric": "signal_rate",
  "value": 0.4991,
  "latency_ms": 35,
  "seed": 42,
  "status": "success"
}
```

Exit code: `0` on success, non-zero (`1`) on any handled or unhandled error.

### Determinism check
Running the command twice on the same input/config always produces the same
`signal_rate` (only `latency_ms`, which measures wall-clock runtime, will
differ between runs) — verified by running the job twice and diffing the
`value` field.

---

## 4. Docker build/run commands

```bash
docker build -t mlops-task .
docker run --rm mlops-task
```

The image bundles `data.csv` and `config.yaml`, so `docker run --rm
mlops-task` requires no extra flags. It:
- Runs the full pipeline inside the container.
- Prints the final `metrics.json` content to stdout.
- Writes `metrics.json` and `run.log` inside the container at `/app`.
- Exits `0` on success, non-zero on failure.

To pull the generated files back out of the container onto the host (e.g.
for inspection):

```bash
docker create --name mlops-task-run mlops-task
docker cp mlops-task-run:/app/metrics.json ./metrics.json
docker cp mlops-task-run:/app/run.log ./run.log
docker rm mlops-task-run
```

To run against a **different** dataset/config without rebuilding the image,
override the entrypoint args:

```bash
docker run --rm -v $(pwd)/other_data.csv:/app/other_data.csv \
  mlops-task python run.py --input other_data.csv --config config.yaml \
  --output metrics.json --log-file run.log
```

---

## 5. Error handling

The job validates and cleanly reports (rather than crashing on) all of the
following, always writing a `metrics.json` with `"status": "error"` and a
non-zero exit code:

| Case                          | Behavior                                             |
|--------------------------------|-------------------------------------------------------|
| Missing input file              | `ValidationError` → error metrics + log entry         |
| Invalid CSV format               | Caught via `pandas.errors.ParserError`                |
| Empty input file                  | Checked before parsing (file size + row count)        |
| Missing `close` column             | Explicit column check with helpful message            |
| Invalid/incomplete config structure | Field presence + type validation on `seed`/`window`/`version` |
| Any other unexpected exception       | Caught by a top-level handler; full traceback logged  |

---

## 6. Logging (`run.log`)

Every run logs, with timestamps:
- Job start
- Config loaded + validated (seed / window / version)
- Rows loaded
- Rolling mean computed (window size, warm-up NaN count)
- Signal generation complete (valid signal count)
- Metrics summary (rows_processed, signal_rate, latency_ms)
- Job end + final status
- Any exceptions / validation errors (with full traceback for unexpected errors)

---

## 7. Example `metrics.json` (success)

```json
{
  "version": "v1",
  "rows_processed": 10000,
  "metric": "signal_rate",
  "value": 0.4991,
  "latency_ms": 35,
  "seed": 42,
  "status": "success"
}
```

## Example `metrics.json` (error)

```json
{
  "version": "v1",
  "status": "error",
  "error_message": "Missing required column 'close'. Found columns: ['a', 'b', 'c']"
}
```

---

## 8. Data note

The provided `data.csv` contains OHLCV columns:
`timestamp, open, high, low, close, volume_btc, volume_usd`. Only `close` is
used by this job, per the task spec.
