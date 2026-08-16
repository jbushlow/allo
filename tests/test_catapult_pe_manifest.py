import importlib.util
import json
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "extract_catapult_pe_manifest.py"
SPEC = importlib.util.spec_from_file_location("extract_catapult_pe_manifest", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_catapult_top_arguments_are_joined_positionally(tmp_path):
    kernel = tmp_path / "kernel.cpp"
    kernel.write_text(
        "void pe_fixed() {}\n"
        "void top(uint32_t v7[4], uint32_t v8[8]) { pe_fixed(); }\n",
        encoding="utf-8",
    )
    rtl = tmp_path / "concat_rtl.v"
    rtl.write_text(
        "module pe_fixed();\nendmodule\n"
        "module top(v7_rsc_dat, v8_rsc_dat);\n"
        "input [31:0] v7_rsc_dat; output [31:0] v8_rsc_dat;\nendmodule\n",
        encoding="utf-8",
    )
    pre = {
        "schema_version": 2,
        "stage": "pre_hls",
        "top": "top",
        "top_arguments": [
            {"ordinal": 0, "name": "A", "direction": "input"},
            {"ordinal": 1, "name": "C", "direction": "output"},
        ],
        "pe_instances": [],
    }
    manifest = MODULE.build_manifest(kernel, pre, rtl, "top")
    assert [
        (arg["name"], arg["catapult_argument"]) for arg in manifest["top_arguments"]
    ] == [
        ("A", "v7"),
        ("C", "v8"),
    ]
    assert manifest["rtl_artifact"]["path"] == str(rtl)


def test_catapult_memory_protocols_are_matched_exactly(tmp_path):
    kernel = tmp_path / "kernel.cpp"
    kernel.write_text(
        "void top(uint32_t v7[64], uint32_t v8[64]) {}\n", encoding="utf-8"
    )
    rtl = tmp_path / "concat_rtl.v"
    rtl.write_text(
        "module top(v7_rsc_radr, v7_rsc_re, v7_rsc_q, "
        "v8_rsc_wadr, v8_rsc_we, v8_rsc_d, v8_triosy_lz);\n"
        "output [5:0] v7_rsc_radr; output v7_rsc_re; input [31:0] v7_rsc_q;\n"
        "output [5:0] v8_rsc_wadr; output v8_rsc_we; output [31:0] v8_rsc_d;\n"
        "output v8_triosy_lz;\nendmodule\n",
        encoding="utf-8",
    )
    pre = {
        "top": "top", "pe_instances": [],
        "top_arguments": [
            {"ordinal": 0, "name": "A", "shape": [64], "type": "ui32", "direction": "input"},
            {"ordinal": 1, "name": "C", "shape": [64], "type": "ui32", "direction": "output"},
        ],
    }
    manifest = MODULE.build_manifest(kernel, pre, rtl, "top")
    read, write = manifest["top_arguments"]
    assert read["interface_protocol"] == "catapult_sync_memory_read"
    assert read["interface"]["resource_root"] == "v7"
    assert read["interface"]["roles"]["read_address"]["width"] == 6
    assert read["interface"]["read_latency_cycles"] == 1
    assert write["interface_protocol"] == "catapult_sync_memory_write"
    assert write["interface"]["roles"]["write_data"]["width"] == 32
    assert manifest["top_interface"]["argument_protocols"] == [
        "catapult_sync_memory_read", "catapult_sync_memory_write"
    ]


def test_catapult_float_memory_resource_suffix_is_matched(tmp_path):
    kernel = tmp_path / "kernel.cpp"
    kernel.write_text(
        "void top(ac_ieee_float<binary16> v7[64]) {}\n", encoding="utf-8"
    )
    rtl = tmp_path / "concat_rtl.v"
    rtl.write_text(
        "module top(v7_d_rsc_radr, v7_d_rsc_re, v7_d_rsc_q, v7_d_triosy_lz);\n"
        "output [5:0] v7_d_rsc_radr; output v7_d_rsc_re; "
        "input [15:0] v7_d_rsc_q; output v7_d_triosy_lz;\nendmodule\n",
        encoding="utf-8",
    )
    pre = {
        "top": "top", "pe_instances": [],
        "top_arguments": [
            {"name": "A", "shape": [64], "type": "ui16", "direction": "input"}
        ],
    }
    manifest = MODULE.build_manifest(kernel, pre, rtl, "top")
    interface = manifest["top_arguments"][0]["interface"]
    assert interface["protocol"] == "catapult_sync_memory_read"
    assert interface["resource_root"] == "v7_d"
    assert interface["roles"]["read_data"]["name"] == "v7_d_rsc_q"
    assert [port["name"] for port in interface["triosy_ports"]] == [
        "v7_d_triosy_lz"
    ]


def test_catapult_unknown_resource_suffix_is_rejected(tmp_path):
    kernel = tmp_path / "kernel.cpp"
    kernel.write_text("void top(uint16_t v7[64]) {}\n", encoding="utf-8")
    rtl = tmp_path / "concat_rtl.v"
    rtl.write_text(
        "module top(v7_guess_rsc_radr, v7_guess_rsc_re, v7_guess_rsc_q);\n"
        "output [5:0] v7_guess_rsc_radr; output v7_guess_rsc_re; "
        "input [15:0] v7_guess_rsc_q;\nendmodule\n",
        encoding="utf-8",
    )
    pre = {"top": "top", "pe_instances": [], "top_arguments": [
        {"name": "A", "shape": [64], "type": "ui16", "direction": "input"}
    ]}
    import pytest
    with pytest.raises(RuntimeError, match="unsupported resource root"):
        MODULE.build_manifest(kernel, pre, rtl, "top")


def test_catapult_incomplete_memory_family_is_rejected(tmp_path):
    kernel = tmp_path / "kernel.cpp"
    kernel.write_text("void top(uint32_t v7[64]) {}\n", encoding="utf-8")
    rtl = tmp_path / "concat_rtl.v"
    rtl.write_text(
        "module top(v7_rsc_radr, v7_rsc_q);\n"
        "output [5:0] v7_rsc_radr; input [31:0] v7_rsc_q;\nendmodule\n",
        encoding="utf-8",
    )
    pre = {"top": "top", "pe_instances": [], "top_arguments": [
        {"name": "A", "shape": [64], "type": "ui32", "direction": "input"}
    ]}
    import pytest
    with pytest.raises(RuntimeError, match="match 0 known schemas"):
        MODULE.build_manifest(kernel, pre, rtl, "top")


def test_catapult_project_discovery_selects_concat_rtl(tmp_path):
    rtl = tmp_path / "Catapult" / "top.v1" / "concat_rtl.v"
    rtl.parent.mkdir(parents=True)
    rtl.write_text("module top(); endmodule\n", encoding="utf-8")
    assert MODULE._find_concat_rtl(tmp_path, "top") == rtl


def test_catapult_manifest_records_hierarchy_and_pe_root(tmp_path):
    kernel = tmp_path / "kernel.cpp"
    kernel.write_text(
        "void pe_fixed() { int value = 0; }\nvoid top() { pe_fixed(); }\n"
    )
    rtl = tmp_path / "concat_rtl.v"
    rtl.write_text(
        "module pe_core(); endmodule\n"
        "module pe_fixed(); pe_core core_inst(); endmodule\n"
        "module top(); pe_fixed pe_inst(); endmodule\n"
    )
    pre = {
        "top": "top",
        "top_arguments": [],
        "pe_instances": [
            {
                "semantic_id": "top/pe/pid=0",
                "kernel": "pe",
                "pid": [0],
                "specialized_function": "pe",
            }
        ],
    }
    manifest = MODULE.build_manifest(kernel, pre, rtl, "top")
    record = manifest["pe_instances"][0]["post_hls_records"][0]
    assert record["rtl_root_module"] == "pe_fixed"
    assert record["rtl_root_instances"][0]["instance_path"] == "top/pe_inst"
    realized = manifest["rtl_hierarchy"]["realized_instances"]
    assert [item["instance_path"] for item in realized] == [
        "top/pe_inst",
        "top/pe_inst/core_inst",
    ]


def test_catapult_rscid_is_metadata_but_constants_remain_behavioral():
    first = """
module pe_a(input [7:0] a, output [7:0] z);
wire [7:0] n; mgc_io #(.rscid(32'sd17)) u(.d(a), .q(n));
assign z = n + 8'd3;
endmodule
"""
    renamed = """
module pe_b(input [7:0] x, output [7:0] y);
wire [7:0] t; mgc_io #(.rscid(32'sd93)) other(.d(x), .q(t));
assign y = t + 8'd3;
endmodule
"""
    fingerprint = MODULE.SHARED.canonical_verilog_fingerprint
    assert fingerprint(first, backend="catapult") == fingerprint(renamed, backend="catapult")
    assert fingerprint(first, backend="catapult") != fingerprint(renamed.replace("8'd3", "8'd4"), backend="catapult")
