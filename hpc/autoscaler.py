import argparse
import csv
import glob
import json
import math
import os
import re
import subprocess
import time
from typing import Dict, List, Optional
import requests

timeseries_records: List[Dict[str, float]] = []
events_records: List[Dict] = []
managed_jobs: Dict[str, Dict] = {}  # job_id -> {submit_time, start_time, registry_time, healthy_time}
last_scale_down_time = 0.0


def log_event(event_type: str, job_id: str, detail: str = ""):
    rec = {
        "timestamp": time.time(),
        "event": event_type,
        "job_id": job_id,
        "detail": detail
    }
    events_records.append(rec)
    print(f"[Autoscaler Event] {event_type} - Job: {job_id} {detail}")


def submit_replica() -> Optional[str]:
    cmd = ["sbatch", "hpc/vllm_replica.sbatch"]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        # Parse: Submitted batch job 123456
        match = re.search(r"Submitted batch job (\d+)", res.stdout)
        if match:
            job_id = match.group(1)
            t_now = time.time()
            managed_jobs[job_id] = {
                "submit_time": t_now,
                "start_time": None,
                "registry_time": None,
                "healthy_time": None
            }
            log_event("SCALE_UP_SUBMIT", job_id)
            return job_id
    except Exception as e:
        print(f"[Autoscaler] Failed to submit replica: {e}")
    return None


def cancel_replica(job_id: str):
    cmd = ["scancel", job_id]
    try:
        subprocess.run(cmd, check=True)
        log_event("SCALE_DOWN_CANCEL", job_id)
        if job_id in managed_jobs:
            managed_jobs.pop(job_id, None)
    except Exception as e:
        print(f"[Autoscaler] Failed to cancel job {job_id}: {e}")


def update_job_states(registry_dir: str):
    # Check squeue for start_time
    try:
        user = os.environ.get("USER", os.environ.get("NETID", "mp7264"))
        res = subprocess.run(["squeue", "-u", user, "-o", "%i %T"], stdout=subprocess.PIPE, text=True)
        running_jobs = set()
        for line in res.stdout.strip().splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                jid, state = parts[0], parts[1]
                if state == "RUNNING" and jid in managed_jobs:
                    if managed_jobs[jid]["start_time"] is None:
                        managed_jobs[jid]["start_time"] = time.time()
                        log_event("JOB_START_RUNNING", jid)
                running_jobs.add(jid)
    except Exception:
        pass

    # Check registry files
    if os.path.isdir(registry_dir):
        for f in glob.glob(os.path.join(registry_dir, "*")):
            jid = os.path.basename(f)
            if jid in managed_jobs and managed_jobs[jid]["registry_time"] is None:
                managed_jobs[jid]["registry_time"] = time.time()
                log_event("JOB_REGISTERED", jid)


def scrape_metrics(healthy_endpoints: List[str]) -> Dict[str, float]:
    total_waiting = 0.0
    total_running = 0.0
    total_cpu_seconds = 0.0

    for ep in healthy_endpoints:
        url = f"http://ep/metrics".replace("ep", ep)
        try:
            r = requests.get(url, timeout=2.0)
            if r.status_code == 200:
                for line in r.text.splitlines():
                    if line.startswith("vllm:num_requests_waiting"):
                        val = float(line.split()[-1])
                        total_waiting += val
                    elif line.startswith("vllm:num_requests_running"):
                        val = float(line.split()[-1])
                        total_running += val
                    elif "process_cpu_seconds_total" in line and not line.startswith("#"):
                        val = float(line.split()[-1])
                        total_cpu_seconds += val
        except Exception:
            pass

    return {
        "waiting": total_waiting,
        "running": total_running,
        "cpu_seconds": total_cpu_seconds,
    }


def main():
    global last_scale_down_time
    parser = argparse.ArgumentParser(description="Relay HPC Autoscaler")
    parser.add_argument("--strategy", choices=["a", "b", "none"], default="b", help="Autoscaling strategy")
    parser.add_argument("--threshold", type=float, default=None, help="Threshold value (queue or cpu %)")
    parser.add_argument("--results-dir", type=str, default=".", help="Directory to save output timeseries.csv")
    args = parser.parse_args()

    netid = os.environ.get("NETID", os.environ.get("USER", "mp7264"))
    registry_dir = os.environ.get("RELAY_SCRATCH", f"/scratch/{netid}/relay") + "/registry"

    check_interval = int(os.environ.get("CHECK_INTERVAL_S", 15))
    scale_down_window = int(os.environ.get("SCALE_DOWN_WINDOW_S", 120))
    min_replicas = int(os.environ.get("MIN_REPLICAS", 1))
    max_replicas = int(os.environ.get("MAX_REPLICAS", 3))

    cpu_target = args.threshold or float(os.environ.get("CPU_TARGET_PERCENT", 60.0))
    queue_threshold = args.threshold or float(os.environ.get("QUEUE_THRESHOLD", 5.0))

    print(f"[Autoscaler] Strategy: {args.strategy.upper()} | Min: {min_replicas} | Max: {max_replicas} | Interval: {check_interval}s")

    last_cpu_seconds = 0.0
    last_scrape_time = time.time()

    # Discover existing replicas registered at start
    if os.path.isdir(registry_dir):
        for f in glob.glob(os.path.join(registry_dir, "*")):
            jid = os.path.basename(f)
            managed_jobs[jid] = {
                "submit_time": time.time(),
                "start_time": time.time(),
                "registry_time": time.time(),
                "healthy_time": time.time()
            }

    try:
        while True:
            t_start = time.time()
            update_job_states(registry_dir)

            # Discover healthy endpoints from router or registry
            healthy_endpoints = []
            registered_job_ids = []
            if os.path.isdir(registry_dir):
                for f in glob.glob(os.path.join(registry_dir, "*")):
                    jid = os.path.basename(f)
                    try:
                        with open(f, "r") as fp:
                            ep = fp.read().strip()
                            if ep:
                                # Quick health check
                                r = requests.get(f"http://{ep}/health", timeout=1.5)
                                if r.status_code == 200:
                                    healthy_endpoints.append(ep)
                                    registered_job_ids.append(jid)
                                    if jid in managed_jobs and managed_jobs[jid]["healthy_time"] is None:
                                        managed_jobs[jid]["healthy_time"] = time.time()
                                        log_event("JOB_FIRST_HEALTHY", jid)
                    except Exception:
                        pass

            num_ready = len(healthy_endpoints)
            metrics = scrape_metrics(healthy_endpoints)
            now = time.time()

            # Calculate CPU percent rate
            dt = max(now - last_scrape_time, 1e-3)
            cpu_delta = max(metrics["cpu_seconds"] - last_cpu_seconds, 0.0)
            # percentage across available replicas
            cpu_percent = (cpu_delta / (dt * max(num_ready, 1))) * 100.0 if num_ready > 0 else 0.0

            last_cpu_seconds = metrics["cpu_seconds"]
            last_scrape_time = now

            timeseries_records.append({
                "time": round(now, 2),
                "replicas_ready": num_ready,
                "waiting": metrics["waiting"],
                "running": metrics["running"],
                "cpu": round(cpu_percent, 2),
                "gpu_nodes": num_ready,
                "p95_ttft": 0.0  # populated by post-processing or Locust event
            })

            # Autoscaling Logic (Same math as Kubernetes HPA / KEDA)
            desired_replicas = max(num_ready, 1)

            if args.strategy == "b":
                # KEDA queue based: sum(waiting) / ready_replicas > queue_threshold
                if num_ready > 0:
                    avg_waiting = metrics["waiting"] / num_ready
                    if avg_waiting > queue_threshold:
                        desired_replicas = min(num_ready + 1, max_replicas)
                    elif avg_waiting == 0 and num_ready > min_replicas:
                        desired_replicas = max(num_ready - 1, min_replicas)

            elif args.strategy == "a":
                # HPA CPU based: ceil(current * (cpu_percent / cpu_target))
                if num_ready > 0 and cpu_percent > 0:
                    desired_replicas = math.ceil(num_ready * (cpu_percent / cpu_target))
                    desired_replicas = max(min_replicas, min(desired_replicas, max_replicas))

            # Execute scale decisions
            current_total = len(managed_jobs)
            if desired_replicas > current_total and current_total < max_replicas:
                print(f"[Autoscaler] Scaling UP: {current_total} -> {desired_replicas}")
                submit_replica()
            elif desired_replicas < num_ready and (now - last_scale_down_time) > scale_down_window:
                # Enforce scale down stabilization window
                if registered_job_ids and len(registered_job_ids) > min_replicas:
                    # Cancel newest replica
                    newest_jid = sorted(registered_job_ids)[-1]
                    print(f"[Autoscaler] Scaling DOWN: removing newest replica {newest_jid}")
                    cancel_replica(newest_jid)
                    last_scale_down_time = now

            # Sleep remaining interval
            elapsed = time.time() - t_start
            time.sleep(max(check_interval - elapsed, 1.0))

    except KeyboardInterrupt:
        pass
    finally:
        os.makedirs(args.results_dir, exist_ok=True)
        ts_path = os.path.join(args.results_dir, "timeseries.csv")
        with open(ts_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["time", "replicas_ready", "waiting", "running", "cpu", "gpu_nodes", "p95_ttft"])
            writer.writeheader()
            writer.writerows(timeseries_records)
        print(f"[Autoscaler] Saved {len(timeseries_records)} records to {ts_path}")

        events_path = os.path.join(args.results_dir, "events.json")
        with open(events_path, "w") as f:
            json.dump({"events": events_records, "jobs": managed_jobs}, f, indent=2)
        print(f"[Autoscaler] Saved timeline events to {events_path}")


if __name__ == "__main__":
    main()
