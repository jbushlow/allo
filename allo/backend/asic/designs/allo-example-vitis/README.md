# Allo 4x4 Vitis ASIC example

This is a fixed, intentionally small ASIC-flow design. It computes one signed
8-bit `4x4 * 4x4` matrix multiplication using a 4x4 systolic compute grid and
depth-4 streams. The deterministic workload uses bounded signed values so every
accumulated result is exactly representable in 8 bits.

The source has no environment-controlled dimensions, datatype, reduction size,
or FIFO depth. Its workload and expected result are fixed so Vitis synthesis,
final ASIC-manifest enrichment, generated testbenches, macro planning, and the
two-stage hierarchical flow can be exercised against a stable input.

`construct-commercial.py` is self-contained. It defines the complete two-stage
hierarchical graph for this fixed source, disables Google Sheets publication,
and sets `min_macro_reuse=2` so only repeated equivalence classes advance to
macro hardening.

The graph resolves its two host runtime dependencies independently. Set these
variables before graph generation when the site defaults do not match your
installation:

```bash
export ALLO_PYTHON=/path/to/your/allo-environment/bin/python
export ALLO_SETUP_SCRIPT=/path/to/your/allo/setup-llvm.sh
```

Always generate an mflowgen build outside the importable `allo` package, for
example under a separate build root.

```bash
mkdir -p /path/to/builds/allo-example-vitis
cd /path/to/builds/allo-example-vitis
conda activate asicflow
mflowgen run --design \
  /path/to/allo/allo/backend/asic/designs/allo-example-vitis/construct-commercial.py
```

Before using licensed Vitis, check the software behavior from the repository
root:

```bash
conda activate allo
source "$ALLO_SETUP_SCRIPT"
cd /path/to/allo
"$ALLO_PYTHON" allo/backend/asic/designs/allo-example-vitis/allo_design.py
```
