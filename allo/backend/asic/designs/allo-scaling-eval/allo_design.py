"""Parameterized systolic GEMM for flat-versus-macro scaling evaluation."""

import os
from pathlib import Path

import allo
import allo.dataflow as df
import numpy as np
from allo.ir.types import Int, Stream


P = int(os.environ.get("allo_array_size", "4"))
K = int(os.environ.get("allo_reduction_size", "4"))
DTYPE_BITS = int(os.environ.get("allo_dtype_bits", "32"))
DEPTH = int(os.environ.get("allo_fifo_depth", "4"))

if P < 1:
    raise ValueError("allo_array_size must be positive")
if K < 1:
    raise ValueError("allo_reduction_size must be positive")
if DTYPE_BITS < 1:
    raise ValueError("allo_dtype_bits must be positive")
if DEPTH < 1:
    raise ValueError("allo_fifo_depth must be positive")

Ty = Int(DTYPE_BITS)


def _numpy_dtype():
    """Return the matching NumPy carrier for the planned evaluation widths."""
    if DTYPE_BITS == 16:
        return np.int16
    if DTYPE_BITS == 32:
        return np.int32
    if DTYPE_BITS == 64:
        return np.int64
    raise ValueError(
        "The executable workload supports allo_dtype_bits in {16, 32, 64}; "
        "Allo compilation itself accepts other Int widths."
    )


@df.region()
def top(A: Ty[P * K], B: Ty[K * P], C: Ty[P * P]):
    """Compute A[P,K] @ B[K,P] on exactly P*P mapped compute PEs.

    Each loader and the result consumer is one large, unmapped spatial
    instance. Only ``compute`` is mapped, so only its P*P instances provide
    the intended shared-macro population.
    """

    horizontal: Stream[Ty, DEPTH][P, P + 1]
    vertical: Stream[Ty, DEPTH][P + 1, P]
    result: Stream[Ty, DEPTH][P, P]

    @df.kernel(mapping=[1], args=[A])
    def load_a(local_A: Ty[P * K]):
        with allo.meta_for(P) as i:
            for k in range(K):
                horizontal[i, 0].put(local_A[i * K + k])

    @df.kernel(mapping=[1], args=[B])
    def load_b(local_B: Ty[K * P]):
        with allo.meta_for(P) as j:
            for k in range(K):
                vertical[0, j].put(local_B[k * P + j])

    @df.kernel(mapping=[P, P])
    def compute():
        i, j = df.get_pid()
        accumulator: Ty = 0
        for k in range(K):
            a: Ty = horizontal[i, j].get()
            b: Ty = vertical[i, j].get()
            accumulator += a * b
            with allo.meta_if(j < P - 1):
                horizontal[i, j + 1].put(a)
            with allo.meta_if(i < P - 1):
                vertical[i + 1, j].put(b)
        result[i, j].put(accumulator)

    @df.kernel(mapping=[1], args=[C])
    def consume_c(local_C: Ty[P * P]):
        with allo.meta_for(P) as i:
            with allo.meta_for(P) as j:
                local_C[i * P + j] = result[i, j].get()


def build(project, target, mode, configs):
    """Build using the standard allo-asic-compilation node contract."""
    project = Path(project).resolve()
    module = df.build(
        top,
        target=target,
        mode=mode,
        project=project,
        wrap_io=False,
        configs=configs,
    )
    module()


def workload():
    """Return a deterministic, non-overflowing matrix transaction."""
    numpy_dtype = _numpy_dtype()
    values_a = ((np.arange(P * K, dtype=np.int64) % 7) - 3).astype(
        numpy_dtype
    )
    values_b = (((np.arange(K * P, dtype=np.int64) * 3) % 7) - 3).astype(
        numpy_dtype
    )
    matrix_a = values_a.reshape(P, K)
    matrix_b = values_b.reshape(K, P)
    expected_c = matrix_a @ matrix_b
    return {
        "inputs": {"A": matrix_a.reshape(-1), "B": matrix_b.reshape(-1)},
        "expected_outputs": {"C": expected_c.reshape(-1)},
    }

def testbench_workload():
    """Return a backend-independent workload for generated RTL testbenches.

    This describes function-level calls and expected architectural results.
    It intentionally contains no AXI or cycle-level information; the
    testbench-generation node obtains that from the Vitis CSYN artifacts.
    """
    transaction = workload()

    matrix_a = transaction["inputs"]["A"].copy()
    matrix_b = transaction["inputs"]["B"].copy()

    # Give the output memory a deterministic nonzero initial state. This helps
    # detect incomplete output writes that a zero-filled memory could conceal.
    matrix_c_initial = np.full(
        P * P,
        fill_value=np.array(-7, dtype=_numpy_dtype()),
        dtype=_numpy_dtype(),
    )

    matrix_c_expected = transaction["expected_outputs"]["C"].copy()

    return {
        "schema_version": 1,
        "top_function": "top",
        "call_signature": ["A", "B", "C"],

        "calls": [
            {
                "name": "gemm",

                # Assert hardware reset before the first transaction.
                "reset_before": True,

                # These are the initial contents of the memories passed to the
                # top-level Allo function.
                "arguments": {
                    "A": matrix_a,
                    "B": matrix_b,
                    "C": matrix_c_initial,
                },

                # The testbench waits for hardware completion and compares the
                # final C memory against this value.
                "expected": {
                    "C": matrix_c_expected,
                },

                "comparison": {
                    "C": {
                        "mode": "bit_exact",
                    },
                },
            }
        ],

        # set timeout - scale with array size 
        "default_timeout_cycles": max(10000, 100 * P * P * K),
    }

def run_workload():
    """Run and check the workload with Allo's dataflow simulator."""
    transaction = workload()
    matrix_a = transaction["inputs"]["A"].copy()
    matrix_b = transaction["inputs"]["B"].copy()
    matrix_c = np.zeros(P * P, dtype=_numpy_dtype())

    simulator = df.build(top, target="simulator")
    simulator(matrix_a, matrix_b, matrix_c)
    np.testing.assert_array_equal(
        matrix_c, transaction["expected_outputs"]["C"]
    )
    print(
        f"Allo scaling GEMM passed: P={P}, K={K}, dtype=Int({DTYPE_BITS})"
    )


if __name__ == "__main__":
    run_workload()
