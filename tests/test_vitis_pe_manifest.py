import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "extract_vitis_pe_manifest.py"
SPEC = importlib.util.spec_from_file_location("extract_vitis_pe_manifest", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_inlined_allo_function_maps_through_loop_label(tmp_path):
    kernel = tmp_path / "kernel.cpp"
    solution = tmp_path / "solution1"
    log = solution / "solution1.log"
    rtl = solution / "syn" / "verilog"
    _write(
        kernel,
        """
void gemm_2_3_4_4_32_32_16_0_fixed() {
  l_S_mt_nt_0_mt15: for (int mt = 0; mt < 8; ++mt) {
    l_nt15: for (int nt = 0; nt < 8; ++nt) {}
  }
}
""",
    )
    _write(
        log,
        """
INFO: Inlining function 'gemm_2_3_4_4_32_32_16_0_fixed' into 'MXU_4_4_32_32_16_0'
WARNING: Legalizing function name 'MXU_4_4_32_32_16_0.1_Pipeline_l_S_mt_nt_0_mt15_l_nt15' to 'MXU_4_4_32_32_16_0_1_Pipeline_l_S_mt_nt_0_mt15_l_nt15'.
INFO: -- Generating RTL for module 'MXU_4_4_32_32_16_0_1_Pipeline_l_S_mt_nt_0_mt15_l_nt15'
""",
    )
    module = "top_MXU_4_4_32_32_16_0_1_Pipeline_l_S_mt_nt_0_mt15_l_nt15"
    _write(rtl / "pe.v", f"module {module}(); endmodule\n")
    _write(rtl / "parent.v", f"module parent(); {module} pe_U0(); endmodule\n")

    manifest = MODULE.build_manifest(
        kernel, log, rtl, MODULE.DEFAULT_MAPPED_RE
    )
    record = manifest["records"][0]
    assert record["semantic_id"] == "gemm/pid=2,3"
    assert record["loop_labels"][:2] == ["l_S_mt_nt_0_mt15", "l_nt15"]
    assert record["rtl_modules"][0]["name"] == module
    assert record["rtl_root_module"] == "parent"
    assert manifest["rtl_hierarchy"]["module_instances"] == [
        {
            "parent_module": "parent",
            "module": module,
            "instance_name": "pe_U0",
            "source_artifact": str(rtl / "parent.v"),
        }
    ]
    assert record["status"] == "matched"


def test_inlined_function_can_map_through_inner_loop_label(tmp_path):
    kernel = tmp_path / "kernel.cpp"
    solution = tmp_path / "solution1"
    log = solution / "solution1.log"
    rtl = solution / "syn" / "verilog"
    _write(
        kernel,
        """void load_0_0_fixed() {
  outer: for (;;) {
    inner7: for (;;) {}
  }
}
""",
    )
    _write(
        log,
        "INFO: -- Generating RTL for module 'parent_Pipeline_inner7'\n",
    )
    _write(rtl / "pipeline.v", "module top_parent_Pipeline_inner7(); endmodule\n")
    manifest = MODULE.build_manifest(
        kernel, log, rtl, MODULE.DEFAULT_MAPPED_RE
    )
    assert manifest["records"][0]["status"] == "matched"
    assert manifest["records"][0]["vitis_process"] == "parent_Pipeline_inner7"


def test_repeated_hls_calls_map_to_clone_ordinals(tmp_path):
    kernel = tmp_path / "kernel.cpp"
    solution = tmp_path / "solution1"
    log = solution / "solution1.log"
    rtl = solution / "syn" / "verilog"
    _write(kernel, "void pe() { pe_loop: for (int i=0; i<4; ++i) {} }\n")
    _write(
        log,
        """
INFO: detected/extracted 2 process function(s):
 'pe'
 'pe.1'.
INFO: next phase
WARNING: Legalizing function name 'pe.1' to 'pe_1'.
INFO: -- Generating RTL for module 'pe'
INFO: -- Generating RTL for module 'pe_1'
""",
    )
    _write(rtl / "top_pe.v", "module top_pe(); endmodule\n")
    _write(rtl / "top_pe_1.v", "module top_pe_1(); endmodule\n")

    manifest = MODULE.build_manifest(
        kernel, log, rtl, MODULE.DEFAULT_MAPPED_RE, function_name="pe"
    )
    assert [r["semantic_id"] for r in manifest["records"]] == [
        "pe/call=0",
        "pe/call=1",
    ]
    assert [r["rtl_modules"][0]["name"] for r in manifest["records"]] == [
        "top_pe",
        "top_pe_1",
    ]
    assert len(manifest["macro_groups"]) == 1
    group = manifest["macro_groups"][0]
    assert group["member_count"] == 2
    assert group["proof"]["status"] == "proven"
    assert group["proof"]["method"] == "specialized_mlir_emitted_hls_contract"
    assert {member["semantic_id"] for member in group["members"]} == {
        "pe/call=0",
        "pe/call=1",
    }


def test_hls_contract_groups_even_when_generated_rtl_audit_differs():
    records = [
        {
            "semantic_id": "pe/0", "status": "matched",
            "hls_equivalence_hash": "same", "hls_interface_signature": {"ports": []},
            "mapping_mode": "catapult_block", "rtl_equivalence_hash": "rtl-a",
            "rtl_modules": [{"name": "pe_a", "file": "rtl.v"}],
        },
        {
            "semantic_id": "pe/1", "status": "matched",
            "hls_equivalence_hash": "same", "hls_interface_signature": {"ports": []},
            "mapping_mode": "catapult_block", "rtl_equivalence_hash": "rtl-b",
            "rtl_modules": [{"name": "pe_b", "file": "rtl.v"}],
        },
    ]
    groups, audits = MODULE.build_equivalence_groups(records)
    assert len(groups) == 1
    assert groups[0]["rtl_audit"]["status"] == "generated_rtl_diverged"
    assert audits[0]["status"] == "diagnostic_only_representative_will_be_used"


def test_hls_contract_separates_directives_protocols_and_synthesis_context():
    base = {
        "status": "matched", "hls_equivalence_hash": "body",
        "hls_interface_signature": {"ports": [{"type": "ac_channel<int>&", "direction": "in"}]},
        "mapping_mode": "catapult_block", "rtl_equivalence_hash": "rtl",
        "rtl_modules": [{"name": "pe", "file": "rtl.v"}],
    }
    changed = dict(base, semantic_id="pe/1", synthesis_contract={"clock_period_ns": 5})
    original = dict(base, semantic_id="pe/0", synthesis_contract={"clock_period_ns": 10})
    groups, _ = MODULE.build_equivalence_groups([original, changed])
    assert len(groups) == 2


def test_hls_hash_ignores_generated_names_and_loop_labels():
    first = MODULE.parse_cpp_functions(
        """
void loader_0(hls::stream<int> &west, hls::stream<int> &east) {
  loop17: for (int i17=0; i17<4; ++i17) { east.write(west.read()); }
}
"""
    )[0]
    second = MODULE.parse_cpp_functions(
        """
void loader_1(hls::stream<int> &north, hls::stream<int> &south) {
  loop93: for (int j=0; j<4; ++j) { south.write(north.read()); }
}
"""
    )[0]
    assert MODULE.canonical_hls_fingerprint(first)[0] == MODULE.canonical_hls_fingerprint(second)[0]


def test_hls_hash_ignores_multidimensional_array_parameter_names():
    first = MODULE.parse_cpp_functions(
        "void drv_w(ac_ieee_float<binary16> v4460[2][32], "
        "int32_t v4461[2][32]) { v4461[0][0] = 1; }"
    )[0]
    second = MODULE.parse_cpp_functions(
        "void drv_e(ac_ieee_float<binary16> v4668[2][32], "
        "int32_t v4669[2][32]) { v4669[0][0] = 1; }"
    )[0]

    assert first.parameters == (
        ("v4460", "ac_ieee_float<binary16> [2][32]"),
        ("v4461", "int32_t [2][32]"),
    )
    assert second.parameters == (
        ("v4668", "ac_ieee_float<binary16> [2][32]"),
        ("v4669", "int32_t [2][32]"),
    )
    assert MODULE.canonical_hls_fingerprint(first)[0] == (
        MODULE.canonical_hls_fingerprint(second)[0]
    )


def test_hls_hash_checks_interface_types_and_nonblocking_behavior():
    blocking = MODULE.parse_cpp_functions(
        "void pe(hls::stream<int> &x) { int v = x.read(); }"
    )[0]
    different_type = MODULE.parse_cpp_functions(
        "void pe(hls::stream<short> &x) { short v = x.read(); }"
    )[0]
    nonblocking = MODULE.parse_cpp_functions(
        "void pe(hls::stream<int> &x) { int v; bool ok = x.read_nb(v); }"
    )[0]
    block_hash, block_interface = MODULE.canonical_hls_fingerprint(blocking)
    assert block_interface["ports"] == [
        {"type": "hls::stream<int> &", "direction": "in"}
    ]
    assert block_hash != MODULE.canonical_hls_fingerprint(different_type)[0]
    assert block_hash != MODULE.canonical_hls_fingerprint(nonblocking)[0]


def test_rtl_hash_is_alpha_equivalent_but_preserves_behavior_and_child_type():
    first = """
module pe_a(input [7:0] west, output [7:0] east);
wire [7:0] value_a;
assign value_a = west + 8'd1;
assign east = value_a;
endmodule
"""
    renamed = """
module pe_b(input [7:0] north, output [7:0] south);
wire [7:0] temporary_93;
assign temporary_93 = north + 8'd1;
assign south = temporary_93;
endmodule
"""
    changed_constant = renamed.replace("8'd1", "8'd2")
    child_a = "module p(input x, output y); child_a u0(x, y); endmodule"
    child_b = "module p(input x, output y); child_b u0(x, y); endmodule"
    assert MODULE.canonical_verilog_fingerprint(first) == MODULE.canonical_verilog_fingerprint(renamed)
    assert MODULE.canonical_verilog_fingerprint(first) != MODULE.canonical_verilog_fingerprint(changed_constant)
    assert MODULE.canonical_verilog_fingerprint(child_a) != MODULE.canonical_verilog_fingerprint(child_b)


def test_rtl_hash_drops_wire_aliases_and_sorts_independent_assigns():
    direct = """
module a(input [7:0] x, input [7:0] y, output [7:0] z, output [7:0] q);
assign z = x + 8'd1;
assign q = y ^ 8'h5a;
endmodule
"""
    aliased_reordered = """
module b(input [7:0] north, input [7:0] west, output [7:0] south, output [7:0] east);
wire [7:0] temporary;
assign temporary = north;
assign east = west ^ 8'h5a;
assign south = temporary + 8'd1;
endmodule
"""
    assert MODULE.canonical_verilog_fingerprint(direct) == MODULE.canonical_verilog_fingerprint(aliased_reordered)
    assert MODULE.canonical_verilog_fingerprint(direct) != MODULE.canonical_verilog_fingerprint(direct.replace("^", "|"))
    assert MODULE.canonical_verilog_fingerprint(direct) != MODULE.canonical_verilog_fingerprint(direct.replace("[7:0]", "[8:0]", 1))


def test_rtl_canonicalizer_keeps_port_direction_buckets():
    rtl = """
module p(a, z, io);
input [3:0] a; output [3:0] z; inout io;
assign z = a;
endmodule
"""
    canonical = MODULE.RTLCanonicalizer().canonicalize(rtl)
    assert len(canonical.inputs) == 1
    assert len(canonical.outputs) == 1
    assert len(canonical.inouts) == 1
    assert len(canonical.combinational) == 1


def test_empty_function_is_reported_without_becoming_an_error(tmp_path):
    kernel = tmp_path / "kernel.cpp"
    solution = tmp_path / "solution1"
    log = solution / "solution1.log"
    rtl = solution / "syn" / "verilog"
    _write(kernel, "void gemm_0_5_fixed() {}\n")
    _write(log, "INFO: synthesis completed\n")
    rtl.mkdir(parents=True)
    manifest = MODULE.build_manifest(kernel, log, rtl, MODULE.DEFAULT_MAPPED_RE)
    assert manifest["records"][0]["status"] == "empty_source"
    assert manifest["summary"]["empty_source"] == 1
    assert manifest["summary"]["unmatched_or_ambiguous"] == 0


def test_ambiguous_rtl_suffix_match_is_not_grouped(tmp_path):
    kernel = tmp_path / "kernel.cpp"
    solution = tmp_path / "solution1"
    log = solution / "solution1.log"
    rtl = solution / "syn" / "verilog"
    _write(kernel, "void pe() { for (int i=0; i<4; ++i) {} }\n")
    _write(log, "INFO: -- Generating RTL for module 'pe'\n")
    _write(rtl / "one.v", "module top_pe(); endmodule\n")
    _write(rtl / "two.v", "module wrapper_pe(); endmodule\n")
    manifest = MODULE.build_manifest(
        kernel, log, rtl, MODULE.DEFAULT_MAPPED_RE, function_name="pe"
    )
    assert manifest["records"][0]["status"] == "ambiguous_rtl_module"
    assert manifest["macro_groups"] == []


def test_log_discovery_prefers_solution_log(tmp_path):
    solution = tmp_path / "solution1"
    _write(solution / "solution1.log", "solution")
    _write(tmp_path / "vitis_hls.log", "driver")
    assert MODULE._find_log(solution) == solution / "solution1.log"


def test_vitis_synthesis_contract_records_tool_part_and_clock():
    contract = MODULE.parse_vitis_synthesis_contract(
        "Vitis HLS - High-Level Synthesis v2022.1 (64-bit)\n"
        "INFO: Running: set_part {xcu280-test}\n"
        "INFO: Running: create_clock -period 8.00\n"
    )
    assert contract == {
        "backend": "vitis_hls",
        "tool_version": "2022.1",
        "target_part": "xcu280-test",
        "clock_period_ns": 8.0,
        "hierarchy_policy": "vitis_dataflow_process",
    }


def test_tcl_manifest_uses_only_builtin_dict_and_list(tmp_path):
    manifest = {
        "schema_version": 1,
        "summary": {"matched": 2, "safe": True},
        "members": ["pe/call=0", "pe/call=$1"],
    }
    path = tmp_path / "manifest.tcl"
    path.write_text(MODULE.render_tcl_manifest(manifest), encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    assert "package require" not in text
    assert "set allo_asic_manifest [dict create" in text
    if shutil.which("tclsh"):
        check = tmp_path / "check.tcl"
        check.write_text(
            text
            + '\nputs [dict get $allo_asic_manifest summary matched]\n'
            + 'puts [lindex [dict get $allo_asic_manifest members] 1]\n',
            encoding="utf-8",
        )
        result = subprocess.run(
            ["tclsh", str(check)], check=True, capture_output=True, text=True
        )
        assert result.stdout.splitlines() == ["2", "pe/call=$1"]


def test_pre_hls_identity_replaces_name_derived_pid_in_unified_manifest():
    pre = {
        "schema_version": 2,
        "stage": "pre_hls",
        "pe_instances": [
            {
                "semantic_id": "top/MXU/gemm/pid=0,1",
                "kernel": "gemm",
                "pid": [0, 1],
                "specialized_function": "gemm_0_1_4_4_32_32_16_0",
            }
        ],
        "channels": [],
        "macro_groups": [],
    }
    record = {
        "semantic_id": "gemm/pid=0,1",
        "kernel": "gemm",
        "pid": [0, 1],
        "hls_function": "gemm_0_1_4_4_32_32_16_0_fixed",
        "hls_equivalence_hash": "hls",
        "rtl_equivalence_hash": "rtl",
        "rtl_modules": [{"name": "pe", "file": "pe.v"}],
        "status": "matched",
    }
    post = {
        "schema_version": 1,
        "inputs": {},
        "records": [record],
        "macro_groups": [],
        "candidate_groups": [],
        "summary": {
            "selected_functions": 1,
            "records": 1,
            "matched": 1,
            "empty_source": 0,
            "unmatched_or_ambiguous": 0,
            "macro_classes": 1,
            "repeated_macro_classes": 0,
            "instances_in_repeated_classes": 0,
            "candidate_classes": 0,
        },
    }
    merged = MODULE.merge_pre_hls_manifest(pre, post)
    assert merged["stage"] == "post_hls_enriched"
    assert merged["summary"]["joined_post_hls_records"] == 1
    assert merged["post_hls_records"][0]["semantic_id"] == (
        "top/MXU/gemm/pid=0,1"
    )
    assert merged["macro_groups"][0]["members"][0]["semantic_id"] == (
        "top/MXU/gemm/pid=0,1"
    )


def test_debug_artifacts_are_exact_hash_inputs(tmp_path):
    kernel = tmp_path / "kernel.cpp"
    log = tmp_path / "solution1.log"
    rtl = tmp_path / "syn" / "verilog"
    _write(kernel, "void pe() { int generated = 1; }\n")
    _write(log, "INFO: -- Generating RTL for module 'pe'\n")
    _write(rtl / "top_pe.v", "module top_pe(); wire generated; endmodule\n")
    manifest = MODULE.build_manifest(
        kernel, log, rtl, MODULE.DEFAULT_MAPPED_RE, function_name="pe"
    )
    debug = tmp_path / "debug"
    MODULE.write_debug_artifacts(debug, kernel, log, rtl, manifest)
    hls_text = (debug / "canonical" / "hls" / "pe.txt").read_text().strip()
    rtl_text = (
        debug / "canonical" / "rtl" / "top_pe.txt"
    ).read_text().strip()
    function = MODULE.parse_cpp_functions(kernel.read_text())[0]
    assert MODULE.hashlib.sha256(hls_text.encode()).hexdigest() == (
        MODULE.canonical_hls_fingerprint(function)[0]
    )
    assert MODULE.hashlib.sha256(rtl_text.encode()).hexdigest() == (
        MODULE.canonical_verilog_fingerprint((rtl / "top_pe.v").read_text())
    )
    assert (debug / "parsed-vitis-log.json").is_file()
