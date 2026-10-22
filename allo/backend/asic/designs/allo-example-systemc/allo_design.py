"""Fixed 4x4 systolic GEMM for the SystemC/Catapult ASIC example."""

from pathlib import Path

import allo
import allo.dataflow as df
import numpy as np
from allo.ir.types import Int, Stream


# Keep this pipe-cleaner interface deliberately fixed and reproducible.
Ty = Int(8)
FIFO_DEPTH = 4


@df.region()
def top(A: Ty[16], B: Ty[16], C: Ty[16]):
    """Compute one fixed 4x4 matrix multiplication."""

    horizontal: Stream[Ty, FIFO_DEPTH][4, 5]
    vertical: Stream[Ty, FIFO_DEPTH][5, 4]
    result: Stream[Ty, FIFO_DEPTH][4, 4]

    @df.kernel(mapping=[1], args=[A])
    def load_a(local_A: Ty[16]):
        with allo.meta_for(4) as row:
            for reduction in range(4):
                horizontal[row, 0].put(local_A[row * 4 + reduction])

    @df.kernel(mapping=[1], args=[B])
    def load_b(local_B: Ty[16]):
        with allo.meta_for(4) as column:
            for reduction in range(4):
                vertical[0, column].put(local_B[reduction * 4 + column])

    @df.kernel(mapping=[4, 4])
    def compute():
        row, column = df.get_pid()
        accumulator: Ty = 0
        for _ in range(4):
            value_a: Ty = horizontal[row, column].get()
            value_b: Ty = vertical[row, column].get()
            accumulator += value_a * value_b
            with allo.meta_if(column < 3):
                horizontal[row, column + 1].put(value_a)
            with allo.meta_if(row < 3):
                vertical[row + 1, column].put(value_b)
        result[row, column].put(accumulator)

    @df.kernel(mapping=[1], args=[C])
    def consume_c(local_C: Ty[16]):
        with allo.meta_for(4) as row:
            with allo.meta_for(4) as column:
                local_C[row * 4 + column] = result[row, column].get()


def build(project, target, mode, configs):
    """Build through the standard allo-asic-compilation node contract."""
    module = df.build(
        top,
        target=target,
        mode=mode,
        project=Path(project).resolve(),
        wrap_io=False,
        configs=configs,
    )
    module()


def workload():
    """Return one bounded signed 8-bit matrix transaction."""
    matrix_a = np.array(
        [
            [1, 2, 0, -1],
            [0, 1, -2, 2],
            [2, -1, 1, 0],
            [-1, 0, 2, 1],
        ],
        dtype=np.int8,
    )
    matrix_b = np.array(
        [
            [1, 0, 2, -1],
            [2, -1, 0, 1],
            [0, 3, -1, 2],
            [-2, 1, 1, 0],
        ],
        dtype=np.int8,
    )
    matrix_c = matrix_a.astype(np.int32) @ matrix_b.astype(np.int32)
    limits = np.iinfo(np.int8)
    if matrix_c.min() < limits.min or matrix_c.max() > limits.max:
        raise ValueError("pipe-cleaner result exceeds the signed 8-bit range")
    return {
        "inputs": {
            "A": matrix_a.reshape(-1),
            "B": matrix_b.reshape(-1),
        },
        "expected_outputs": {
            "C": matrix_c.astype(np.int8).reshape(-1),
        },
    }


def testbench_workload():
    """Describe the transaction for backend-specific RTL testbench creation."""
    transaction = workload()
    return {
        "schema_version": 1,
        "top_function": "top",
        "call_signature": ["A", "B", "C"],
        "calls": [
            {
                "name": "gemm_4x4",
                "reset_before": True,
                "arguments": {
                    "A": transaction["inputs"]["A"].copy(),
                    "B": transaction["inputs"]["B"].copy(),
                    "C": np.full(16, -7, dtype=np.int8),
                },
                "expected": {
                    "C": transaction["expected_outputs"]["C"].copy(),
                },
                "comparison": {
                    "C": {"mode": "bit_exact"},
                },
            }
        ],
        "default_timeout_cycles": 10_000,
    }


def run_workload():
    """Run the fixed transaction with Allo's software simulator."""
    transaction = workload()
    matrix_c = np.zeros(16, dtype=np.int8)
    simulator = df.build(top, target="simulator")
    simulator(
        transaction["inputs"]["A"].copy(),
        transaction["inputs"]["B"].copy(),
        matrix_c,
    )
    np.testing.assert_array_equal(
        matrix_c,
        transaction["expected_outputs"]["C"],
    )
    print("Allo 4x4 pipe-cleaner GEMM passed")


if __name__ == "__main__":
    run_workload()
