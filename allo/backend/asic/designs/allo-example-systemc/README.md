# Allo 4x4 SystemC/Catapult ASIC example

This is a standalone copy of the fixed Vitis pipe cleaner configured for:

`Allo dataflow -> SystemC/Connections -> Catapult -> hierarchical ASIC flow`

It computes one signed 8-bit `4x4 * 4x4` matrix multiplication with a 4x4
systolic compute grid and depth-4 streams. The deterministic workload uses
bounded signed values so every accumulated result is exactly representable in
8 bits. The source, workload, and expected result remain independent of the
Vitis design after this copy.

The construct selects `backend=systemc`, `build_mode=csyn`, the Nangate 45 nm
Catapult library, and identity-only PID generalization. The latter keeps the 16
compute instances in one reusable implementation class while passing each
instance's coordinates through constant scalar ports. `min_macro_reuse=2`
keeps the three single-use boundary kernels out of macro hardening.

The graph resolves its two Allo runtime dependencies independently. Set these
variables before graph generation when the site defaults do not match your
installation:

```bash
export ALLO_PYTHON=/path/to/your/allo-environment/bin/python
export ALLO_SETUP_SCRIPT=/path/to/your/allo/setup-llvm.sh
```

The compilation node loads the Catapult environment module and requires it to
provide `catapult` and `MGC_HOME`. It derives `SYSTEMC_HOME` from
`$MGC_HOME/shared` unless it is already set.

Generate the mflowgen build outside the importable `allo` package:

```bash
mkdir -p /path/to/builds/allo-example-systemc
cd /path/to/builds/allo-example-systemc
conda activate asicflow
mflowgen run --design \
  /path/to/allo/allo/backend/asic/designs/allo-example-systemc/construct-commercial.py
```

The compilation node publishes `backend-rtl/concat_rtl.v`, both manifest
stages, the fixed workload vectors, and a generated SystemC-family RTL
testbench. The maintained end-to-end run has exercised those artifacts through
the complete hierarchical flow.

Before launching, rebuild the unified Allo checkout in the interpreter named by
`ALLO_PYTHON` so the native extension contains the updated SystemC emitter:

```bash
cd /home/jb2698/test/sunwoo-allo-test
source "$ALLO_SETUP_SCRIPT"
"$ALLO_PYTHON" -m pip install -v -e .
```
