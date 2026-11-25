#!/usr/bin/env python3
"""Send one streaming chat request to an OpenAI compatible endpoint and print the answer and TTFT.

Standard library only, so it runs anywhere: laptop, Cloud Shell, a Torch job.
    python3 loadtest/smoke_test.py --url http://127.0.0.1:8000 --model Qwen/Qwen2.5-1.5B-Instruct
Exit code 0 means the server answered with at least one token.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://127.0.0.1:8000", help="base URL, without /v1")
    p.add_argument("--model", required=True, help="served model name, MODEL_ID")
    p.add_argument("--prompt", default="Say hello and name one fact about New York City.")
    p.add_argument("--max-tokens", type=int, default=32)
    p.add_argument("--timeout", type=float, default=60.0)
    return p.parse_args()


def stream_chat(url: str, model: str, prompt: str, max_tokens: int, timeout: float) -> tuple[str, float, float]:
    """Return (answer, ttft_s, latency_s). TTFT is measured at the first chunk with content."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
    }).encode()
    req = urllib.request.Request(f"{url.rstrip('/')}/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    start = time.perf_counter()
    ttft = float("nan")
    parts: list[str] = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            choices = json.loads(data).get("choices") or [{}]
            text = (choices[0].get("delta") or {}).get("content") or ""
            if text and ttft != ttft:  # ttft is still NaN
                ttft = time.perf_counter() - start
            parts.append(text)
    return "".join(parts), ttft, time.perf_counter() - start


def main() -> int:
    args = parse_args()
    try:
        answer, ttft, latency = stream_chat(args.url, args.model, args.prompt, args.max_tokens, args.timeout)
    except Exception as exc:  # noqa: BLE001, show any failure plainly
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"answer: {answer.strip()}")
    print(f"ttft_s: {ttft:.3f}")
    print(f"latency_s: {latency:.3f}")
    if not answer:
        print("FAILED: no tokens received", file=sys.stderr)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
