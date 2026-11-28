import csv
import json
import os
import random
import time
from typing import Any, Dict, List, Optional
from locust import HttpUser, LoadTestShape, events, task

# Global in-memory storage for raw request metrics
REQUEST_RECORDS: List[Dict[str, Any]] = []

# Load prompts
PROMPTS_FILE = os.path.join(os.path.dirname(__file__), "prompts.json")
try:
    with open(PROMPTS_FILE, "r") as f:
        PROMPTS = json.load(f)
except Exception:
    PROMPTS = ["Explain cloud computing architecture and scaling principles in detail."]

MODEL_ID = os.environ.get("MODEL_ID", "Qwen/Qwen2.5-1.5B-Instruct")
RESULTS_DIR = os.environ.get("RESULTS_DIR", ".")


@events.init_command_line_parser.add_listener
def _(parser):
    # Hook for custom arguments if needed
    pass


class LLMUser(HttpUser):
    # Minimal wait time between requests to simulate continuous user traffic
    min_wait = 0.1
    max_wait = 0.5

    @task
    def stream_completion(self):
        prompt = random.choice(PROMPTS)
        payload = {
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 128,
            "temperature": 0.0,
            "ignore_eos": True,
            "stream": True,
        }

        start_time = time.time()
        ttft_s: Optional[float] = None
        ok = False

        try:
            with self.client.post(
                "/v1/chat/completions",
                json=payload,
                headers={"Content-Type": "application/json"},
                stream=True,
                timeout=60.0,
                catch_response=True,
            ) as response:
                if response.status_code == 200:
                    for line in response.iter_lines():
                        if line:
                            decoded = line.decode("utf-8") if isinstance(line, bytes) else line
                            if decoded.startswith("data: ") and decoded != "data: [DONE]":
                                if ttft_s is None:
                                    ttft_s = time.time() - start_time
                    ok = (ttft_s is not None)
                    latency_s = time.time() - start_time
                    response.success()
                else:
                    response.failure(f"Status {response.status_code}")
                    latency_s = time.time() - start_time
        except Exception as e:
            latency_s = time.time() - start_time
            ok = False

        if ttft_s is None:
            ttft_s = latency_s

        # Custom event for Locust UI/CSV
        events.request.fire(
            request_type="POST_stream",
            name="/v1/chat/completions:ttft",
            response_time=int(ttft_s * 1000),
            response_length=0,
            exception=None if ok else Exception("Request failed"),
            context=None,
        )

        REQUEST_RECORDS.append({
            "start": round(start_time, 4),
            "ttft_s": round(ttft_s, 4),
            "latency_s": round(latency_s, 4),
            "ok": int(ok),
        })


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    out_csv = os.path.join(RESULTS_DIR, "requests.csv")
    with open(out_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["start", "ttft_s", "latency_s", "ok"])
        writer.writeheader()
        writer.writerows(REQUEST_RECORDS)
    print(f"[Locust] Wrote {len(REQUEST_RECORDS)} request rows to {out_csv}")


class RelayTrafficShape(LoadTestShape):
    """
    Traffic shapes per CLAUDE.md:
      - steady: 8 users for 600 s
      - ramp: 2 rising evenly to 60 users over 600 s
      - spike: 4 users (0-120s), 60 users (120-420s), 4 users (420-720s)
      - calibrate: reads USERS and DURATION
    """
    shape_name = os.environ.get("SHAPE", "steady").lower()
    calibrate_users = int(os.environ.get("USERS", 16))
    calibrate_duration = int(os.environ.get("DURATION", 120))

    def tick(self):
        run_time = self.get_run_time()

        if self.shape_name == "steady":
            if run_time > 600:
                return None
            return (8, 4)

        elif self.shape_name == "ramp":
            if run_time > 600:
                return None
            # 2 users at t=0 rising to 60 users at t=600
            current_users = int(2 + (58 * (run_time / 600.0)))
            return (current_users, 4)

        elif self.shape_name == "spike":
            if run_time > 720:
                return None
            if run_time < 120:
                return (4, 4)
            elif run_time <= 420:
                return (60, 20)
            else:
                return (4, 10)

        elif self.shape_name == "calibrate":
            if run_time > self.calibrate_duration:
                return None
            return (self.calibrate_users, 4)

        return None
