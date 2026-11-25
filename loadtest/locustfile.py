"""Relay load test: streaming chat requests against any OpenAI compatible endpoint.

Every request: 128 output tokens, temperature 0, ignore_eos true, a random prompt from prompts.json.
TTFT is the time from sending the request to the first streamed chunk that carries text.

Environment:
  MODEL_ID     served model name (default Qwen/Qwen2.5-1.5B-Instruct)
  SHAPE        steady | ramp | spike | calibrate (default steady), see CLAUDE.md
  USERS        calibrate only, user count (default 4)
  DURATION     calibrate only, seconds (default 120)
  RESULTS_DIR  where requests.csv is written on test stop (default .)
  MAX_TOKENS   default 128; change only for debugging

Run:
  SHAPE=spike locust -f loadtest/locustfile.py --headless --host http://127.0.0.1:8000
"""
from __future__ import annotations

import csv
import json
import os
import random
import time
from pathlib import Path

from locust import HttpUser, LoadTestShape, constant, events, task

HERE = Path(__file__).resolve().parent
MODEL_ID = os.environ.get("MODEL_ID", "Qwen/Qwen2.5-1.5B-Instruct")
SHAPE = os.environ.get("SHAPE", "steady").lower()
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "128"))
REQUEST_TIMEOUT_S = 60.0
PROMPTS: list[str] = json.loads((HERE / "prompts.json").read_text())

# One row per request: (start unix time, ttft_s or "", latency_s, ok 0/1). Written on test_stop.
RESULTS: list[tuple[float, str, float, int]] = []


def record(start: float, ttft: float | None, latency: float, ok: bool) -> None:
    RESULTS.append((round(start, 3), "" if ttft is None else f"{ttft:.4f}", round(latency, 4), int(ok)))


def chunk_text(line: bytes) -> tuple[bool, str]:
    """Parse one SSE line. Returns (is_done, text). Non data lines give (False, "")."""
    if not line.startswith(b"data:"):
        return False, ""
    data = line[5:].strip()
    if data == b"[DONE]":
        return True, ""
    choices = json.loads(data).get("choices") or [{}]
    delta = choices[0].get("delta") or {}
    return False, delta.get("content") or ""


class ChatUser(HttpUser):
    """A closed loop user: sends the next request as soon as the previous one finishes."""

    wait_time = constant(0)

    @task
    def chat(self) -> None:
        body = {
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": random.choice(PROMPTS)}],
            "max_tokens": MAX_TOKENS,
            "temperature": 0,
            "ignore_eos": True,
            "stream": True,
        }
        start_wall = time.time()
        start = time.perf_counter()
        ttft: float | None = None
        ok = False
        with self.client.post("/v1/chat/completions", json=body, stream=True, catch_response=True,
                              timeout=REQUEST_TIMEOUT_S, name="chat") as resp:
            try:
                if resp.status_code != 200:
                    resp.failure(f"HTTP {resp.status_code}")
                else:
                    for line in resp.iter_lines():
                        if time.perf_counter() - start > REQUEST_TIMEOUT_S:
                            raise TimeoutError(f"stream exceeded {REQUEST_TIMEOUT_S:.0f} s")
                        done, text = chunk_text(line)
                        if text and ttft is None:
                            ttft = time.perf_counter() - start
                        if done:
                            break
                    ok = ttft is not None
                    if ok:
                        resp.success()
                    else:
                        resp.failure("stream ended without tokens")
            except Exception as exc:  # noqa: BLE001, any stream error counts as a failed request
                resp.failure(f"{type(exc).__name__}: {exc}")
                ok = False
        latency = time.perf_counter() - start
        record(start_wall, ttft if ok else None, latency, ok)
        if ttft is not None:
            # Custom event so Locust's own stats and CSV show a "ttft" row next to "chat".
            events.request.fire(request_type="TTFT", name="ttft", response_time=ttft * 1000.0,
                                response_length=0, exception=None, context={})


@events.test_stop.add_listener
def write_requests_csv(environment, **_kwargs) -> None:
    out_dir = Path(os.environ.get("RESULTS_DIR", "."))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "requests.csv"
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["start", "ttft_s", "latency_s", "ok"])
        writer.writerows(sorted(RESULTS))
    print(f"relay: wrote {len(RESULTS)} rows to {path}")


# ---------------------------------------------------------------- traffic shapes, see CLAUDE.md

def steady(t: float) -> int | None:
    return 8 if t < 600 else None


def ramp(t: float) -> int | None:
    return round(2 + (60 - 2) * t / 600) if t < 600 else None


def spike(t: float) -> int | None:
    if t >= 720:
        return None
    return 60 if 120 <= t < 420 else 4


def calibrate(t: float) -> int | None:
    users = int(os.environ.get("USERS", "4"))
    duration = float(os.environ.get("DURATION", "120"))
    return users if t < duration else None


SHAPES = {"steady": steady, "ramp": ramp, "spike": spike, "calibrate": calibrate}
if SHAPE not in SHAPES:
    raise SystemExit(f"SHAPE must be one of {sorted(SHAPES)}, got {SHAPE!r}")


class RelayShape(LoadTestShape):
    """Users over time for the chosen SHAPE. A high spawn rate makes level changes near instant."""

    def tick(self) -> tuple[int, float] | None:
        users = SHAPES[SHAPE](self.get_run_time())
        return None if users is None else (users, 100.0)
