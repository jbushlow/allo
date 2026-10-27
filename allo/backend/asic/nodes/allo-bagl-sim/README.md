<!--- Copyright Allo authors. All Rights Reserved. -->
<!--- SPDX-License-Identifier: Apache-2.0  -->

# Allo back-annotated gate-level simulation

This node runs the generated self-checking Allo testbench against the
post-route VCS netlist with required SDF annotation, negative timing-check
support, and a VCD suitable for PrimeTime PX. By default,
`bagl_powerup_mode=random` initializes sequential state beneath the DUT to
random binary values while leaving testbench state untouched. Set
`bagl_powerup_seed` to a reproducible integer of at least 2; VCS reserves 0 and
1 for uniform initialization. The alternate `x` mode preserves strict
four-state startup and enables `xprop=tmerge`; `zero` and `one` are diagnostic
corner modes. VCS does not support `initreg` and X propagation together, so the
node selects exactly one strategy per run. The selected mode and seed are
recorded in `simulation-report.json`.

The node annotates both the recomputed top-level SDF and every hardened-macro
SDF using exact instance names from `macro-collateral.json`;
`sdf-annotation-manifest.json` records that mapping.

Clock source latency is extracted from `design.pt.sdc`. The testbench contract
supplies the common flow clock period, and BAGL validates it against the node
parameter. Vitis AXI BFM outputs and Catapult-family synchronous-memory read
data are driven after one half period plus the propagated-clock compensation
and input margin, keeping them stable before the next active edge. Reset,
input, and output margins remain configurable. The JSON report separates top
and macro annotation coverage, timing violations, unknown DUT behavior, and SDF
warning categories. Category policies can promote unmatched timing checks,
unmatched IOPATHs, or ignored up-hierarchy interconnect warnings independently.
`bagl_failure_policy=error` requires a completed simulation to pass the workload
and timing checks. `bagl_failure_policy=report` keeps the JSON status and failure
details unchanged but makes a completed simulation failure nonfatal, allowing the
flow-summary and spreadsheet-reporting nodes to consume it. Missing collateral,
SDF errors, incomplete top/macro annotation, and an empty VCD remain fatal in
both modes.
Optional SRAM and macro functional models are loaded recursively; LVS and
power-ground macro netlists remain excluded to avoid duplicate definitions.
