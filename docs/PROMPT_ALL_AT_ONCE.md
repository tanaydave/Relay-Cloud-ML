# Claude Code: build everything in one go

Use this to generate the whole codebase in one session. It is faster, but harder to debug: afterwards, still test each piece in the order of `docs/PROMPTS_STEP_BY_STEP.md` before running real experiments.

Tips:
- Start Claude Code in the repo root so it loads `CLAUDE.md` automatically.
- Let it work through the checklist; approve file writes, but deny any command that creates cloud resources or submits Slurm jobs.
- When it finishes, commit, then test from Step 1 onward.

---

## The prompt

```
You are building the complete Relay codebase. Read CLAUDE.md, docs/RUN_ON_GCP.md, docs/RUN_ON_NYU_HPC.md and docs/PROMPTS_STEP_BY_STEP.md fully before writing anything. CLAUDE.md is the source of truth; follow its scope, layout, conventions and safety rules exactly.

Build everything described in Prompts 0 to 8 of docs/PROMPTS_STEP_BY_STEP.md, in that order, as one continuous job. Work like this:

1. First, write a checklist in docs/BUILD_CHECKLIST.md with one line per file you will create, grouped by prompt number. Keep it updated as you go.
2. For each prompt in order:
   a. Write every file it asks for, completely. No placeholders, no TODOs, except values that must come from config/relay.env.
   b. Run only safe local checks: bash -n on every shell script, shellcheck if available, python -m py_compile on every Python file, and yaml parsing on every manifest after envsubst with the example config.
   c. For the load test, start loadtest/mock_server.py and run Locust against it for 30 seconds with SHAPE=calibrate USERS=5 DURATION=30 and confirm requests.csv is written correctly.
   d. For analysis, generate a small synthetic results tree under results/synthetic/ and confirm every chart renders. Delete it afterwards or keep it clearly labeled as synthetic.
   e. Tick the item in docs/BUILD_CHECKLIST.md.
3. Never run gcloud, kubectl, helm, crane, sbatch, srun or scancel. Never create, resize or delete cloud resources. Never submit Slurm jobs. Those are for humans to run.
4. Keep docs/FILES.md current: every file with 2 to 3 lines on what it does and how it is used.
5. At the end, print:
   a. The full file tree.
   b. A numbered run order for GCP, from create_cluster.sh to make_charts.py.
   c. A numbered run order for NYU Torch, from download_model.sh to make_charts.py.
   d. Every assumption you made and anything that must be verified on real infrastructure, especially metric names, image tags, and Slurm options.
   e. A suggested split of these files across 3 teammates: cloud and capacity, serving and autoscaling, load metrics and analysis.

If anything in the docs conflicts, follow CLAUDE.md and tell me about the conflict at the end.
```
