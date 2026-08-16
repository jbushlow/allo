# Copyright Allo authors. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import ast

import pytest

import allo
import allo.dataflow as df
from allo.customize import customize
from allo.ir.pid_generalization import generalize_pid_uses
from allo.ir.types import int32


@df.region()
def pid_identity_region(out: int32[2]):
    @df.kernel(mapping=[2], args=[out])
    def pe(local_out: int32[2]):
        i = df.get_pid()
        identity: int32 = i
        local_out[i] = identity


def test_identity_only_adds_runtime_identity_but_keeps_structural_index():
    schedule = customize(
        pid_identity_region, pid_generalization_policy="identity_only"
    )
    df._build_top(schedule, {"pe_0": [], "pe_1": []})
    module = str(schedule.module)
    assert "func.func @pe_0(%arg0: memref<2xi32>, %arg1: i1)" in module
    assert "func.func @pe_1(%arg0: memref<2xi32>, %arg1: i1)" in module
    assert 'df.pid_generalization_policy = "identity_only"' in module
    assert "call @pe_0(%arg0, %false)" in module
    assert "call @pe_1(%arg0, %true)" in module
    assert "affine.store %1, %arg0[0]" in module
    assert "affine.store %1, %arg0[1]" in module


def test_all_operations_still_preserves_stream_topology_indices():
    source = """
def top():
    pipe: Stream[int32, 2][2]
    @df.kernel(mapping=[2])
    def pe():
        i = df.get_pid()
        value: int32 = i + 1
        with allo.meta_if(i == 0):
            value = value + 2
        pipe[i].put(value)
"""
    tree = ast.parse(source)
    transformed = generalize_pid_uses(
        tree,
        {"allo": allo, "df": df, "Stream": object, "int32": int32},
        "all_operations",
    )
    text = ast.unparse(transformed)
    assert "value: int32 = _df_pid0 + 1" in text
    assert "allo.meta_if(i == 0)" in text
    assert "pipe[i].put(value)" in text


def test_unknown_pid_generalization_policy_is_rejected():
    with pytest.raises(ValueError, match="pid_generalization_policy"):
        generalize_pid_uses(ast.parse("def top(): pass"), {}, "all")
