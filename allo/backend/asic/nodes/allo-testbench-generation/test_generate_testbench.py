# Copyright Allo authors. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).with_name("generate_testbench.py")
SPEC = importlib.util.spec_from_file_location("allo_testbench_generator", SCRIPT)
GENERATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GENERATOR)


def test_vitis_artifact_parsers():
    kernel = """
    void top(int32_t A[64], int32_t B[64], int32_t C[64]) {
      #pragma HLS interface m_axi port=A offset=slave bundle=gmem0
      #pragma HLS interface m_axi port=B offset=slave bundle=gmem1
      #pragma HLS interface m_axi port=C offset=slave bundle=gmem2
    }
    """
    assert GENERATOR._parse_cpp_arguments(kernel, "top") == ["A", "B", "C"]
    assert GENERATOR._parse_m_axi_pragmas(kernel) == {
        "A": "gmem0",
        "B": "gmem1",
        "C": "gmem2",
    }


def test_vitis_width_expressions_and_pointer_registers():
    rtl = """
    parameter C_M_AXI_GMEM0_DATA_WIDTH = 32;
    parameter C_M_AXI_GMEM0_WSTRB_WIDTH = (C_M_AXI_GMEM0_DATA_WIDTH / 8);
    output [C_M_AXI_GMEM0_DATA_WIDTH - 1:0] m_axi_gmem0_WDATA;
    output [C_M_AXI_GMEM0_WSTRB_WIDTH - 1:0] m_axi_gmem0_WSTRB;
    input ap_clk;
    """
    ports, parameters = GENERATOR._parse_ports(rtl)
    assert parameters["C_M_AXI_GMEM0_WSTRB_WIDTH"] == 4
    assert ports["m_axi_gmem0_WDATA"]["width"] == 32
    assert ports["m_axi_gmem0_WSTRB"]["width"] == 4

    control = """
    ADDR_A_DATA_0 = 6'h10,
    ADDR_A_DATA_1 = 6'h14,
    """
    assert GENERATOR._parse_pointer_registers(control, ["A"]) == {
        "A": {"data_0": 0x10, "data_1": 0x14}
    }


def test_vitis_integral_axi_packing_ratios():
    assert GENERATOR._packing_ratio(32, 32) == 1
    assert GENERATOR._packing_ratio(16, 32) == 2
    assert GENERATOR._packing_ratio(8, 64) == 8


def test_vitis_rejects_nonintegral_axi_packing_ratios():
    try:
        GENERATOR._packing_ratio(24, 32)
    except RuntimeError as error:
        assert "integral AXI packing ratio" in str(error)
    else:
        raise AssertionError("non-integral AXI packing ratio was accepted")


def test_vitis_simulation_templates_use_zero_based_memory_and_explicit_vcd():
    template_dir = SCRIPT.parent / "templates" / "vitis"
    memory_bfm = (template_dir / "vitis-axi-memory-bfm.sv").read_text()
    testbench = (template_dir / "testbench.sv.tpl").read_text()

    assert "BASE_ADDR = 'h0" in memory_bfm
    assert "ELEMENTS_PER_WORD = DATA_WIDTH / ELEMENT_WIDTH" in memory_bfm
    assert "packed_lane_index*ELEMENT_WIDTH +: ELEMENT_WIDTH" in memory_bfm
    assert '$fscanf(file_handle, "%h", element_value)' in memory_bfm
    assert '$test$plusargs("ALLO_DUMP_VCD")' in testbench
    assert '$dumpfile("outputs/run.vcd")' in testbench
    assert "$dumpvars(0, allo_generated_testbench)" in testbench


def test_vitis_bfms_drive_away_from_propagated_clock_edge():
    template_dir = SCRIPT.parent / "templates" / "vitis"
    memory_bfm = (template_dir / "vitis-axi-memory-bfm.sv").read_text()

    axil_bfm = (template_dir / "vitis-axilite-master-bfm.sv").read_text()
    testbench = (template_dir / "testbench.sv.tpl").read_text()

    assert "ALLO_BAGL_BFM_DRIVE_DELAY_NS=%f" in memory_bfm
    assert "awready <= #(drive_delay_ns)" in memory_bfm
    assert "rvalid <= #(drive_delay_ns)" in memory_bfm
    assert "ALLO_BAGL_INPUT_DELAY_NS=%f" in axil_bfm
    assert "@(negedge clk);" in axil_bfm
    assert "ALLO_BAGL_CLK_INS_SRC_LAT_NS=%f" in testbench
    assert (
        "#(allo_bagl_clock_compensation_ns + allo_bagl_output_delay_ns);" in testbench
    )


def test_systemc_reset_starts_deasserted_before_workload_assertion():
    template = (
        SCRIPT.parent / "templates" / "catapult" / "testbench.sv.tpl"
    ).read_text()

    assert "@RESET@ = 1'b@RESET_DEASSERTED@;" in template
    assert "@RESET@ = 1'b@RESET_ASSERTED@;" not in template


def test_systemc_reuses_synchronous_memory_generation(tmp_path, monkeypatch):
    inputs = tmp_path / "inputs"
    outputs = tmp_path / "outputs"
    rtl = inputs / "backend-rtl"
    rtl.mkdir(parents=True)
    outputs.mkdir()
    (rtl / "concat_rtl.v").write_text(
        "module top(input clk, input rst, output done, "
        "output [1:0] v10_radr, output v10_re, input [15:0] v10_q, "
        "input v10_rrdy, output [1:0] v11_wadr, output v11_we, "
        "output [15:0] v11_d, input v11_wrdy); endmodule\n"
    )

    def port(name, direction, width):
        return {"name": name, "direction": direction, "width": width}

    read_roles = {
        "read_address": port("v10_radr", "output", 2),
        "read_enable": port("v10_re", "output", 1),
        "read_data": port("v10_q", "input", 16),
        "read_ready": port("v10_rrdy", "input", 1),
    }
    write_roles = {
        "write_address": port("v11_wadr", "output", 2),
        "write_enable": port("v11_we", "output", 1),
        "write_data": port("v11_d", "output", 16),
        "write_ready": port("v11_wrdy", "input", 1),
    }
    manifest = {
        "backend": "systemc",
        "top": "top",
        "rtl_artifact": {"published_path": "backend-rtl/concat_rtl.v"},
        "top_interface": {
            "protocol": "systemc_connections",
            "clock": {"name": "clk", "edge": "rising"},
            "reset": {
                "name": "rst",
                "polarity": "active_low",
                "default_asserted_cycles": 2,
            },
            "completion": {"kind": "top_done", "port": "done", "active_level": 1},
        },
        "top_arguments": [
            {
                "name": "A",
                "catapult_argument": "v10",
                "semantic_direction": "input",
                "interface_protocol": "systemc_sync_memory_read",
                "interface": {
                    "roles": read_roles,
                    "layout": "row_major",
                    "element_bits": 16,
                    "element_count": 4,
                    "data_width": 16,
                    "address_width": 2,
                    "address_capacity": 4,
                    "read_latency_cycles": 1,
                },
                "rtl_ports": list(read_roles.values()),
                "triosy_ports": [],
            },
            {
                "name": "C",
                "catapult_argument": "v11",
                "semantic_direction": "output",
                "interface_protocol": "systemc_sync_memory_write",
                "interface": {
                    "roles": write_roles,
                    "layout": "row_major",
                    "element_bits": 16,
                    "element_count": 4,
                    "data_width": 16,
                    "address_width": 2,
                    "address_capacity": 4,
                    "read_latency_cycles": None,
                },
                "rtl_ports": list(write_roles.values()),
                "triosy_ports": [],
            },
        ],
    }
    (inputs / "asic-manifest-final.json").write_text(json.dumps(manifest))
    vector = lambda name: {
        "element_bits": 16,
        "element_count": 4,
        "file": f"workload-vectors/call_000/{name}.initial.hex",
    }
    workload = {
        "top_function": "top",
        "call_signature": ["A", "C"],
        "default_timeout_cycles": 100,
        "calls": [
            {
                "name": "memory",
                "reset_before": True,
                "arguments": {"A": vector("A"), "C": vector("C")},
                "expected": {
                    "C": {
                        "element_count": 4,
                        "file": "workload-vectors/call_000/C.expected.hex",
                    }
                },
            }
        ],
    }
    monkeypatch.setattr(GENERATOR, "INPUTS", inputs)
    monkeypatch.setattr(GENERATOR, "OUTPUTS", outputs)
    monkeypatch.setattr(GENERATOR, "TEMPLATES", SCRIPT.parent / "templates")
    contract = GENERATOR._generate_catapult(
        workload, {"backend": "systemc", "clock_period_ns": 10.0}
    )
    testbench = (outputs / "testbench.sv").read_text()
    assert "assign v10_rrdy = 1'b1;" in testbench
    assert "assign v11_wrdy = 1'b1;" in testbench
    assert "if ((rst === 1'b1) && (v10_re === 1'b1)) begin" in testbench
    assert (
        "v10_q <= #(allo_bagl_bfm_drive_delay_ns) " "memory_A[v10_radr];"
    ) in testbench
    assert "if (done === 1'b1) completion_seen[0] <= 1'b1;" in testbench
    assert "rst = 1'b0;" in testbench
    assert "rst = 1'b1;" in testbench
    assert contract["backend"] == "systemc"
    assert contract["completion_output_count"] == 1
