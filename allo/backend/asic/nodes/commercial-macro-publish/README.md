<!--- Copyright Allo authors. All Rights Reserved. -->
<!--- SPDX-License-Identifier: Apache-2.0  -->

# Commercial macro publish

This node is the Stage 1 boundary. It rejects missing or empty required views,
checks each class against the final Allo manifest, records SHA-256 checksums,
and publishes JSON and dependency-free Tcl registries for Stage 2.
