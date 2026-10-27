<!--- Copyright Allo authors. All Rights Reserved. -->
<!--- SPDX-License-Identifier: Apache-2.0  -->

# Allo build node

This node loads a parameterized Allo Python design and calls its configured
entrypoint with:

```python
build(project, target, mode, configs)
```

The node has explicit `vitis`, `catapult`, and `systemc` compilation branches.
The supported new Catapult pathway is `backend=systemc`: Allo emits SystemC
with Connections channels, Catapult is the synthesis engine, and the stable RTL
handoff is `concat_rtl.v`. The direct `catapult` branch is retained as existing
compatibility code; extending that pure-Catapult path is outside the current
integration scope. Backend selection is handled in `backend.py`; later backends
can add their own target, configuration, tool checks, artifact discovery, and
validation without adding backend-specific path parameters to the graph. The
complete integrated manifest contract is validated end to end for both Vitis
and the supported SystemC-to-Catapult path. Each branch runs synthesis,
validates its backend-specific evidence in the common enriched ASIC manifest,
and publishes the complete Allo project, debug artifacts, manifests, and
synthesis RTL.

The current SystemC-to-Catapult qualification status and ordered remaining work
are recorded in `../../SYSTEMC_CATAPULT_NOTES.md`.

Important parameters are:

- `allo_design_file`: absolute path, or a path relative to the design's
  `construct_path`.
- `allo_entrypoint`: design-module callable, default `build`.
- `backend`: `vitis`, `catapult`, or `systemc`; unsupported values fail explicitly.
- `backend_options`: shell-safe comma-separated `key=value` options interpreted
  by the selected backend branch. The Vitis default is `device=u280`.
- `build_mode`: currently expected to be `csyn`.
- `macro_clock_period`: HLS and hardened-macro target period in nanoseconds.
  The SystemC path passes the period directly to Catapult so fractional periods
  are not lost in a frequency conversion.
- `clock_period`: full-chip target period in nanoseconds. It must be greater
  than or equal to `macro_clock_period`, ensuring macros are built for a clock
  at least as fast as the chip that instantiates them.
- `python_bin`: Python executable, default `python`.
- `allo_setup_script`: shell script sourced before preflight and compilation to
  configure the prebuilt LLVM backend. It defaults to
  `/work/shared/common/allo/setup-llvm-main.sh`.
- `allo_testbench_enabled`: when true, freeze a design-supplied workload after
  HLS succeeds while the same Allo environment and design parameters remain
  active.
- `allo_testbench_workload_factory`: design-module callable returning the
  backend-independent call signature, initial argument arrays, and expected
  output arrays. It defaults to `testbench_workload`.
- `allo_testbench_top_function`: fallback top-function name if the workload
  does not provide one.

The frozen outputs are `workload-manifest.json` and `workload-vectors/`.
Array dtype, shape, element width, and count are recorded in JSON; values are
serialized as one hexadecimal element per line. This means downstream
testbench generation does not receive design-only parameters such as array
size, FIFO depth, or dtype—it consumes the realized workload and HLS outputs.

`python_bin` and `allo_setup_script` are node parameters, not hidden global
settings. Maintained constructors resolve the host-facing `ALLO_PYTHON` and
`ALLO_SETUP_SCRIPT` environment variables, validate those paths, and pass the
resolved values into these two parameters. For example:

```bash
export ALLO_PYTHON=/home/user/.conda/envs/allo/bin/python
export ALLO_SETUP_SCRIPT=/work/shared/common/allo/setup-llvm-main.sh
```

The generated build records the resolved parameter values in its mflowgen
configuration. Running inside the Allo checkout does not implicitly select the
correct interpreter or LLVM setup, and generated builds should remain outside
the `allo/` package directory.

The success marker is written only after the pre-HLS/final manifests, zero
unmatched joins, debug directory, synthesized Verilog, and frozen workload
interface have all been validated. Catapult publishes a compact RTL contract:
the self-contained `concat_rtl.v`, available reports/constraints, and selected
SCVerify protocol sources. SystemC synthesis uses Catapult from the project's
`build/` subdirectory and publishes the same stable filename, normalizing
`rtl.v` to `concat_rtl.v` when necessary. The complete project remains available under
`allo-build` but is not duplicated into `backend-rtl`.

For both supported paths, the final manifest is produced by the same
`allo.backend.asic_manifest` API as the pre-HLS manifest. Vitis joins explicit
compiler PE identity to emitted-HLS behavior, synthesis context, process names,
and parsed RTL hierarchy. SystemC joins emitted `SC_MODULE` kernels and exact
memory-pin protocols to Catapult's self-contained RTL. Specialized MLIR plus
emitted HLS remains the macro-equivalence authority; canonical RTL is retained
as an audit. Optional debug output contains the exact canonical MLIR, HLS, and
RTL strings used by the recorded hashes.

This mflowgen version does not expand node parameters inside assertion text.
Parameterized environment checks therefore run in `preflight.py`, invoked as
the first expanded command. Static mflowgen preconditions only verify that the
node's required scripts were staged. The preflight checks the selected Python
executable, Allo import, design path, build mode, backend selection, and backend
tool availability before `run.sh` starts. SystemC additionally requires a valid
`SYSTEMC_HOME`; `run.sh` defaults it to `$MGC_HOME/shared` after loading Catapult.

Preflight and compilation run from the same `run.sh` command, so the backend
module and LLVM setup are each loaded only once. Backend selection owns module
selection: the Vitis branch loads `xilinx-2022.1`, while Catapult and SystemC
load `catapult`; unsupported backends error. The wrapper disables Bash
`nounset` while sourcing because the shared setup script unsets and subsequently
reads `PYTHONPATH`.

The wrappers also clear inherited `LD_LIBRARY_PATH`, `LIBRARY_PATH`, compiler
include variables, `GCC_EXEC_PREFIX`, and `LD_PRELOAD` before sourcing the
Allo setup. This prevents commercial ASIC tool modules—especially Synopsys's
bundled `libstdc++.so.6`—from overriding the GCC 13 runtime required by Allo's
compiled MLIR extension. `PATH` is retained so a loaded backend such as Vitis
remains discoverable. The Catapult branch instead loads the `catapult` module
and verifies `MGC_HOME`.
