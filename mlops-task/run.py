#!/usr/bin/env python3
"""
run.py — Minimal MLOps-style batch job.

Pipeline:
    1. Load + validate config (YAML)
    2. Load + validate dataset (CSV, must contain a 'close' column)
    3. Compute rolling mean on 'close' using window from config
    4. Generate binary signal: 1 if close > rolling_mean else 0
    5. Compute metrics (rows_processed, signal_rate, latency_ms)
    6. Write structured metrics.json (success or error) + detailed run.log

Usage:
    python run.py --input data.csv --config config.yaml \
                   --output metrics.json --log-file run.log

No paths are hard-coded; everything is supplied via CLI arguments.
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

REQUIRED_CONFIG_FIELDS = {"seed": int, "window": int, "version": str}
REQUIRED_DATA_COLUMN = "close"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch job: rolling-mean signal generator with metrics + logging."
    )
    parser.add_argument("--input", required=True, help="Path to input CSV (OHLCV data).")
    parser.add_argument("--config", required=True, help="Path to YAML config file.")
    parser.add_argument("--output", required=True, help="Path to write metrics JSON.")
    parser.add_argument("--log-file", required=True, help="Path to write the log file.")
    return parser.parse_args()


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def setup_logging(log_file: str) -> logging.Logger:
    logger = logging.getLogger("mlops_task")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()  # avoid duplicate handlers on re-invocation

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler — always write logs to run.log
    file_handler = logging.FileHandler(log_file, mode="w")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    # Console handler — so logs are also visible via `docker run` / local stdout
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    return logger


# --------------------------------------------------------------------------- #
# Custom exception for clean, expected validation failures
# --------------------------------------------------------------------------- #
class ValidationError(Exception):
    """Raised for any expected/handled validation failure (config or data)."""


# --------------------------------------------------------------------------- #
# Step 1: Load + validate config
# --------------------------------------------------------------------------- #
def load_config(config_path: str, logger: logging.Logger) -> dict:
    path = Path(config_path)
    if not path.exists():
        raise ValidationError(f"Config file not found: {config_path}")

    try:
        with open(path, "r") as f:
            config = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ValidationError(f"Invalid YAML in config file: {e}")

    if not isinstance(config, dict):
        raise ValidationError("Invalid config structure: expected a top-level YAML mapping.")

    for field, expected_type in REQUIRED_CONFIG_FIELDS.items():
        if field not in config:
            raise ValidationError(f"Missing required config field: '{field}'")
        if not isinstance(config[field], expected_type):
            raise ValidationError(
                f"Config field '{field}' must be of type {expected_type.__name__}, "
                f"got {type(config[field]).__name__}"
            )

    if config["window"] <= 0:
        raise ValidationError("Config field 'window' must be a positive integer.")

    # Set the seed for deterministic behavior
    np.random.seed(config["seed"])

    logger.info(
        "Config loaded + validated | seed=%s window=%s version=%s",
        config["seed"], config["window"], config["version"],
    )
    return config


# --------------------------------------------------------------------------- #
# Step 2: Load + validate dataset
# --------------------------------------------------------------------------- #
def load_dataset(input_path: str, logger: logging.Logger) -> pd.DataFrame:
    path = Path(input_path)
    if not path.exists():
        raise ValidationError(f"Input file not found: {input_path}")

    if path.stat().st_size == 0:
        raise ValidationError(f"Input file is empty: {input_path}")

    try:
        df = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        raise ValidationError(f"Input file has no parsable data: {input_path}")
    except pd.errors.ParserError as e:
        raise ValidationError(f"Invalid CSV format: {e}")

    if df.empty:
        raise ValidationError(f"Input file contains no rows: {input_path}")

    if REQUIRED_DATA_COLUMN not in df.columns:
        raise ValidationError(
            f"Missing required column '{REQUIRED_DATA_COLUMN}'. "
            f"Found columns: {list(df.columns)}"
        )

    if not pd.api.types.is_numeric_dtype(df[REQUIRED_DATA_COLUMN]):
        # Try coercion; anything unparsable becomes NaN, which we then reject.
        df[REQUIRED_DATA_COLUMN] = pd.to_numeric(df[REQUIRED_DATA_COLUMN], errors="coerce")
        if df[REQUIRED_DATA_COLUMN].isna().all():
            raise ValidationError(f"Column '{REQUIRED_DATA_COLUMN}' is not numeric.")

    logger.info("Rows loaded: %d", len(df))
    return df


# --------------------------------------------------------------------------- #
# Step 3 + 4: Rolling mean + signal generation
# --------------------------------------------------------------------------- #
def compute_signal(df: pd.DataFrame, window: int, logger: logging.Logger) -> pd.DataFrame:
    # min_periods=window keeps the first (window - 1) rows as NaN rather than
    # producing a partial/misleading rolling mean. This is a deliberate,
    # consistent choice, documented in the README.
    df["rolling_mean"] = df[REQUIRED_DATA_COLUMN].rolling(window=window, min_periods=window).mean()
    logger.info("Rolling mean computed | window=%d | NaN rows (warm-up)=%d",
                window, df["rolling_mean"].isna().sum())

    df["signal"] = np.where(df[REQUIRED_DATA_COLUMN] > df["rolling_mean"], 1, 0)
    # Rows still in warm-up (rolling_mean is NaN) have no valid signal; mark as NaN
    # and exclude them from signal-based metrics (signal_rate).
    df.loc[df["rolling_mean"].isna(), "signal"] = np.nan

    logger.info("Signal generation complete | valid signals=%d",
                int(df["signal"].notna().sum()))
    return df


# --------------------------------------------------------------------------- #
# Step 5: Metrics
# --------------------------------------------------------------------------- #
def compute_metrics(df: pd.DataFrame, config: dict, start_time: float,
                     logger: logging.Logger) -> dict:
    rows_processed = int(len(df))
    valid_signals = df["signal"].dropna()
    signal_rate = float(valid_signals.mean()) if len(valid_signals) > 0 else 0.0
    latency_ms = int(round((time.perf_counter() - start_time) * 1000))

    metrics = {
        "version": config["version"],
        "rows_processed": rows_processed,
        "metric": "signal_rate",
        "value": round(signal_rate, 4),
        "latency_ms": latency_ms,
        "seed": config["seed"],
        "status": "success",
    }
    logger.info(
        "Metrics summary | rows_processed=%d signal_rate=%.4f latency_ms=%d",
        rows_processed, signal_rate, latency_ms,
    )
    return metrics


# --------------------------------------------------------------------------- #
# Output writer (used for both success + error cases)
# --------------------------------------------------------------------------- #
def write_metrics(output_path: str, metrics: dict, logger: logging.Logger) -> None:
    with open(output_path, "w") as f:
        json.dump(metrics, f, indent=2)
    logger.info("Metrics written to %s", output_path)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    args = parse_args()
    logger = setup_logging(args.log_file)

    start_time = time.perf_counter()
    logger.info("Job start")

    # Best-effort version for error payloads (config may not have loaded yet)
    version_fallback = "v1"

    try:
        config = load_config(args.config, logger)
        df = load_dataset(args.input, logger)
        df = compute_signal(df, config["window"], logger)
        metrics = compute_metrics(df, config, start_time, logger)

        write_metrics(args.output, metrics, logger)
        print(json.dumps(metrics, indent=2))

        logger.info("Job end | status=success")
        return 0

    except ValidationError as e:
        logger.error("Validation error: %s", e)
        error_metrics = {
            "version": version_fallback,
            "status": "error",
            "error_message": str(e),
        }
        write_metrics(args.output, error_metrics, logger)
        print(json.dumps(error_metrics, indent=2))
        logger.info("Job end | status=error")
        return 1

    except Exception as e:  # noqa: BLE001 - catch-all so metrics.json is always written
        logger.exception("Unexpected error: %s", e)
        error_metrics = {
            "version": version_fallback,
            "status": "error",
            "error_message": f"Unexpected error: {e}",
        }
        write_metrics(args.output, error_metrics, logger)
        print(json.dumps(error_metrics, indent=2))
        logger.info("Job end | status=error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
