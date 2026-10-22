# Copyright Allo authors. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import ast
import json
import re

import pytest

import allo
import allo.dataflow as df
from allo.backend import asic_manifest
from allo.ir.pid_generalization import generalize_pid_uses
from allo.ir.types import int32, Stream


@df.region()
def pid_identity_region(out: int32[2]):
    @df.kernel(mapping=[2], args=[out])
    def pe(local_out: int32[2]):
        i = df.get_pid()
        identity: int32 = i
        local_out[i] = identity


@df.region()
def pid_data_only_region(out: int32[1]):
    @df.kernel(mapping=[2], args=[out])
    def pe(local_out: int32[1]):
        i = df.get_pid()
        local_out[0] = i


@df.region()
def pid_2d_region(out: int32[1]):
    @df.kernel(mapping=[2, 4], args=[out])
    def pe(local_out: int32[1]):
        i, j = df.get_pid()
        local_out[0] = i + j


@df.region()
def pid_stream_region(out: int32[2]):
    pipe: Stream[int32, 2][2]

    @df.kernel(mapping=[2])
    def source():
        i = df.get_pid()
        pipe[i].put(i)

    @df.kernel(mapping=[2], args=[out])
    def sink(local_out: int32[2]):
        i = df.get_pid()
        local_out[i] = pipe[i].get()


def test_identity_only_generalizes_data_but_not_structural_index():
    schedule = df.customize(
        pid_identity_region, pid_generalization_policy="identity_only"
    )
    module = str(schedule.module)
    assert "func.func @pe_0(%arg0: memref<2xi32>, %arg1: i1)" in module
    assert "func.func @pe_1(%arg0: memref<2xi32>, %arg1: i1)" in module
    assert 'df.pid_generalization_policy = "identity_only"' in module
    assert "call @pe_0(%arg0, %false)" in module
    assert "call @pe_1(%arg0, %true)" in module
    assert "affine.store %1, %arg0[0]" in module
    assert "affine.store %1, %arg0[1]" in module


def test_generalization_is_opt_in():
    module = str(df.customize(pid_identity_region).module)
    assert "df.pid_generalization_policy" not in module
    assert "func.func @pe_0(%arg0: memref<2xi32>)" in module


def test_all_operations_keeps_stream_topology_specialized():
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
    transformed = generalize_pid_uses(
        ast.parse(source),
        {"allo": allo, "df": df, "Stream": object, "int32": int32},
        "all_operations",
    )
    text = ast.unparse(transformed)
    assert "value: int32 = _df_pid0 + 1" in text
    assert "allo.meta_if(i == 0)" in text
    assert "pipe[i].put(value)" in text


def test_all_operations_threads_each_generalized_axis():
    module = str(
        df.customize(pid_2d_region, pid_generalization_policy="all_operations").module
    )
    assert 'df.pid_generalized_axes = "0,1"' in module
    assert "func.func @pe_0_0(%arg0: memref<1xi32>, %arg1: i1, %arg2: i2)" in module
    assert "call @pe_1_3" in module


def test_unknown_policy_is_rejected():
    with pytest.raises(ValueError, match="pid_generalization_policy"):
        generalize_pid_uses(ast.parse("def top(): pass"), {}, "all")


def test_generalized_identity_forms_one_pre_hls_candidate():
    schedule = df.customize(
        pid_data_only_region, pid_generalization_policy="identity_only"
    )
    manifest = asic_manifest.collect(schedule.module, schedule.top_func_name)
    assert len(manifest["candidate_groups"]) == 1
    assert manifest["candidate_groups"][0]["member_count"] == 2


def test_generalized_axes_survive_stream_interface_rewriting():
    schedule = df.customize(
        pid_stream_region, pid_generalization_policy="all_operations"
    )
    manifest = asic_manifest.collect(schedule.module, schedule.top_func_name)
    assert all(pe["pid_generalized_axes"] == [0] for pe in manifest["pe_instances"])
    assert sorted(group["member_count"] for group in manifest["candidate_groups"]) == [
        2,
        2,
    ]
    assert {
        port["operation"] for pe in manifest["pe_instances"] for port in pe["ports"]
    } == {
        "get",
        "put",
    }


def test_systemc_emits_generalized_pid_configuration_ports(tmp_path):
    module = df.build(
        pid_data_only_region,
        target="systemc",
        project=str(tmp_path / "systemc"),
        configs={
            "generalize_pid_specializations": True,
            "pid_generalization_policy": "identity_only",
        },
    )
    code = module.hls_code

    assert len(re.findall(r"sc_in< bool > v\d+;", code)) == 2
    assert len(re.findall(r"int32_t v\d+ = v\d+\.read\(\);", code)) == 2
    for instance, value in (("u0", "false"), ("u1", "true")):
        signal = f"{instance}_cfg1"
        assert f"sc_signal< bool > {signal};" in code
        assert re.search(rf"{instance}\.v\d+\({signal}\);", code)
        assert f"{signal}.write({value});" in code


def test_build_records_generalization_contract_and_debug_inputs(tmp_path):
    project = tmp_path / "top.prj"
    df.build(
        pid_identity_region,
        target="vitis_hls",
        project=str(project),
        configs={
            "generalize_pid_specializations": True,
            "asic_manifest": {"enabled": True, "debug_artifacts": True},
        },
    )
    manifest = json.loads((project / "asic-manifest.json").read_text())
    # Structural memory indices remain specialized, so these are two candidates.
    assert len(manifest["candidate_groups"]) == 2
    for pe in manifest["pe_instances"]:
        assert pe["pid_generalization_policy"] == "identity_only"
        assert pe["pid_generalized_axes"] == [0]
        contract = pe["pre_hls_implementation_contract"]
        assert contract["pid_generalization_policy"] == "identity_only"
        assert contract["pid_generalized_axes"] == [0]
    debug = project / "asic-debug"
    assert (debug / "identity-map.json").is_file()
    assert (debug / "canonical/pre-hls/pe_0.txt").is_file()
