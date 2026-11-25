#!/usr/bin/env bash
# Run every run of one study in order, skipping runs that are already complete. COSTS MONEY: hours of GPU time.
source "$(dirname "$0")/common.sh"

usage() {
  cat <<'USAGE'
Usage: scripts/gcp/run_study.sh <main|warm|sweep> [--dry-run]

  main   strategies a and b x patterns steady ramp spike x 3 repeats = 18 runs (about 9 to 10 hours)
  warm   strategy b, spike, 0 or 1 spare GPU node x 3 repeats        =  6 runs (about 3.5 hours)
  sweep  strategy b, spike, thresholds 2 5 10 x 2 repeats             =  6 runs (about 3.5 hours)

With A100 MIG slices (GPU_PARTITION_SIZE set) every replica shares one node, so a spare node changes
nothing and warm refuses to run unless --force is given.

Order: repeat by repeat, and the order of the compared settings flips on every pair (a then b,
then b then a, ...), so slow drift in the cloud does not favour one side.
A run is skipped if its folder has meta.json. The study stops at the first failed or partial run
so a person can look at it; delete that folder and rerun this script to continue.
  --dry-run  print the plan and which runs are done, then exit. Touches nothing.
  --force    run warm even with MIG slices
USAGE
}
handle_help "$@"
[[ $# -ge 1 ]] || { usage; exit 1; }
study="$1"
dry_run=0 force=0
for arg in "${@:2}"; do
  case "$arg" in
    --dry-run) dry_run=1 ;;
    --force) force=1 ;;
    *) die "unknown argument $arg (see --help)" ;;
  esac
done

# Each line: strategy pattern repeat [extra args for run_experiment.sh]
plan() {
  local r p i=0 first second
  case "$study" in
    main)
      for r in 1 2 3; do
        for p in steady ramp spike; do
          if (( i % 2 == 0 )); then first=a second=b; else first=b second=a; fi
          echo "$first $p $r"; echo "$second $p $r"
          i=$(( i + 1 ))
        done
      done ;;
    warm)
      for r in 1 2 3; do
        if (( r % 2 == 1 )); then echo "b spike $r --spare 0"; echo "b spike $r --spare 1"
        else echo "b spike $r --spare 1"; echo "b spike $r --spare 0"; fi
      done ;;
    sweep)
      for r in 1 2; do
        if (( r == 1 )); then first="2 5 10"; else first="10 5 2"; fi
        for t in $first; do echo "b spike $r --threshold $t"; done
      done ;;
    *) die "study must be main, warm or sweep" ;;
  esac
}

# Same folder naming as run_experiment.sh.
run_dir() {
  local strategy="$1" pattern="$2" repeat="$3" flag="${4:-}" value="${5:-}" variant=""
  case "$flag" in
    --spare) variant="spare${value}" ;;
    --threshold) variant="t${value}" ;;
  esac
  echo "${REPO_ROOT}/results/gcp/${study}/${strategy}_${pattern}_r${repeat}${variant:+_${variant}}"
}

pattern_seconds() { case "$1" in spike) echo 720 ;; *) echo 600 ;; esac; }
OVERHEAD_S=480  # reset, strategy switch, loadgen refresh, 180 s cool down, export: first guess, refined below

runs=()
while IFS= read -r line; do runs+=("$line"); done < <(plan)   # not mapfile: macOS ships bash 3.2
total=${#runs[@]}
todo=()
for line in "${runs[@]}"; do
  # shellcheck disable=SC2086
  dir=$(run_dir $line)
  if [[ -f "${dir}/meta.json" && ! -f "${dir}/FAILED" ]]; then
    state="done"
  elif [[ -d "$dir" && -n "$(ls -A "$dir")" ]]; then
    state="PARTIAL"
  else
    state="todo"; todo+=("$line")
  fi
  printf '  %-8s %s\n' "$state" "${dir#"${REPO_ROOT}/"}"
  [[ "$state" == "PARTIAL" && "$dry_run" -eq 0 ]] && die "partial run at ${dir}. Inspect it, delete it, then rerun."
done
log "study ${study}: ${total} runs, $(( total - ${#todo[@]} )) done, ${#todo[@]} to go"
[[ "$dry_run" -eq 1 ]] && exit 0
[[ ${#todo[@]} -gt 0 ]] || { log "nothing to do"; exit 0; }

load_config
if [[ "$study" == "warm" && -n "$GPU_PARTITION_SIZE" && "$force" -eq 0 ]]; then
  die "warm spare does not apply with MIG slices (GPU_PARTITION_SIZE=${GPU_PARTITION_SIZE}): all replicas share one node. Use --force to run it anyway."
fi
study_start=$(date +%s)
spent_s=0
planned_s=0
n=0
for line in "${todo[@]}"; do
  n=$(( n + 1 ))
  # Estimated time left: planned seconds for remaining runs, scaled by how long runs have really taken.
  remaining=0
  for rest in "${todo[@]:$(( n - 1 ))}"; do
    remaining=$(( remaining + $(pattern_seconds "$(echo "$rest" | cut -d' ' -f2)") + OVERHEAD_S ))
  done
  if (( planned_s > 0 )); then remaining=$(( remaining * spent_s / planned_s )); fi
  log "[$n/${#todo[@]}] ${line}   about $(( remaining / 60 )) min left for this study"
  t0=$(date +%s)
  # shellcheck disable=SC2086
  "${GCP_SCRIPTS}/run_experiment.sh" "$study" $line
  spent_s=$(( spent_s + $(date +%s) - t0 ))
  planned_s=$(( planned_s + $(pattern_seconds "$(echo "$line" | cut -d' ' -f2)") + OVERHEAD_S ))
done
log "study ${study} finished in $(( ($(date +%s) - study_start) / 60 )) min"
log "If you are done for the day: scripts/gcp/scale_gpu_zero.sh"
