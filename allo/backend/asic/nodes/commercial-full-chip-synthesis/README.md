<!--- Copyright Allo authors. All Rights Reserved. -->
<!--- SPDX-License-Identifier: Apache-2.0  -->

# Commercial full-chip synthesis

This is a minimal adaptation of the retained
`legacy-flat-flow/synopsys-dc-synthesis` node.
It reads the assembled full-chip RTL and chip constraints, adds every hardened
macro DB to `link_library` (never `target_library`), checks that each canonical
macro has linked instances, marks those instances `dont_touch`, and otherwise
uses the existing DC compile and reporting scripts unchanged.

The default is non-topographical because hardened macro physical abstracts are
LEF/GDS inputs to Innovus rather than Milkyway reference libraries for DC.
