#!/usr/bin/env python3
"""Fake vLLM server for testing Locust, the HPC router and the HPC autoscaler without a GPU.

Standard library only. Implements:
  POST /v1/chat/completions  streaming (SSE) or not; emits max_tokens fake tokens
  GET  /health               200 OK
  GET  /metrics              vllm:num_requests_waiting, vllm:num_requests_running,
                             vllm:time_to_first_token_seconds histogram, process_cpu_seconds_total
At most --max-seqs requests run at once; the rest wait, which drives num_requests_waiting,
so a queue builds when users exceed --max-seqs, as with vLLM --max-num-seqs.

    python3 loadtest/mock_server.py --port 8000 --token-delay 0.01 --max-seqs 8
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TTFT_BUCKETS = [0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 60.0]


class State:
    """Shared counters, guarded by one lock."""

    def __init__(self, max_seqs: int) -> None:
        self.lock = threading.Lock()
        self.slots = threading.Semaphore(max_seqs)
        self.waiting = 0
        self.running = 0
        self.ttft_counts = [0] * len(TTFT_BUCKETS)
        self.ttft_sum = 0.0
        self.ttft_n = 0

    def observe_ttft(self, seconds: float) -> None:
        with self.lock:
            self.ttft_sum += seconds
            self.ttft_n += 1
            for i, le in enumerate(TTFT_BUCKETS):
                if seconds <= le:
                    self.ttft_counts[i] += 1

    def metrics_text(self, model: str) -> str:
        with self.lock:
            lab = f'model_name="{model}"'
            lines = [
                "# TYPE vllm:num_requests_waiting gauge",
                f"vllm:num_requests_waiting{{{lab}}} {self.waiting}",
                "# TYPE vllm:num_requests_running gauge",
                f"vllm:num_requests_running{{{lab}}} {self.running}",
                "# TYPE vllm:time_to_first_token_seconds histogram",
            ]
            lines += [f'vllm:time_to_first_token_seconds_bucket{{{lab},le="{le}"}} {c}'
                      for le, c in zip(TTFT_BUCKETS, self.ttft_counts)]
            lines += [
                f'vllm:time_to_first_token_seconds_bucket{{{lab},le="+Inf"}} {self.ttft_n}',
                f"vllm:time_to_first_token_seconds_sum{{{lab}}} {self.ttft_sum:.6f}",
                f"vllm:time_to_first_token_seconds_count{{{lab}}} {self.ttft_n}",
                "# TYPE process_cpu_seconds_total counter",
                f"process_cpu_seconds_total {time.process_time():.3f}",
            ]
        return "\n".join(lines) + "\n"


def make_handler(state: State, args: argparse.Namespace) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *a: object) -> None:  # quiet by default
            if args.verbose:
                super().log_message(fmt, *a)

        def send_plain(self, code: int, body: str, ctype: str = "text/plain") -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self.send_plain(200, "")
            elif self.path == "/metrics":
                self.send_plain(200, state.metrics_text(args.model), "text/plain; version=0.0.4")
            elif self.path == "/v1/models":
                self.send_plain(200, json.dumps({"data": [{"id": args.model}]}), "application/json")
            else:
                self.send_plain(404, "not found")

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/chat/completions":
                self.send_plain(404, "not found")
                return
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            n_tokens = int(req.get("max_tokens") or 16)
            start = time.perf_counter()
            with state.lock:
                state.waiting += 1
            state.slots.acquire()
            with state.lock:
                state.waiting -= 1
                state.running += 1
            try:
                time.sleep(args.prefill_delay)
                if req.get("stream"):
                    self.stream(n_tokens, start)
                else:
                    time.sleep(args.token_delay * n_tokens)
                    state.observe_ttft(time.perf_counter() - start)
                    body = {"id": f"chatcmpl-{uuid.uuid4().hex}", "object": "chat.completion",
                            "model": args.model,
                            "choices": [{"index": 0, "finish_reason": "length",
                                         "message": {"role": "assistant", "content": "tok " * n_tokens}}]}
                    self.send_plain(200, json.dumps(body), "application/json")
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with state.lock:
                    state.running -= 1
                state.slots.release()

        def stream(self, n_tokens: int, start: float) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            cid = f"chatcmpl-{uuid.uuid4().hex}"

            def send(obj: object) -> None:
                payload = f"data: {obj if isinstance(obj, str) else json.dumps(obj)}\n\n".encode()
                self.wfile.write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")
                self.wfile.flush()

            def chunk(delta: dict, finish: str | None = None) -> dict:
                return {"id": cid, "object": "chat.completion.chunk", "model": args.model,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

            send(chunk({"role": "assistant", "content": ""}))
            for i in range(n_tokens):
                if i == 0:
                    state.observe_ttft(time.perf_counter() - start)
                send(chunk({"content": f"tok{i} "}))
                time.sleep(args.token_delay)
            send(chunk({}, "length"))
            send("[DONE]")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    return Handler


class QuietServer(ThreadingHTTPServer):
    """Ignore clients that disconnect mid request, as Locust does when it stops users."""

    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--model", default="Qwen/Qwen2.5-1.5B-Instruct")
    p.add_argument("--token-delay", type=float, default=0.01, help="seconds between streamed tokens")
    p.add_argument("--prefill-delay", type=float, default=0.05, help="seconds before the first token")
    p.add_argument("--max-seqs", type=int, default=8, help="concurrent requests; the rest wait")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    server = QuietServer((args.host, args.port), make_handler(State(args.max_seqs), args))
    print(f"mock vLLM on http://{args.host}:{args.port} max_seqs={args.max_seqs}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
