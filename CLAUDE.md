# allo — Coding Agent Notes

## Quick pitfalls

Full list with symptoms and fixes: `notes/ALLO_GOTCHAS.md`.

- **`~x` is silently WRONG** — use `(0 - x)`. Corrupted a one-hot grant with no error.
- **`UInt(N)` locals read back signed** — values ≥ 2^(N-1) flip sign; every width needs one spare bit.
- **Unused results are deleted** — `MemRefDCE.cpp:28` drops any result-producing op with no uses (the `try_put` ok flag, `memref.atomic_rmw`). Always consume the result.
- **A `Wire` has no synchronisation** — zero storage AND zero alignment; reads garbage unless both kernels are cycle-locked.
- **Wrong `LLVM_BUILD_DIR`** — the conda `allo` env sets it already (`build-rhel8`); overriding with `build/` → GLIBC_2.33 crash.
- **OMP segfault at exit** — GC races OMP teardown; set `OMP_NUM_THREADS=N`, run regions in separate processes.
- **Scalar `@df.region()` args** — bare `int32` in `args=[...]` is **rejected** (PR #577); use `int32[1]` → `m_axi`.
- **`Wire`/`Channel` are SystemC-only** — `hls.py:293` raises `NotImplementedError` for any other target.

## Environment

```bash
conda activate allo
# LLVM_BUILD_DIR already set in conda env (don't override)
export OMP_NUM_THREADS=8   # required for multi-kernel simulator
export PYTHONPATH=/home/zsm9/allo_sup   # else the import grabs installed /home/zsm9/allo
```

## Golden test for dataflow simulator

```bash
OMP_NUM_THREADS=8 conda run -n allo python tests/dataflow/test_df_unit.py
OMP_NUM_THREADS=8 conda run -n allo python tests/dataflow/test_region_stateful.py
```

## Project state

Live state is judged from git/GitHub/notes, not a checked-in status file:
`git branch -vv`, `gh pr list -R cornell-zhang/allo`,
`gh issue list -R sunwookim028/allo`, and `notes/`.

- Living feature map (fork vs upstream): the pinned fork issue
  https://github.com/sunwookim028/allo/issues/13. It is the single living
  picture of what the fork carries vs upstream; project state is otherwise
  judged from git/GitHub, not checked-in `.md` snapshots.
- Fork-local file inventory: fork issue #5
  (https://github.com/sunwookim028/allo/issues/5#issuecomment-4977128476).
- `notes/MAINTENANCE_CHECKLIST.md`: the upstream-merge procedure.

## Notes from AGENTS.md

See `AGENTS.md` for build instructions, testing, and code style guidelines.
