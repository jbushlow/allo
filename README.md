<!--- Copyright Allo authors. All Rights Reserved. -->
<!--- SPDX-License-Identifier: Apache-2.0  -->

<img src="tutorials/allo-icon.png" width=128/> Accelerator Design and Programming Language
==============================================================================

[**Upstream docs**](https://cornell-zhang.github.io/allo) | [**Installation**](https://cornell-zhang.github.io/allo/setup/index.html) | [**Tutorials**](https://github.com/cornell-zhang/allo-tutorials)

> **This is a research fork of [cornell-zhang/allo](https://github.com/cornell-zhang/allo).**
> It adds a **SystemC / Catapult-HLS backend** that takes an Allo dataflow design all the way
> to ASIC RTL, plus a JIT dataflow simulator. Everything below the "Upstream Allo" heading is
> unchanged from upstream. Start at [`notes/README.md`](notes/README.md) for project state.

Allo is a Python-embedded, MLIR-based language and compiler for building large-scale,
high-performance accelerators from composable parts.

---

## What this fork adds

**A SystemC emitter targeting Siemens Catapult HLS** (`target="systemc"`). An Allo
`@df.region` becomes synthesizable SystemC, which Catapult compiles to Verilog, which
Xcelium verifies bit-exact against the C simulation.

```
@df.region (Python)  ──►  MLIR  ──►  SystemC  ──►  Catapult  ──►  Verilog  ──►  Genus  ──►  gates
                                      │                            │
                                    csim                         cosim (Xcelium, bit-exact)
```

**Three link primitives, three genuinely different hardware realizations** — the link type
is a design decision, not a label:

| Allo type | RTL | cost |
|---|---|---|
| `Wire[T]` | raw `sc_in`/`sc_out` | no storage, **no synchronisation** — only safe cycle-locked |
| `Channel[T, valid_ready]` | MatchLib `Connections::Combinational` | full handshake, no buffer |
| `Channel[T, valid_only]` | `_dat` + `_vld` signal pair | valid only, no back-pressure |
| `Stream[T, depth]` | `Connections::Fifo` | buffered, full handshake |

**Three build modes** over the *same* emitted source:

| mode | what runs | proves |
|---|---|---|
| `csim` | g++ compiles + runs the SystemC on the host | functional correctness (fast) |
| `csyn` | Catapult synthesizes → Verilog | it synthesizes; area / Fmax |
| `cosim` | synthesized RTL in Xcelium vs the csim golden | RTL is bit-exact with csim |

> csim and synthesis compile **different code** (five `#ifdef __SYNTHESIS__` splits).
> A green csim does *not* prove the RTL is right — run `cosim`.

## Quick start

```bash
conda activate allo
export OMP_NUM_THREADS=8      # required for multi-kernel designs
export PYTHONPATH=$(pwd)      # else the import grabs the installed allo

# csim links against a SystemC library; Catapult ships one:
export SYSTEMC_HOME=$MGC_HOME/shared
# ...whose libsystemc needs GLIBCXX_3.4.26, newer than the system libstdc++ on
# some hosts. Without this the build compiles and then dies at run time.
export ALLO_CXX_EXTRA="-L$CONDA_PREFIX/lib -Wl,-rpath,$CONDA_PREFIX/lib"
```

A complete design — a producer and consumer joined by a handshake channel:

```python
import numpy as np
import allo.dataflow as df
from allo.ir.types import int32, Channel, valid_ready

N = 4

@df.region()
def pc_channel(A: int32[N], B: int32[N]):
    ch: Channel[int32, valid_ready]        # handshake link, no buffer

    @df.kernel(mapping=[1], args=[A])
    def producer(a: int32[N]):
        for i in range(N):
            ch.put(a[i])

    @df.kernel(mapping=[1], args=[B])
    def consumer(b: int32[N]):
        for i in range(N):
            b[i] = ch.get()

A = np.arange(N, dtype=np.int32)
B = np.zeros(N, dtype=np.int32)

mod = df.build(pc_channel, target="systemc", mode="csim", project="pc_prj")
mod(A, B)
assert (B == A).all()
```

Swap `mode="csim"` for `"csyn"` to synthesize, or `"cosim"` to check the RTL against the
csim golden.

Runnable, with MLIR/SystemC dumps: [`examples/systemc/pc_channel.py`](examples/systemc/pc_channel.py).

## Results

Genus 20.1 high effort, Nangate 45 nm, 2.0 ns, `concat_rtl.v`, 0 black boxes. Allo designs
measured against hand-written industrial references:

| design | Allo | reference | verdict |
|---|---|---|---|
| WHVCRouter | 2 cyc/step, 510 MHz, 32,415 µm² | MatchLib 1 cyc, 509 MHz, 31,784 µm² | MatchLib 2.04× per area |
| Crossbar | 1 cyc/step, 746 MHz, 6,466 µm² | MatchLib 3 cyc, 775 MHz, 6,056 µm² | **Allo 2.71× per area** |
| RaveNoC-equivalent | 2 cyc/step, 606 MHz, 10,001 µm² | RaveNoC 1 cyc, 567 MHz, 7,857 µm² | RaveNoC 2.38× per area |

The NoC evaluation itself lives in a **separate repo**, `final_noc`.

## Repository map

| Path | What |
|---|---|
| `mlir/lib/Translation/EmitSystemC.cpp` | the SystemC emitter (~3,000 lines) |
| `mlir/lib/Translation/EmitSystemC.md` | layered walkthrough of that emitter |
| `allo/backend/{hls,catapult}.py` | build flow, Catapult TCL generation |
| `examples/systemc/` | runnable SystemC examples + per-example verdicts |
| `tests/dataflow/` | the dataflow suite (24 of 30 designs also run cosim) |
| `docs/` | user-facing backend and link-type docs |
| `notes/` | project state, simulator, backend, gotchas — **start here** |
| `agents/` | LLM/agent interface-IP experiments |
| `devtools/` | compiler-pipeline introspection and sweep scripts |

## Documentation

| Doc | Covers |
|---|---|
| [`notes/README.md`](notes/README.md) | index of all project documentation |
| [`notes/STATE.md`](notes/STATE.md) | branches, current work, known gaps, next steps |
| [`notes/ALLO_GOTCHAS.md`](notes/ALLO_GOTCHAS.md) | **pitfalls that cost a day** — silent wrong answers first |
| [`notes/SIMULATOR.md`](notes/SIMULATOR.md) | the JIT dataflow simulator |
| [`notes/BACKEND.md`](notes/BACKEND.md) | SystemC/Catapult backend design notes |
| [`docs/SYSTEMC_BACKEND.md`](docs/SYSTEMC_BACKEND.md) | user-facing backend guide |
| [`docs/DATAFLOW_LINKS.md`](docs/DATAFLOW_LINKS.md) | link types, with a runnable companion |
| [`CLAUDE.md`](CLAUDE.md) / [`AGENTS.md`](AGENTS.md) | coding-agent instructions |

**Read `notes/ALLO_GOTCHAS.md` before writing Allo code.** Several Allo constructs fail
*silently* rather than erroring — e.g. `~x` is wrong (use `0 - x`), and `UInt(N)` locals read
back signed.

---

# Upstream Allo

Allo provides a unified abstraction for both **accelerator design and programming**:
* **Composable Design and Programming**: behavioral and structural composition, letting users
  incrementally build and compose accelerator components into a complete system.
* **End-to-End Deployment**: automatic accelerator generation from PyTorch models, integrated
  with a high-performance simulator and a formal verifier.
* **Multiple Backend Support**: AMD and Intel FPGAs, AMD Ryzen NPUs (AI Engine), with GPUs and
  ASICs planned.

Please check out the [Allo documentation](https://cornell-zhang.github.io/allo) for
installation instructions and tutorials. If you encounter any problems, please open an
[issue](https://github.com/cornell-zhang/allo/issues).

**IMPORTANT:** If you are using a coding agent for our codebase, please import [AGENTS.md](AGENTS.md).

## Publications
Please refer to our [PLDI'24 paper](https://dl.acm.org/doi/10.1145/3656401) for more details. If you use Allo in your research, please cite our paper:
> Hongzheng Chen, Niansong Zhang, Shaojie Xiang, Zhichen Zeng, Mengjia Dai, and Zhiru Zhang, "**Allo: A Programming Model for Composable Accelerator Design**", Proc. ACM Program. Lang. 8, PLDI, Article 171 (June 2024), 2024.

Please also consider citing the following papers if you utilize specific components of Allo:
* [Dataflow programming model](https://arxiv.org/abs/2509.06794): Shihan Fang, Hongzheng Chen, Niansong Zhang, Jiajie Li, Han Meng, Adrian Liu, Zhiru Zhang, "**Dato: A Task-Based Programming Model for Dataflow Accelerators**", arXiv:2509.06794, 2025.
* [AIE backend](https://cornell-zhang.github.io/allo/backends/aie.html): Jinming Zhuang, Shaojie Xiang, Hongzheng Chen, Niansong Zhang, Zhuoping Yang, Tony Mao, Zhiru Zhang, Peipei Zhou, "**ARIES: An Agile MLIR-Based Compilation Flow for Reconfigurable Devices with AI Engines**", *International Symposium on Field-Programmable Gate Arrays (FPGA)*, 2025. (Best paper nominee)
* [Equivalence checker](https://github.com/cornell-zhang/allo/blob/main/allo/verify.py): Louis-Noël Pouchet, Emily Tucker, Niansong Zhang, Hongzheng Chen, Debjit Pal, Gabriel Rodríguez, Zhiru Zhang, "**Formal Verification of Source-to-Source Transformations for HLS**", *International Symposium on Field-Programmable Gate Arrays (FPGA)*, 2024. (Best paper award)
* [LLM accelerator](https://github.com/cornell-zhang/allo/tree/main/examples): Hongzheng Chen, Jiahao Zhang, Yixiao Du, Shaojie Xiang, Zichao Yue, Niansong Zhang, Yaohui Cai, Zhiru Zhang, "**Understanding the Potential of FPGA-Based Spatial Acceleration for Large Language Model Inference**", *ACM Transactions on Reconfigurable Technology and Systems (TRETS)*, 2024. (FCCM’24 Journal Track)


## Related Projects
* Accelerator Programming Languages: [Exo](https://github.com/exo-lang/exo), [Halide](https://github.com/halide/Halide), [TVM](https://github.com/apache/tvm), [Triton](https://github.com/openai/triton)
* Accelerator Design Languages: [Dahlia](https://github.com/cucapra/dahlia), [HeteroCL](https://github.com/cornell-zhang/heterocl), [PyLog](https://github.com/hst10/pylog), [Spatial](https://github.com/stanford-ppl/spatial)
* HLS Frameworks: [Stream-HLS](https://github.com/UCLA-VAST/Stream-HLS), [ScaleHLS](https://github.com/hanchenye/scalehls)
* Compiler Frameworks: [MLIR](https://mlir.llvm.org/)
