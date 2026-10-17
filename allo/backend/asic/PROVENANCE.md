# Provenance of this flow

This directory is Julian Bushlow's mflowgen ASIC flow, vendored from
[`jbushlow/allo-asic`](https://github.com/jbushlow/allo-asic) at commit
`e903e36` on 2026-09-24. It is Apache-2.0; `LICENSE` is his, unmodified. The
import kept its history, so `git log allo/backend/asic` credits its author
across 103 commits.

## Local compiler-integration work after the import

The local working tree reconciles this vendored flow with Julian's newer Allo
compiler work. These changes are separate from the `e903e36` flow import:

- The staged Vitis extractor and RTL canonicalizer have been consolidated into
  `allo/backend/asic_manifest.py` and their standalone files removed. The
  existing Vitis `csyn` completion hook now joins compiler identity to emitted
  HLS behavior, synthesis context, Vitis processes, RTL hierarchy, and exact
  diagnostic RTL fingerprints before writing the final manifest.
- `allo/ir/visitor.py`, `allo/ir/infer.py`, `allo/customize.py`, and
  `allo/ir/builder.py` now produce explicit per-instance compiler metadata:
  kernel name, PID, mapping, parent region, specialization suffix, predicate
  tag, and selected meta-branch trace.
- `allo/ir/pid_generalization.py` and narrow compiler plumbing restore the
  opt-in `identity_only` and `all_operations` PE/PID-generalization policies.
  The policy and axes survive interface rewriting and participate in the
  pre-HLS implementation contract.
- Dataflow interface rebuilding preserves compiler identity, and
  `allo/backend/asic_manifest.py` consumes it directly. The same module now
  records canonical specialized-MLIR/interface contracts, candidate classes,
  stream access semantics, complete backend-neutral RTL hierarchy, and exact
  MLIR/HLS/RTL debug hash inputs. Its JSON and Tcl outputs carry the same full
  schema.
- Focused metadata, PID-generalization, manifest, ordinary simulator, node
  configuration, and backend source-emission regressions pass. The supported
  SystemC path now emits generalized PID scalar ports, binds them to constant
  instance coordinates, and enriches the common manifest from Catapult's
  self-contained RTL.

This integration keeps Sunwoo's single `asic_manifest.py` API and validation
boundary while restoring compiler-owned identity and backend-specific Vitis
and SystemC/Catapult evidence behind one common schema.

## Integrated Vitis validation

The October 6, 2026 fixed 4-by-4 Vitis pipe cleaner exercised the integrated
path from the Allo specification through a licensed `csyn` and the complete
two-stage physical flow. Its final schema-version-2 manifest contains 19
matched post-HLS PE records, zero unmatched/ambiguous or unjoined records,
seven proven implementation classes, and 119 parsed RTL module definitions.
The same artifact drove workload freezing, testbench generation, RTL
normalization, macro planning and hardening, full-chip assembly, synthesis,
and PNR.

FFGL and the BAGL functional workload passed with no DUT unknowns. Innovus
reported zero connectivity/DRC violations and clean signoff timing; Calibre
reported zero non-antenna DRC results and correct LVS. This is end-to-end
integration evidence, not a claim of final signoff: Calibre antenna results,
SDF coverage warnings, and 45 BAGL runtime setup/hold checks remain documented
deferred issues.

## Integrated SystemC/Catapult validation

The October 7, 2026 SystemC/Catapult run exercised the same hierarchical flow
through SystemC/Connections emission, Catapult synthesis, manifest enrichment,
RTL simulation, macro hardening, full-chip assembly and P&R, FFGL, BAGL, DRC,
LVS, activity extraction, and power. Both gate-level simulations passed, all
15 macro instances received SDF in BAGL, and LVS was correct. The remaining
antenna, SDF-warning, and PrimeTime-versus-Innovus hold issues are documented
as deferred physical-flow limitations rather than integration failures.

The maintained entry point is now the hierarchical `allo-*` flow. It consumes
Allo-level architecture — unit boundaries, semantic equivalence classes,
stream bundles with widths and directions, and PE grid coordinates — through
an `asic_manifest` configuration passed into Allo's own `build()`. The older
flat RTL nodes remain available under `nodes/legacy-flat-flow/` only for the
historical TinyTPU constructors; they are not part of the maintained Allo
tutorial or hierarchical graph.

## What was deliberately not imported

**The FreePDK45/NanGate ADK payload** (`adks/freepdk-45nm/pkgs`, `view-tiny`):
17 files, 7 MB, whose headers carry Nangate copyright and "provided pursuant to
a License Agreement" language. Redistributing them is not ours to do, so they
were removed from the imported history as well as from the tree. Nothing is
lost: the ADK node fetches the standard view at run time, that view is the only
one DC can use — `view-tiny` has no compiled `stdcells.db` and no
`rtk-tech.tf`, so DC emits a GTECH netlist and fails the node's own
postcondition — and `tools/preflight.py` verifies what was fetched against the
`stdcells.db` md5 recorded in every run's settings snapshot.

**Designs that needed an Allo fork the original import was not on**, and
`allo-rebuild/`:
`allo-test`, `allo-mininpu-v2`, `allo-scaling-eval`, `allo-eva-*`,
`AlloSystemC-channel`, `TestNPU-*`, and `tutorial-vvadd` (whose RTL was never in
the repo — it is a Drive download). They were not part of the `e903e36`
history import. Local integration designs added later, including the fixed
pipe cleaner and scaling/Catapult workspaces, are new validation material and
should not be mistaken for files from that vendored commit.

The original `designs/GcdUnit` smoke design was retired when the integrated
tree was narrowed to maintained Allo examples. `designs/template-design`
remains the user-facing skeleton, alongside fixed Vitis and SystemC examples,
the scaling evaluation, and the retained EVA validation designs.

## Known defects in the imported legacy flat flow

Both were hit and diagnosed during the TinyTPU/Gemmini area runs; neither is
fixed here, so that the vendored code still matches its upstream:

1. **The ADK node is not idempotent against a repository checkout.**
   `adks/freepdk-45nm/configure.yml` sets `sandbox: False` and runs
   `mv {adk_view}/adk.tcl {adk_view}/adk-base.tcl`, so with a view that lives in
   the tree a second run renames the overlay onto its own base and `adk.tcl`
   sources itself — DC then dies with "too many nested evaluations". With
   `view-standard` the view is unpacked into the build directory and the tree is
   untouched, which is the configuration we use.
2. **`legacy-flat-flow/sv2v-design-collector` rejects a Vitis top module.** Its top-module check
   does not match a `module` declaration carrying a `(* CORE_GENERATION_INFO *)`
   attribute, which is what Vitis HLS emits. The TinyTPU designs set
   `normalize_rtl: False` and pass Verilog-2001 through unchanged, which avoids
   it.

## Tests

The node library's tests are run per node directory because several share a
basename and several assume the working directory is their own node. The
October 16 closeout pass ran 147 maintained and legacy node tests plus 41
compiler/manifest tests successfully. Both supported backends are covered; the
legacy flat-node tests remain local to `nodes/legacy-flat-flow/`.

`nodes/allo-asic-compilation` formerly failed because it imported `allo` and
expected an `asic_manifest` emitter this fork did not have.
`allo/backend/asic_manifest.py` is now that emitter
(`docs/source/backends/asic_manifest.rst`), and both the Vitis and supported
SystemC-to-Catapult compilation contracts are covered by focused tests plus
real end-to-end runs. Direct `backend=catapult` remains compatibility code,
not a supported ASIC integration path.

```bash
cd allo/backend/asic/nodes/<node> && python -m pytest -q .
```
