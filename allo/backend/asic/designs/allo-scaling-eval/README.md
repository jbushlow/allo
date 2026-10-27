# Allo shared-PE scaling evaluation

This parameterized design computes
`A[P,K] * B[K,P] -> C[P,P]` with a `P x P` systolic compute array. The two
loaders and the result consumer each use `mapping=[1]`, so they remain single,
non-reused modules. Only the compute kernel uses `mapping=[P,P]`; consequently
the intended shared compute-PE population is exactly `P^2`.

The commercial graph exposes these experiment parameters:

- `allo_array_size` (`P`, default 4)
- `allo_reduction_size` (`K`, default 4)
- `allo_dtype_bits` (`Int(bits)`, default 32)
- `allo_fifo_depth` (default 4)

The primary reuse sweep should hold `K`, datatype, clocks, FIFO depth, and all
physical-flow settings constant while varying `P`. The arithmetic-complexity
sweep should hold `P` and `K` constant while varying only `allo_dtype_bits`.
The deterministic `workload()` uses small values that do not overflow any of
the planned `Int(16)`, `Int(32)`, or `Int(64)` configurations.

`construct-commercial.py` contains the complete explicit commercial graph:
Allo/Vitis compilation, RTL normalization, Stage 1 macro planning, synthesis,
PNR, physical verification, signoff and publication, followed by Stage 2
assembly, full-chip synthesis, physical intent, PNR, GDS merge, DRC, LVS, and
summary reporting. A flat comparison must consume the same normalized HLS RTL,
use the same full-chip clock and DC/Innovus settings, and omit only Stage 1
hardening plus Stage 2 macro substitution. 
