# Copyright Allo authors. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Allo ASIC design template."""

from pathlib import Path

import allo.dataflow as df
import numpy as np


# Replace this placeholder with an @df.region() function named `top`.
#
# @df.region()
# def top(...):
#     ...
top = None


def build(project, target, mode, configs):
    """Build the design through the Allo ASIC compilation contract."""
    if top is None:
        raise NotImplementedError(
            "Replace the `top` placeholder with an Allo dataflow region"
        )

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
    """Create one test transaction and its expected outputs."""
    return {
        # List every top-level argument in declaration order.
        "call_signature": [
            # "input_name",
            # "output_name",
        ],
        # Give every argument an initial value, including output arrays.
        "arguments": {
            # "input_name": np.array([...], dtype=np.int8),
            # "output_name": np.zeros(..., dtype=np.int8),
        },
        # Include each output which the testbench should check.
        "expected": {
            # "output_name": np.array([...], dtype=np.int8),
        },
    }


def testbench_workload():
    """Describe the workload used by RTL, FFGL, and BAGL simulation."""
    transaction = workload()

    return {
        "top_function": "top",
        "call_signature": transaction["call_signature"],
        "calls": [
            {
                "name": "example",
                "reset_before": True,
                "arguments": transaction["arguments"],
                "expected": transaction["expected"],
                "comparison": {
                    # "output_name": {"mode": "bit_exact"},
                },
            }
        ],
        "default_timeout_cycles": 10_000,
    }
