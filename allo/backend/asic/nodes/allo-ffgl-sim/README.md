<!--- Copyright Allo authors. All Rights Reserved. -->
<!--- SPDX-License-Identifier: Apache-2.0  -->

# Allo functional gate-level simulation

This node runs the generated self-checking Allo testbench against the mapped
full-chip synthesis netlist with VCS zero-delay semantics. By default,
`ffgl_powerup_mode=random` initializes sequential state beneath the DUT to
random binary values while leaving testbench state untouched. Set
`ffgl_powerup_seed` to a reproducible integer of at least 2; VCS reserves 0 and
1 for uniform initialization. The alternate `x` mode preserves strict
four-state startup and enables `xprop=tmerge`; `zero` and `one` are available
as diagnostic corner modes. VCS does not support `initreg` and X-propagation
together, so the node selects exactly one strategy for each run. The selected
mode and seed are recorded in `simulation-report.json`.

It recursively loads Verilog/SystemVerilog models from optional `srams/` and
`macro-registry/` directory inputs; a missing or empty optional directory is
silently skipped. LVS and power-ground netlist variants are excluded so they do
not duplicate the functional macro definitions. Success requires the
testbench's `ALLO_TEST_PASS` marker and a nonempty VCD.
