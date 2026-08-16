import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path


SOURCE = Path(__file__).parents[1] / "allo" / "backend" / "asic.py"
SPEC = importlib.util.spec_from_file_location("allo_asic_manifest", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _manifest():
    return MODULE.build_pre_hls_manifest(
        top="region",
        functions={
            "loader_0_1": "func.func @loader_0_1(%arg0: i32) { %x = arith.addi %arg0, %arg0 : i32 }",
            "loader_1_0": "func.func @loader_1_0(%north: i32) { %y = arith.addi %north, %north : i32 }",
        },
        stream_info={
            "loader_0_1": [("fifo_0_0", "in"), ("fifo_0_1", "out")],
            "loader_1_0": [("fifo_0_0", "out"), ("fifo_1_0", "in")],
        },
        stream_types={
            "fifo_0_0": "!allo.stream<i32, 2>",
            "fifo_0_1": "!allo.stream<i32, 2>",
            "fifo_1_0": "!allo.stream<i32, 2>",
        },
        extra_stream_info={"loader_0_1": {"fifo_0_1": {"i": 0}}},
        func_instances={"loader": {(0, 1): "edge", (1, 0): "edge"}},
        mappings={"loader": [2, 2]},
    )


def test_capture_builds_region_wide_channels_and_stable_pe_ids():
    manifest = _manifest()
    assert [pe["semantic_id"] for pe in manifest["pe_instances"]] == [
        "region/loader/pid=0,1",
        "region/loader/pid=1,0",
    ]
    shared = next(c for c in manifest["channels"] if c["stream"] == "fifo_0_0")
    assert {(e["pe"], e["direction"]) for e in shared["endpoints"]} == {
        ("region/loader/pid=0,1", "in"),
        ("region/loader/pid=1,0", "out"),
    }
    assert manifest["pe_instances"][0]["ports"][1]["symbol_bindings"] == {"i": 0}


def test_pre_hls_equivalence_ignores_generated_names_and_orientation():
    manifest = _manifest()
    first, second = manifest["pe_instances"]
    assert first["pre_hls_equivalence_hash"] == second["pre_hls_equivalence_hash"]
    assert first["interface_hash"] == second["interface_hash"]
    assert manifest["candidate_groups"][0]["member_count"] == 2
    assert (
        first["pre_hls_implementation_contract_hash"]
        == second["pre_hls_implementation_contract_hash"]
    )
    assert all(
        port["desired_compass_direction"] == "unassigned"
        for pe in manifest["pe_instances"] for port in pe["ports"]
    )


def test_pre_hls_hash_does_not_depend_on_stream_argument_order():
    first = """
func.func @left(%west: !allo.stream<i32>, %east: !allo.stream<i32>)
    attributes {stypes = "io"} {
  %0 = allo.stream_get(%west) : i32
  allo.stream_put(%east, %0) : i32
  func.return
}
"""
    rotated = """
func.func @top(%south: !allo.stream<i32>, %north: !allo.stream<i32>)
    attributes {stypes = "oi"} {
  %value = allo.stream_get(%north) : i32
  allo.stream_put(%south, %value) : i32
  func.return
}
"""
    assert MODULE.canonical_pre_hls_ir(first) == MODULE.canonical_pre_hls_ir(rotated)


def test_pre_hls_contract_keeps_stream_depth_and_blocking_protocol():
    shallow = _manifest()
    deep = MODULE.build_pre_hls_manifest(
        top="region",
        functions={
            "loader_0_1": "func.func @loader_0_1(%arg0: i32) { %x = arith.addi %arg0, %arg0 : i32 }",
        },
        stream_info={"loader_0_1": [("fifo", "in")]},
        stream_types={"fifo": "!allo.stream<i32, 8>"},
        extra_stream_info={},
        mappings={"loader": [2, 2]},
    )
    assert (
        shallow["pe_instances"][0]["pre_hls_implementation_contract_hash"]
        != deep["pe_instances"][0]["pre_hls_implementation_contract_hash"]
    )


def test_pre_hls_contract_records_pid_generalization_policy():
    manifest = MODULE.build_pre_hls_manifest(
        top="region",
        functions={"pe_0": "func.func @pe_0() { func.return }"},
        stream_info={"pe_0": []},
        stream_types={},
        extra_stream_info={},
        identities={
            "pe_0": {
                "kernel": "pe",
                "pid": [0],
                "mapping": [2],
                "pid_generalization_policy": "identity_only",
                "pid_generalized_axes": [0],
            }
        },
    )
    pe = manifest["pe_instances"][0]
    assert pe["pid_generalization_policy"] == "identity_only"
    assert pe["pid_generalized_axes"] == [0]
    assert pe["pre_hls_implementation_contract"]["pid_generalization_policy"] == (
        "identity_only"
    )


def test_pre_hls_hash_ignores_frontend_debug_names_but_keeps_constants():
    first = 'func.func @a() { %0 = "op"() {name = "a", loop_name = "i3", from = "a", to = "a", value = 7} : () -> i8 }'
    renamed = 'func.func @b() { %x = "op"() {name = "b", loop_name = "j9", from = "b", to = "b", value = 7} : () -> i8 }'
    changed = renamed.replace("value = 7", "value = 8")
    assert MODULE.canonical_pre_hls_ir(first) == MODULE.canonical_pre_hls_ir(renamed)
    assert MODULE.canonical_pre_hls_ir(first) != MODULE.canonical_pre_hls_ir(changed)


def test_config_path_and_paired_json_tcl_output(tmp_path):
    config = MODULE.normalize_manifest_config(
        {"asic_manifest": {"enabled": True, "path": "graph.json"}}, str(tmp_path)
    )
    assert config["path"] == str(tmp_path / "graph.json")
    json_path, tcl_path = MODULE.write_manifest(_manifest(), config["path"])
    assert json.loads(Path(json_path).read_text())["stage"] == "pre_hls"
    assert Path(tcl_path).is_file()
    if shutil.which("tclsh"):
        check = tmp_path / "check.tcl"
        check.write_text(
            Path(tcl_path).read_text()
            + '\nputs [dict get $allo_asic_manifest stage]\n',
            encoding="utf-8",
        )
        result = subprocess.run(
            ["tclsh", str(check)], check=True, capture_output=True, text=True
        )
        assert result.stdout.strip() == "pre_hls"


def test_debug_config_writes_mlir_identity_and_canonical_inputs(tmp_path):
    config = MODULE.normalize_manifest_config(
        {
            "asic_manifest": {
                "debug_artifacts": True,
                "debug_dir": "debug",
            }
        },
        str(tmp_path),
    )
    functions = {"pe_0": "func.func @pe_0() { func.return }"}
    identities = {"pe_0": {"kernel": "pe", "pid": [0], "mapping": [1]}}
    MODULE.write_debug_artifact(config, "01-specialized.mlir", "module {}")
    MODULE.write_pre_hls_debug_artifacts(config, functions, identities)
    debug = tmp_path / "debug"
    assert (debug / "01-specialized.mlir").is_file()
    assert json.loads((debug / "identity-map.json").read_text()) == identities
    canonical = debug / "canonical" / "pre-hls" / "pe_0.txt"
    assert canonical.read_text().strip() == MODULE.canonical_pre_hls_ir(
        functions["pe_0"]
    )


def test_mapping_rank_separates_pid_from_other_specialization_constants():
    semantic_id, kernel, pid = MODULE._semantic_identity(
        "top", "gemm_2_3_4_4_32_16", {"gemm": [4, 4]}
    )
    assert semantic_id == "top/gemm/pid=2,3"
    assert kernel == "gemm"
    assert pid == [2, 3]


def test_explicit_identity_overrides_specialized_function_suffix():
    manifest = MODULE.build_pre_hls_manifest(
        top="top",
        functions={"gemm_0_1_4_4_32_32_16_0": "func.func @f() { func.return }"},
        stream_info={"gemm_0_1_4_4_32_32_16_0": []},
        stream_types={},
        extra_stream_info={},
        identities={
            "gemm_0_1_4_4_32_32_16_0": {
                "kernel": "gemm",
                "pid": [0, 1],
                "mapping": [6, 6],
                "parent_region": "MXU_4_4_32_32_16_0",
                "selected_branch_trace": ["meta_elif@42:8"],
            }
        },
    )
    pe = manifest["pe_instances"][0]
    assert pe["semantic_id"] == "top/MXU_4_4_32_32_16_0/gemm/pid=0,1"
    assert pe["pid"] == [0, 1]
    assert pe["mapping"] == [6, 6]
    assert pe["selected_branch_trace"] == ["meta_elif@42:8"]


def test_repeated_stream_accesses_share_one_logical_endpoint():
    manifest = MODULE.build_pre_hls_manifest(
        top="top",
        functions={"drain_0": "func.func @f() { func.return }"},
        stream_info={"drain_0": [("fifo", "out"), ("fifo", "out")]},
        stream_types={"fifo": "!allo.stream<i32, 2>"},
        extra_stream_info={},
    )
    endpoints = manifest["channels"][0]["endpoints"]
    assert len(endpoints) == 1
    assert endpoints[0]["role"] == "producer"
    assert endpoints[0]["accesses"] == [
        {"port_ordinal": 0, "operation": "put"},
        {"port_ordinal": 1, "operation": "put"},
    ]
