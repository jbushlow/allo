# Legacy flat RTL flow nodes

These nodes support historical constructors that consume an existing RTL file
set rather than compiling an Allo design and emitting the hierarchical ASIC
manifest. They are retained for the TinyTPU standard-cell synthesis and
reduction experiments under `examples/tinytpu/`.

Maintained Allo ASIC designs must use the hierarchical nodes that are direct
children of `allo/backend/asic/nodes/`. The legacy constructors combine the
flat-only collector and implementation nodes in this directory with shared
top-level nodes such as `sram-collateral` and `asic-flow-utilities`.

The directory is intentionally isolated: adding a new backend or hierarchical
Allo feature must not create a dependency on these nodes.
