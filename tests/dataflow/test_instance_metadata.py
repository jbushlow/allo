# Copyright Allo authors. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import ast
import json

import allo
import allo.dataflow as df
from allo.backend import asic_manifest
from allo.ir.types import int32


CORE_ATTRS = {"df.kernel_name", "df.pid", "df.mapping"}
AUDIT_ATTRS = {"df.predicate_tag", "df.selected_branch_trace"}
INNER_SIZE = 2


def _kernels(module):
    """Return kernel ops while the caller keeps their MLIR module alive."""
    return {
        op.attributes["sym_name"].value: op
        for op in module.body.operations
        if op.operation.name == "func.func" and "df.kernel" in op.attributes
    }


def _df_attrs(op):
    return {name for name in op.attributes if name.startswith("df.")}


def plain_add(A: int32[1]):
    A[0] += 1


@df.region()
def digit_named_region(out: int32[2]):
    @df.kernel(mapping=[2], args=[out])
    def stage_2(local_out: int32[2]):
        i = df.get_pid()
        local_out[i] = i


@df.region()
def conditional_region(out: int32[2]):
    @df.kernel(mapping=[2], args=[out])
    def select(local_out: int32[2]):
        i = df.get_pid()
        with allo.meta_if(i == 0):
            local_out[i] = 1
        with allo.meta_else():
            local_out[i] = 2


@df.region()
def metadata_inner[P](out: int32[2]):
    @df.kernel(mapping=[P], args=[out])
    def leaf_7(local_out: int32[2]):
        i = df.get_pid()
        local_out[i] = i


@df.region()
def nested_region(out: int32[2]):
    @df.kernel(mapping=[1], args=[out])
    def wrapper(local_out: int32[2]):
        metadata_inner[INNER_SIZE](local_out)


def test_plain_allo_has_no_dataflow_metadata():
    schedule = allo.customize(plain_add)
    assert all(not _df_attrs(op) for op in schedule.module.body.operations)


def test_digit_ending_kernel_keeps_explicit_identity_without_audit():
    schedule = df.customize(digit_named_region)
    kernels = _kernels(schedule.module)
    assert set(kernels) == {"stage_2_0", "stage_2_1"}
    for pid in range(2):
        op = kernels[f"stage_2_{pid}"]
        assert CORE_ATTRS <= _df_attrs(op)
        assert op.attributes["df.kernel_name"].value == "stage_2"
        assert op.attributes["df.pid"].value == str(pid)
        assert op.attributes["df.mapping"].value == "2"
        assert not (AUDIT_ATTRS & _df_attrs(op))
        assert "df.parent_region" not in op.attributes
        assert "df.specialization_suffix" not in op.attributes


def test_manifest_prefers_explicit_identity_and_keeps_symbol_fallback():
    schedule = df.customize(digit_named_region)
    expected = [("stage_2", [0], [2]), ("stage_2", [1], [2])]

    explicit = asic_manifest.collect(schedule.module, schedule.top_func_name)
    assert [
        (pe["kernel"], pe["pid"], pe["mapping"]) for pe in explicit["pe_instances"]
    ] == expected

    for op in _kernels(schedule.module).values():
        for name in CORE_ATTRS:
            del op.attributes[name]
    fallback = asic_manifest.collect(
        schedule.module, schedule.top_func_name, mappings={"stage_2": [2]}
    )
    assert [
        (pe["kernel"], pe["pid"], pe["mapping"]) for pe in fallback["pe_instances"]
    ] == expected


def test_conditional_audit_is_opt_in_and_identity_is_stable():
    ordinary_schedule = df.customize(conditional_region)
    ordinary = {
        name: (
            {attr: op.attributes[attr].value for attr in CORE_ATTRS},
            _df_attrs(op),
        )
        for name, op in _kernels(ordinary_schedule.module).items()
    }
    audited_schedule = df.customize(conditional_region, capture_dataflow_audit=True)
    audited = _kernels(audited_schedule.module)
    assert ordinary.keys() == audited.keys()

    branches = set()
    for name, op in audited.items():
        identity, attrs = ordinary[name]
        assert identity == {attr: op.attributes[attr].value for attr in CORE_ATTRS}
        assert not (AUDIT_ATTRS & attrs)
        trace = ast.literal_eval(op.attributes["df.selected_branch_trace"].value)
        assert len(trace) == 1
        branches.add(trace[0].split("@", 1)[0])
        assert "df.predicate_tag" not in op.attributes
    assert branches == {"meta_if", "meta_else"}


def test_nested_identity_survives_both_interface_rewrites():
    schedule = df.customize(nested_region)
    kernels = _kernels(schedule.module)
    nested = [op for op in kernels.values() if "df.nested_kernel" in op.attributes]
    assert len(nested) == 2
    assert {op.attributes["df.pid"].value for op in nested} == {"0", "1"}
    for op in nested:
        assert op.attributes["df.kernel_name"].value == "leaf_7"
        assert op.attributes["df.mapping"].value == "2"
        assert op.attributes["df.parent_region"].value == "metadata_inner_2_0"
        assert op.attributes["df.specialization_suffix"].value == "2_0"


def test_manifest_config_enables_audit_capture(tmp_path):
    built = df.build(
        conditional_region,
        target="vitis_hls",
        project=str(tmp_path / "top.prj"),
        configs={"asic_manifest": {"enabled": True}},
    )
    assert all(
        "df.selected_branch_trace" in op.attributes
        for op in _kernels(built.module).values()
    )
    manifest = json.loads((tmp_path / "top.prj" / "asic-manifest.json").read_text())
    traces = {
        pe["selected_branch_trace"][0].split("@", 1)[0]
        for pe in manifest["pe_instances"]
    }
    assert traces == {"meta_if", "meta_else"}
    assert all(pe["predicate_tag"] is None for pe in manifest["pe_instances"])
