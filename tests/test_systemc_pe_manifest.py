"""Tests for post-HLS ASIC manifest extraction from SystemC/Catapult."""

import importlib.util
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "extract_systemc_pe_manifest.py"
SPEC = importlib.util.spec_from_file_location("extract_systemc_pe_manifest", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


SYSTEMC = """
SC_MODULE(node_0_0) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::In<ac_int<8, false>> input;
  Connections::Out<ac_int<8, false>> output;
  void run() {
    output.Push(input.Pop() + 1);
  }
};

SC_MODULE(top) {
  sc_in_clk clk;
  sc_in<bool> rst;
  void run() { }
};
"""


def test_parse_systemc_module_exposes_ports_and_run_body():
    functions = MODULE.parse_systemc_modules(SYSTEMC)
    pe = functions["node_0_0"]
    assert [name for name, _ in pe.parameters] == ["clk", "rst", "input", "output"]
    assert "output.Push" in pe.body


def test_parse_systemc_module_selects_synthesis_preprocessor_branch():
    source = """
SC_MODULE(pe) {
  sc_in_clk clk;
  void run() {
#ifdef __SYNTHESIS__
    while (1) {
#else
    for (int i = 0; i < 4; ++i) {
#endif
      wait();
    }
  }
};
"""
    function = MODULE.parse_systemc_modules(source)["pe"]
    assert "while (1)" in function.body
    assert "for (int i" not in function.body


def test_systemc_manifest_joins_pe_and_records_reset_contract(tmp_path):
    kernel = tmp_path / "kernel.cpp"
    kernel.write_text(SYSTEMC, encoding="utf-8")
    (tmp_path / "run.tcl").write_text(
        "directive set -CLOCKS {clk {-CLOCK_PERIOD 3.333}}\n"
        "solution library add nangate-45nm_beh\n",
        encoding="utf-8",
    )
    rtl = tmp_path / "build" / "Catapult_1" / "top.v1" / "concat_rtl.v"
    rtl.parent.mkdir(parents=True)
    rtl.write_text(
        "module node_0_0(); endmodule\n"
        "module top(); node_0_0 node_inst(); endmodule\n",
        encoding="utf-8",
    )
    pre = {
        "schema_version": 2,
        "stage": "pre_hls",
        "top": "top",
        "top_arguments": [],
        "pe_instances": [
            {
                "semantic_id": "top/node/pid=0,0",
                "kernel": "node",
                "pid": [0, 0],
                "specialized_function": "node_0_0",
            }
        ],
    }

    manifest = MODULE.build_manifest(kernel, pre, rtl, "top")

    assert manifest["stage"] == "post_hls_enriched"
    assert manifest["backend"] == "systemc"
    assert manifest["summary"]["unmatched_or_ambiguous"] == 0
    record = manifest["pe_instances"][0]["post_hls_records"][0]
    assert record["rtl_root_module"] == "node_0_0"
    assert record["rtl_root_instances"][0]["instance_path"] == "top/node_inst"
    assert record["synthesis_contract"]["clock_period_ns"] == 3.333
    assert manifest["top_interface"]["reset"]["polarity"] == "active_low"


def test_systemc_rtl_discovery_prefers_concat_rtl(tmp_path):
    directory = tmp_path / "build" / "Catapult_1" / "top.v1"
    directory.mkdir(parents=True)
    (directory / "rtl.v").write_text("module top; endmodule\n")
    concat = directory / "concat_rtl.v"
    concat.write_text("module top(); endmodule\n")
    assert MODULE.find_concat_rtl(tmp_path, "top") == concat.resolve()
