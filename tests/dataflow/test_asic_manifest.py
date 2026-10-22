# Copyright Allo authors. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Does the ASIC manifest still describe the design it claims to describe?

`allo/backend/asic` plans a chip from `asic_manifest`: it places macros from
the PID deltas in `semantic_id`, weights the wires between them by the stream
widths in `channels[].type`, and decides which RTL modules are the same macro
from the equivalence records. None of that is checkable by looking at the
emitted Verilog, so nothing downstream can catch a manifest that quietly stops
matching the design -- it just plans a different chip.

So the assertions here are of two kinds, and both matter:

* **against the design**: every expectation is computed from
  `TPU.architecture` -- its units, their `instances=`, its channels and their
  shapes -- rather than typed in, so changing TinyTPU changes what this file
  demands and a manifest that did not follow fails;
* **against the consumer**: the flow's own `plan_macros.graph_pin_sides` and
  `plan_physical_intent.semantic_kernel`/`semantic_pid` are loaded from
  `allo/backend/asic/nodes/` and run on the emitted manifest, so "the consumer
  accepts it" is executed rather than asserted.

The geometry test is the sharp one. The flow is told nothing about systolic
arrays; it is given PIDs and channel endpoints. If it reads back `a_fwd` going
east and `p_fwd` going south, the manifest carried the array.
"""

import importlib.util
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import allo
import allo.dataflow as df
from allo.backend import asic_manifest
from allo.ir.types import int32, Stream
from allo.passes import analyze_arg_load_store

from examples.tinytpu.microarch_isa import TPU, tinytpu_isa

NODES = Path(allo.__file__).parent / "backend" / "asic" / "nodes"
MANIFEST_CONFIG = {
    "enabled": True,
    "path": "asic-manifest.json",
    "debug_artifacts": True,
    "debug_dir": "asic-debug",
}


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _vitis_fixture(pre, root, missing_sink=False):
    """Write reduced emitted-HLS, log, and RTL artifacts for one chain."""
    source = root / "kernel.cpp"
    log = root / "solution1.log"
    rtl = root / "verilog"
    top = pre["top"]
    functions = []
    log_lines = []
    for pe in pre["pe_instances"]:
        function = pe["rtl_instance"]
        functions.append(f"void {function}() {{ int value = 1; }}")
        process = function if pe["kernel"] == "source" else f"{function}.1"
        legalized = process.replace(".", "_")
        log_lines.append(f"INFO: -- Generating RTL for module '{process}'")
        if process != legalized:
            log_lines.append(
                f"WARNING: Legalizing function name '{process}' to '{legalized}'."
            )
        if missing_sink and pe["kernel"] == "sink":
            continue
        module = f"{top}_{legalized}"
        child = f"{module}_Pipeline_LOOP_1"
        _write(
            rtl / f"{module}.v",
            f"module {module}(input ap_clk); {child} process(); endmodule\n",
        )
        _write(rtl / f"{child}.v", f"module {child}(); endmodule\n")
    _write(source, "\n".join(functions) + "\n")
    _write(log, "\n".join(log_lines) + "\n")
    _write(rtl / f"{top}.v", f"module {top}(); endmodule\n")
    _write(rtl / f"{top}_gmem0_m_axi.v", f"module {top}_gmem0_m_axi(); endmodule\n")
    return source, log, rtl


def _systemc_fixture(project):
    """Write the minimal artifacts that define the supported SystemC path."""
    rtl = project / "build/Catapult/top.v1"
    _write(
        project / "kernel.cpp",
        """
SC_MODULE(pe_0) {
  Connections::In<ac_int<16, true> > in;
  Connections::Out<ac_int<16, true> > out;
  void run() { while (1) { int value = 1; wait(); } }
};
""",
    )
    _write(
        rtl / "concat_rtl.v",
        """
module top(clk, rst, done, v10_radr, v10_re, v10_q, v10_rrdy,
           v11_wadr, v11_d, v11_we, v11_wrdy);
  input clk; input rst; output done;
  output [1:0] v10_radr; output v10_re;
  input [15:0] v10_q; input v10_rrdy;
  output [1:0] v11_wadr; output [15:0] v11_d;
  output v11_we; input v11_wrdy;
  pe_0 u0(.clk(clk), .rst(rst));
endmodule
module pe_0(clk, rst);
  input clk; input rst;
  pe_0_run run_inst(.clk(clk), .rst(rst));
endmodule
module pe_0_run(clk, rst);
  input clk; input rst;
endmodule
""",
    )
    _write(
        project / "run.tcl",
        "directive set -CLOCK_PERIOD 8.0\n"
        "solution library add nangate-45nm_beh\n",
    )
    _write(
        rtl / "catapult.log",
        "Catapult Ultra Synthesis 2024.3_16.10\n",
    )
    return rtl


def _single_pe_pre(function="pe", semantic_id="top/pe/pid=0"):
    return {
        "schema_version": 2,
        "stage": "pre_hls",
        "backend": "vitis",
        "top": "top",
        "pe_instances": [
            {
                "semantic_id": semantic_id,
                "kernel": "pe",
                "pid": [0],
                "rtl_instance": function,
                "pre_hls_implementation_contract": {
                    "canonical_specialized_mlir_hash": "mlir"
                },
                "pre_hls_implementation_contract_hash": "pre-hls",
                "ports": [],
            }
        ],
        "channels": [],
        "macro_groups": [],
        "summary": {},
    }


def _load(name, relative):
    """Load one of the flow's node scripts as a module, by path.

    They are not a package -- several share a basename and several assume
    their own directory is the working directory -- so importing one is by
    file, which is also how the node itself runs them.
    """
    path = NODES / relative
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


plan_macros = _load(
    "allo_asic_plan_macros",
    "allo-asic-macro-plan/plan_macros.py",
)
physical_intent = _load(
    "allo_asic_physical_intent",
    "allo-asic-physical-intent/plan_physical_intent.py",
)


@pytest.fixture(scope="module")
def tinytpu():
    """The shipped TinyTPU, compiled once, as its pre-HLS manifest."""
    schedule = df.customize(tinytpu_isa)
    arguments = df._build_manifest_top_arguments(
        schedule.func_args[schedule.top_func_name],
        [a.top_name for a in schedule.func_args[schedule.top_func_name]],
        analyze_arg_load_store(schedule.module)[schedule.top_func_name],
        asic_manifest.DIRECTION_MAP,
    )
    return asic_manifest.collect(
        schedule.module,
        schedule.top_func_name,
        top_arguments=arguments,
        mappings=getattr(tinytpu_isa, "mappings", None),
        backend="vitis",
        clock_period_ns=3.33,
    )


def _expected_instances():
    """{kernel: [grid extent]} straight off the composed architecture."""
    parameters = TPU.architecture.parameters
    return {
        unit.name: [int(eval(dim, {}, dict(parameters))) for dim in unit.instances]
        for unit in TPU.architecture.units
    }


def _expected_channel_count():
    """One channel per element of every declared `Channel`, arrays included."""
    parameters = TPU.architecture.parameters
    total = 0
    for channel in TPU.architecture.channels:
        count = 1
        for dim in channel.shape:
            count *= int(eval(dim, {}, dict(parameters)))
        total += count
    return total


def test_manifest_describes_the_composed_architecture(tinytpu):
    assert tinytpu["stage"] == "pre_hls"
    assert tinytpu["schema_version"] == asic_manifest.SCHEMA_VERSION
    assert tinytpu["top"] == TPU.architecture.name

    expected = _expected_instances()
    assert tinytpu["kernel_grids"] == expected
    assert len(tinytpu["pe_instances"]) == sum(
        _product(grid) for grid in expected.values()
    )
    assert len(tinytpu["channels"]) == _expected_channel_count()
    assert [memory["name"] for memory in tinytpu["memories"]] == [
        memory.name for memory in TPU.architecture.memories
    ]


def _product(values):
    total = 1
    for value in values:
        total *= value
    return total


def test_every_wired_channel_has_one_producer_and_one_consumer(tinytpu):
    """`Architecture._check` demands it of the design; the manifest must say it.

    A channel that reaches the flow with two producers, or half-wired, is a
    netlist the placer will happily lay out and the hardware cannot implement.

    The exception is real and is kept rather than hidden: a stream ARRAY
    declares an element per grid point, and each of TinyTPU's three chains
    leaves its terminal element unwired -- the last column forwards no weights
    or activations east, and the bottom row sends `cw` rather than a partial
    sum south. That is `3 * T` elements, and the count moving is how a wiring
    change would show up here.
    """
    unconnected = []
    for channel in tinytpu["channels"]:
        roles = [endpoint["role"] for endpoint in channel["endpoints"]]
        if not roles:
            unconnected.append(channel["channel_id"])
            continue
        assert sorted(roles) == [
            "consumer",
            "producer",
        ], f"{channel['channel_id']} has endpoints {roles}"
    assert len(unconnected) == 3 * TPU.architecture.parameters["T"]
    assert tinytpu["summary"]["unconnected_channels"] == len(unconnected)


def test_memories_name_the_axi_adapters_the_backend_will_emit(tinytpu):
    """The `gmem<i>_m_axi` adapters are `bundle=gmem{i}` on argument `i`.

    `postprocess_hls_code` pragmas them that way, and the shipped synthesis
    hierarchy report names `gmem0_m_axi_U` .. `gmem3_m_axi_U`.
    """
    top = tinytpu["top"]
    for ordinal, memory in enumerate(tinytpu["memories"]):
        assert memory["bundle"] == f"gmem{ordinal}"
        assert memory["rtl_adapter_module"] == f"{top}_gmem{ordinal}_m_axi"
        assert memory["pe"] in {pe["semantic_id"] for pe in tinytpu["pe_instances"]}


def test_semantic_ids_round_trip_through_the_flows_own_parsers(tinytpu):
    for pe in tinytpu["pe_instances"]:
        semantic_id = pe["semantic_id"]
        assert physical_intent.semantic_pid(semantic_id) == tuple(pe["pid"])
        assert physical_intent.semantic_kernel(semantic_id) == (
            f"{tinytpu['top']}/{pe['kernel']}"
        )


def test_the_flows_placer_recovers_the_systolic_geometry(tinytpu):
    """Run the consumer, and check it reads the array back out.

    `graph_pin_sides` is `plan_macros`'s own function: it joins
    `channels[].endpoints[].accesses[].port_ordinal` to `pe_instances[].ports`
    and turns PID deltas into compass sides. TinyTPU sends activations east
    (`a_fwd`) and partial sums south (`p_fwd`), and nothing in the manifest
    says so in words.
    """
    sides = plan_macros.graph_pin_sides(tinytpu)
    assert sides, "the consumer resolved no stream pins at all"

    by_pe = {pe["semantic_id"]: pe for pe in tinytpu["pe_instances"]}
    interior = f"{tinytpu['top']}/pe/pid=1,1"
    observed = {}
    for port in by_pe[interior]["ports"]:
        decision = sides.get((interior, port["ordinal"]))
        if decision is None:
            continue
        observed[(port["stream"].rsplit("_", 2)[0], port["direction"])] = decision[
            "side"
        ]
    assert observed[("a_fwd", "in")] == "W"
    assert observed[("a_fwd", "out")] == "E"
    assert observed[("p_fwd", "in")] == "N"
    assert observed[("p_fwd", "out")] == "S"

    methods = {decision["method"] for decision in sides.values()}
    assert "same_kernel_neighbor" in methods


def test_the_compilation_nodes_pre_hls_gate_accepts_it(tinytpu):
    """The checks `nodes/allo-asic-compilation/validate_build.py` runs."""
    assert tinytpu["stage"] == "pre_hls"
    assert tinytpu["pe_instances"]
    assert tinytpu["summary"]["unmatched_or_ambiguous"] == 0
    assert tinytpu["summary"]["unjoined_post_hls_records"] == 0


def test_a_manifest_that_stops_describing_the_design_is_refused(tinytpu):
    """The emitter checks what the consumers assume and never state.

    Each of these is a `KeyError`, a `StopIteration` or a silently misplaced
    macro several nodes downstream, so it is caught where it is written.
    """
    broken = json.loads(json.dumps(tinytpu))
    broken["channels"][0]["endpoints"][0]["pe"] = "nowhere/at/pid=0"
    with pytest.raises(RuntimeError, match="unknown PE"):
        asic_manifest._check(broken)

    broken = json.loads(json.dumps(tinytpu))
    broken["channels"][0]["endpoints"][0]["accesses"][0]["port_ordinal"] = 99
    with pytest.raises(RuntimeError, match="does not have"):
        asic_manifest._check(broken)

    broken = json.loads(json.dumps(tinytpu))
    broken["pe_instances"][1]["semantic_id"] = broken["pe_instances"][0]["semantic_id"]
    with pytest.raises(RuntimeError, match="duplicate"):
        asic_manifest._check(broken)


def test_build_manifest_top_arguments_uses_the_realized_order():
    """A region's arguments are not emitted in the order they were declared.

    The flow binds the frozen workload's vectors to ports by this order, so
    getting it from the declaration rather than from the build is a silent
    wrong answer. Same contract as the flow's own
    `nodes/allo-asic-compilation/test_catapult_manifest.py`.
    """
    from types import SimpleNamespace

    source = [
        SimpleNamespace(name="A", top_name="A", shape=(4,), dtype="i16"),
        SimpleNamespace(name="B", top_name="B", shape=(8,), dtype="i32"),
        SimpleNamespace(name="C", top_name="C", shape=(2,), dtype="f16"),
    ]
    arguments = df._build_manifest_top_arguments(
        source,
        ["A", "C", "B"],
        ["in", "out", "both"],
        {"in": "input", "out": "output", "both": "inout"},
    )
    assert [item["name"] for item in arguments] == ["A", "C", "B"]
    assert [item["shape"] for item in arguments] == [[4], [2], [8]]
    assert [item["type"] for item in arguments] == ["i16", "f16", "i32"]
    assert [item["direction"] for item in arguments] == [
        "input",
        "output",
        "inout",
    ]
    assert [item["ordinal"] for item in arguments] == [0, 1, 2]


# ---------------------------------------------------------------------------
# A second design, so nothing here is TinyTPU-shaped by accident
# ---------------------------------------------------------------------------


def _chain_region(depth=4):
    Ty = int32
    M = 8

    @df.region()
    def chain(A: Ty[M], B: Ty[M]):
        pipe: Stream[Ty, depth]

        @df.kernel(mapping=[1], args=[A])
        def source(local_a: Ty[M]):
            for i in allo.grid(M):
                pipe.put(local_a[i])

        @df.kernel(mapping=[1], args=[B])
        def sink(local_b: Ty[M]):
            for i in allo.grid(M):
                local_b[i] = pipe.get() + 1

    return chain


def test_build_writes_what_the_compilation_node_asks_for(tmp_path):
    """`configs["asic_manifest"]` is the whole interface the node uses.

    It calls `build(project, target, mode, configs)` and then expects the
    manifest beside the project -- this is that contract, on a design that is
    not TinyTPU.
    """
    project = tmp_path / "top.prj"
    df.build(
        _chain_region(),
        target="vitis_hls",
        mode="csyn",
        project=str(project),
        configs={
            "frequency": 300.0,
            "device": "u280",
            "asic_manifest": dict(MANIFEST_CONFIG),
        },
    )
    manifest = json.loads((project / "asic-manifest.json").read_text())
    assert (project / "asic-manifest.tcl").is_file()
    assert (project / "asic-debug" / "pe-instances.tsv").is_file()
    assert manifest["stage"] == "pre_hls"
    assert manifest["backend"] == "vitis"
    assert {pe["kernel"] for pe in manifest["pe_instances"]} == {"source", "sink"}
    assert len(manifest["channels"]) == 1
    assert manifest["channels"][0]["type"] == "!allo.stream<i32, 4>"
    assert [memory["bundle"] for memory in manifest["memories"]] == [
        "gmem0",
        "gmem1",
    ]
    assert manifest["top_interface"]["clock"]["name"] == "ap_clk"
    assert sum(group["member_count"] for group in manifest["candidate_groups"]) == 2

    by_kernel = {pe["kernel"]: pe for pe in manifest["pe_instances"]}
    assert by_kernel["source"]["ports"][0]["operation"] == "put"
    assert by_kernel["sink"]["ports"][0]["operation"] == "get"
    for pe in by_kernel.values():
        contract = pe["pre_hls_implementation_contract"]
        assert contract["canonical_specialized_mlir_hash"] == (
            pe["pre_hls_equivalence_hash"]
        )
        assert contract["stream_interface"][0]["type"] == "!allo.stream<i32, 4>"

        raw = (
            project / "asic-debug/functions" / f"{pe['rtl_instance']}.mlir"
        ).read_text()
        canonical = (
            (project / "asic-debug/canonical/pre-hls" / f"{pe['rtl_instance']}.txt")
            .read_text()
            .strip()
        )
        assert canonical == asic_manifest.canonical_pre_hls_ir(raw)
        assert hashlib.sha256(canonical.encode()).hexdigest() == (
            pe["pre_hls_equivalence_hash"]
        )


def test_pre_hls_canonicalization_ignores_names_but_keeps_behavior():
    first = """
func.func @pe_0(%left: i32, %right: i32) {
  %sum = arith.addi %left, %right : i32
  %constant = arith.constant 1 : i32
  %out = arith.addi %sum, %constant : i32
  func.return
}
"""
    renamed = first.replace("@pe_0", "@pe_1").replace("%sum", "%generated")
    changed = renamed.replace("constant 1", "constant 2")
    assert asic_manifest.canonical_pre_hls_ir(first) == (
        asic_manifest.canonical_pre_hls_ir(renamed)
    )
    assert asic_manifest.canonical_pre_hls_ir(first) != (
        asic_manifest.canonical_pre_hls_ir(changed)
    )


def test_pre_hls_contract_keeps_stream_depth():
    manifests = []
    for depth in (2, 4):
        schedule = df.customize(_chain_region(depth))
        manifests.append(asic_manifest.collect(schedule.module, schedule.top_func_name))
    source = [
        next(pe for pe in manifest["pe_instances"] if pe["kernel"] == "source")
        for manifest in manifests
    ]
    assert source[0]["pre_hls_implementation_contract_hash"] != (
        source[1]["pre_hls_implementation_contract_hash"]
    )


def test_enrichment_joins_compiler_hls_and_rtl_evidence(tmp_path):
    """The final manifest joins compiler identity through emitted HLS to RTL."""
    project = tmp_path / "top.prj"
    df.build(
        _chain_region(),
        target="vitis_hls",
        mode="csyn",
        project=str(project),
        configs={
            "frequency": 300.0,
            "device": "u280",
            "asic_manifest": dict(MANIFEST_CONFIG),
        },
    )
    pre = json.loads((project / "asic-manifest.json").read_text())
    top = pre["top"]
    source, log, rtl = _vitis_fixture(pre, tmp_path / "artifacts")
    final = asic_manifest.enrich(pre, rtl, source, log)
    assert final["stage"] == "post_hls_enriched"
    assert final["summary"]["unmatched_or_ambiguous"] == 0
    assert final["summary"]["unjoined_post_hls_records"] == 0

    roots = {
        pe["semantic_id"]: pe["post_hls_records"][0]["rtl_root_module"]
        for pe in final["pe_instances"]
    }
    assert roots == {
        f"{top}/source/pid=0": f"{top}_source_0",
        f"{top}/sink/pid=0": f"{top}_sink_0_1",
    }
    for pe in final["pe_instances"]:
        record = pe["post_hls_records"][0]
        assert record["implementation_contract"]["pre_hls_contract_hash"] == (
            pe["pre_hls_implementation_contract_hash"]
        )
        assert len(record["rtl_dependency_modules"]) == 2
        assert record["rtl_dependency_modules"][1].endswith("_Pipeline_LOOP_1")

    # Different specialized MLIR contracts remain distinct even when reduced
    # emitted-HLS fixtures happen to have identical bodies.
    assert len(final["macro_groups"]) == 2
    for group in final["macro_groups"]:
        assert group["proof"]["status"] == "proven"
        assert group["proof"]["method"] == ("specialized_mlir_emitted_hls_contract")
        assert group["rtl_audit"]["authority"] is False
        assert group["member_count"] == 1
        assert group["representative"] in roots
    assert f"{top}_gmem0_m_axi" in final["support_modules"]

    # The macro planner reads the enriched records; it must not choke on them.
    assert plan_macros.graph_pin_sides(final)


def test_enrichment_reports_a_pe_it_could_not_join(tmp_path):
    """Silence is the failure mode worth guarding: the count must move."""
    project = tmp_path / "top.prj"
    df.build(
        _chain_region(),
        target="vitis_hls",
        mode="csyn",
        project=str(project),
        configs={
            "frequency": 300.0,
            "device": "u280",
            "asic_manifest": dict(MANIFEST_CONFIG),
        },
    )
    pre = json.loads((project / "asic-manifest.json").read_text())
    source, log, rtl = _vitis_fixture(pre, tmp_path / "artifacts", missing_sink=True)
    final = asic_manifest.enrich(pre, rtl, source, log)
    assert final["summary"]["unmatched_or_ambiguous"] == 1
    assert final["summary"]["unmatched_pe_instances"] == [f"{pre['top']}/sink/pid=0"]
    assert final["summary"]["unjoined_post_hls_records"] == 1


def test_inlined_function_joins_through_an_inner_loop_label(tmp_path):
    pre = _single_pe_pre("gemm_2_3_fixed", "top/gemm/pid=2,3")
    source = tmp_path / "kernel.cpp"
    log = tmp_path / "solution1.log"
    rtl = tmp_path / "rtl"
    _write(
        source,
        """void gemm_2_3_fixed() {
  outer: for (;;) {
    inner7: for (;;) {}
  }
}
""",
    )
    process = "MXU_0_1_Pipeline_inner7"
    legalized = "MXU_0_1_Pipeline_inner7"
    module = f"top_{legalized}"
    _write(
        log,
        "INFO: Inlining function 'gemm_2_3_fixed' into 'MXU_0'\n"
        f"INFO: -- Generating RTL for module '{process}'\n",
    )
    _write(rtl / "pe.v", f"module {module}(); endmodule\n")
    _write(rtl / "parent.v", f"module parent(); {module} pe_U0(); endmodule\n")

    final = asic_manifest.enrich(pre, rtl, source, log)
    record = final["post_hls_records"][0]
    assert record["semantic_id"] == "top/gemm/pid=2,3"
    assert record["mapping_mode"] == "inlined_pipeline"
    assert record["vitis_process"] == process
    assert record["rtl_root_module"] == "parent"
    assert final["rtl_hierarchy"]["module_instances"][0]["module"] == module

    _write(rtl / "parent2.v", f"module parent2(); {module} pe_U1(); endmodule\n")
    ambiguous = asic_manifest.enrich(pre, rtl, source, log)
    assert ambiguous["post_hls_records"][0]["status"] == "ambiguous_rtl_root"
    assert ambiguous["summary"]["unmatched_or_ambiguous"] == 1


def test_direct_process_takes_precedence_over_its_multiple_pipelines(tmp_path):
    """Internal pipeline modules are dependencies, not competing PE roots."""
    pre = _single_pe_pre("loader_0", "top/loader/pid=0")
    source = tmp_path / "kernel.cpp"
    log = tmp_path / "solution1.log"
    rtl = tmp_path / "rtl"
    _write(
        source,
        """void loader_0() {
  load_row_0: for (int i=0; i<2; ++i) {}
  load_row_1: for (int i=0; i<2; ++i) {}
}
""",
    )
    process = "loader_0.1"
    legalized = "loader_0_1"
    root = f"top_{legalized}"
    pipeline_0 = f"{root}_Pipeline_load_row_0"
    pipeline_1 = f"{root}_Pipeline_load_row_1"
    _write(
        log,
        """INFO: detected/extracted 1 process function(s):
 'loader_0.1'.
INFO: next phase
WARNING: Legalizing function name 'loader_0.1' to 'loader_0_1'.
INFO: -- Generating RTL for module 'loader_0_1_Pipeline_load_row_0'
INFO: -- Generating RTL for module 'loader_0_1_Pipeline_load_row_1'
INFO: -- Generating RTL for module 'loader_0_1'
""",
    )
    _write(
        rtl / "loader.v",
        f"module {root}(); {pipeline_0} row0(); {pipeline_1} row1(); endmodule\n",
    )
    _write(rtl / "row0.v", f"module {pipeline_0}(); endmodule\n")
    _write(rtl / "row1.v", f"module {pipeline_1}(); endmodule\n")
    _write(rtl / "top.v", f"module top(); {root} loader_U0(); endmodule\n")

    final = asic_manifest.enrich(pre, rtl, source, log)
    record = final["post_hls_records"][0]
    assert record["status"] == "matched"
    assert record["mapping_mode"] == "direct_or_cloned"
    assert record["vitis_process"] == process
    assert record["legalized_process"] == legalized
    assert record["rtl_root_module"] == root
    assert record["rtl_dependency_modules"] == [root, pipeline_0, pipeline_1]
    assert final["summary"]["unmatched_or_ambiguous"] == 0


def test_clone_suffix_uses_compiler_identity_and_ambiguous_clones_fail_closed(
    tmp_path,
):
    pre = _single_pe_pre()
    source = tmp_path / "kernel.cpp"
    log = tmp_path / "solution1.log"
    rtl = tmp_path / "rtl"
    _write(source, "void pe() { for (int i=0; i<4; ++i) {} }\n")
    _write(
        log,
        "WARNING: Legalizing function name 'pe.1' to 'pe_1'.\n"
        "INFO: -- Generating RTL for module 'pe_1'\n",
    )
    _write(rtl / "pe.v", "module top_pe_1(); endmodule\n")
    final = asic_manifest.enrich(pre, rtl, source, log)
    assert final["post_hls_records"][0]["legalized_process"] == "pe_1"
    assert final["post_hls_records"][0]["semantic_id"] == "top/pe/pid=0"

    _write(
        log,
        """INFO: detected/extracted 2 process function(s):
 'pe'
 'pe.1'.
INFO: next phase
""",
    )
    ambiguous = asic_manifest.enrich(pre, rtl, source, log)
    assert ambiguous["post_hls_records"][0]["status"] == "ambiguous_hls_process"
    assert ambiguous["summary"]["unmatched_or_ambiguous"] == 1
    assert ambiguous["macro_groups"] == []


def test_empty_hls_is_allowed_but_ambiguous_rtl_fails_closed(tmp_path):
    pre = _single_pe_pre()
    source = tmp_path / "kernel.cpp"
    log = tmp_path / "solution1.log"
    rtl = tmp_path / "rtl"
    rtl.mkdir()
    _write(source, "void pe() {}\n")
    _write(log, "INFO: synthesis completed\n")
    empty = asic_manifest.enrich(pre, rtl, source, log)
    assert empty["post_hls_records"][0]["status"] == "empty_source"
    assert empty["summary"]["unmatched_or_ambiguous"] == 0

    _write(source, "void pe() { int value = 1; }\n")
    _write(log, "INFO: -- Generating RTL for module 'pe'\n")
    _write(rtl / "one.v", "module top_pe(); endmodule\n")
    _write(rtl / "two.v", "module wrapper_pe(); endmodule\n")
    ambiguous = asic_manifest.enrich(pre, rtl, source, log)
    assert ambiguous["post_hls_records"][0]["status"] == "ambiguous_rtl_module"
    assert ambiguous["summary"]["unmatched_or_ambiguous"] == 1
    assert ambiguous["macro_groups"] == []


def test_equivalence_uses_hls_contract_and_rtl_is_diagnostic():
    base = {
        "status": "matched",
        "pre_hls_implementation_contract_hash": "pre",
        "hls_equivalence_hash": "hls",
        "hls_interface_signature": {"ports": []},
        "mapping_mode": "direct_or_cloned",
        "synthesis_contract": {"clock_period_ns": 5},
        "rtl_modules": [{"name": "pe", "file": "pe.v"}],
    }
    records = [
        dict(base, semantic_id="pe/0", rtl_equivalence_hash="rtl-a"),
        dict(base, semantic_id="pe/1", rtl_equivalence_hash="rtl-b"),
    ]
    groups, audits = asic_manifest.build_equivalence_groups(records)
    assert len(groups) == 1
    assert groups[0]["rtl_audit"]["status"] == "generated_rtl_diverged"
    assert groups[0]["rtl_audit"]["authority"] is False
    assert audits[0]["status"] == "diagnostic_only_representative_will_be_used"

    records[1]["synthesis_contract"] = {"clock_period_ns": 10}
    assert len(asic_manifest.build_equivalence_groups(records)[0]) == 2


def test_hls_canonicalization_keeps_behavior_and_interface():
    first = asic_manifest.parse_cpp_functions(
        """void loader_0(hls::stream<int> &west, hls::stream<int> &east) {
  loop17: for (int i17=0; i17<4; ++i17) { east.write(west.read()); }
}
"""
    )[0]
    renamed = asic_manifest.parse_cpp_functions(
        """void loader_1(hls::stream<int> &north, hls::stream<int> &south) {
  loop93: for (int j=0; j<4; ++j) { south.write(north.read()); }
}
"""
    )[0]
    nonblocking = asic_manifest.parse_cpp_functions(
        "void pe(hls::stream<int> &x) { int v; bool ok = x.read_nb(v); }"
    )[0]
    different_type = asic_manifest.parse_cpp_functions(
        "void pe(hls::stream<short> &x) { short v = x.read(); }"
    )[0]
    digest, interface = asic_manifest.canonical_hls_fingerprint(first)
    assert digest == asic_manifest.canonical_hls_fingerprint(renamed)[0]
    assert digest != asic_manifest.canonical_hls_fingerprint(nonblocking)[0]
    assert digest != asic_manifest.canonical_hls_fingerprint(different_type)[0]
    assert interface["ports"] == [
        {"type": "hls::stream<int> &", "direction": "in"},
        {"type": "hls::stream<int> &", "direction": "out"},
    ]

    multidimensional = asic_manifest.parse_cpp_functions(
        "void drv(ac_ieee_float<binary16> data[2][32]) { data[0][0] = 1; }"
    )[0]
    assert multidimensional.parameters == (("data", "ac_ieee_float<binary16> [2][32]"),)


def test_rtl_canonicalization_ignores_spelling_but_keeps_behavior():
    first = """
module pe_a(input [7:0] west, output [7:0] east);
wire [7:0] value_a;
assign value_a = west + 8'd1;
assign east = value_a;
endmodule
"""
    renamed = """
module pe_b(input [7:0] north, output [7:0] south);
wire [7:0] temporary;
assign south = temporary;
assign temporary = north + 8'd1;
endmodule
"""
    first_hash = asic_manifest.canonical_rtl_fingerprint(first)
    assert first_hash == asic_manifest.canonical_rtl_fingerprint(renamed)
    assert first_hash != asic_manifest.canonical_rtl_fingerprint(
        renamed.replace("8'd1", "8'd2")
    )
    assert first_hash != asic_manifest.canonical_rtl_fingerprint(
        first.replace("[7:0]", "[8:0]", 1)
    )
    child_a = "module p(input x, output y); child_a u0(x, y); endmodule"
    child_b = "module p(input x, output y); child_b u0(x, y); endmodule"
    assert asic_manifest.canonical_rtl_fingerprint(
        child_a
    ) != asic_manifest.canonical_rtl_fingerprint(child_b)
    serialized = asic_manifest.canonical_rtl_serialization(
        "module p(a,z,io); input a; output z; inout io; assign z=a; endmodule"
    )
    assert all(f"{bucket}:" in serialized for bucket in ("inputs", "outputs", "inouts"))

    direct = """
module a(input [7:0] x, input [7:0] y, output [7:0] z, output [7:0] q);
assign z = x + 8'd1;
assign q = y ^ 8'h5a;
endmodule
"""
    aliased = """
module b(input [7:0] north, input [7:0] west, output [7:0] south, output [7:0] east);
wire [7:0] temporary;
assign temporary = north;
assign east = west ^ 8'h5a;
assign south = temporary + 8'd1;
endmodule
"""
    assert asic_manifest.canonical_rtl_fingerprint(
        direct
    ) == asic_manifest.canonical_rtl_fingerprint(aliased)


def test_vitis_synthesis_contract_and_dependency_free_tcl(tmp_path):
    contract = asic_manifest.parse_vitis_synthesis_contract(
        "Vitis HLS - High-Level Synthesis v2022.1 (64-bit)\n"
        "INFO: Running: set_part {xcu280-test}\n"
        "INFO: Running: create_clock -period 8.00\n"
    )
    assert contract["tool_version"] == "2022.1"
    assert contract["target_part"] == "xcu280-test"
    assert contract["clock_period_ns"] == 8.0

    manifest = {
        "schema_version": 2,
        "stage": "pre_hls",
        "summary": {"matched": 2, "safe": True},
        "members": ["pe/call=0", "pe/call=$1"],
    }
    text = asic_manifest.to_tcl(manifest)
    assert "package require" not in text
    assert "set allo_asic_manifest [dict create" in text
    if shutil.which("tclsh"):
        check = tmp_path / "check.tcl"
        check.write_text(
            text
            + "\nputs [dict get $allo_asic_manifest summary matched]\n"
            + "puts [lindex [dict get $allo_asic_manifest members] 1]\n"
        )
        result = subprocess.run(
            ["tclsh", str(check)], check=True, capture_output=True, text=True
        )
        assert result.stdout.splitlines() == ["2", "pe/call=$1"]


def test_post_hls_debug_artifacts_are_exact_hash_inputs(tmp_path):
    pre = _single_pe_pre()
    source = tmp_path / "kernel.cpp"
    log = tmp_path / "solution1.log"
    rtl = tmp_path / "rtl"
    _write(source, "void pe() { int generated = 1; }\n")
    _write(log, "INFO: -- Generating RTL for module 'pe'\n")
    _write(rtl / "top_pe.v", "module top_pe(); wire generated; endmodule\n")
    final = asic_manifest.enrich(pre, rtl, source, log)
    debug = tmp_path / "debug"
    asic_manifest.write_post_hls_debug(final, debug, source, log, rtl)

    hls_text = (debug / "canonical/hls/pe.txt").read_text().strip()
    rtl_text = (debug / "canonical/rtl/top_pe.txt").read_text().strip()
    function = asic_manifest.parse_cpp_functions(source.read_text())[0]
    assert hashlib.sha256(hls_text.encode()).hexdigest() == (
        asic_manifest.canonical_hls_fingerprint(function)[0]
    )
    assert hashlib.sha256(rtl_text.encode()).hexdigest() == (
        asic_manifest.canonical_rtl_fingerprint((rtl / "top_pe.v").read_text())
    )
    assert (debug / "parsed-vitis-log.json").is_file()


def test_emit_final_uses_the_integrated_vitis_path(tmp_path):
    project = tmp_path / "build"
    solution = project / "top.prj" / "solution1"
    rtl = solution / "syn" / "verilog"
    pre = _single_pe_pre()
    _write(project / "asic-manifest.json", json.dumps(pre))
    _write(project / "kernel.cpp", "void pe() { int value = 1; }\n")
    _write(
        project / "run.tcl",
        "set_part {xcu280-test}\ncreate_clock -period 5.0\n",
    )
    _write(
        solution / "solution1.log",
        "Vitis HLS - High-Level Synthesis v2022.1\n"
        "INFO: -- Generating RTL for module 'pe'\n",
    )
    _write(rtl / "top_pe.v", "module top_pe(); endmodule\n")
    configs = {
        "asic_manifest": {
            "enabled": True,
            "path": "asic-manifest.json",
            "debug_artifacts": True,
            "debug_dir": "asic-debug",
        }
    }

    final = asic_manifest.emit_final(str(project), configs, "vitis")
    assert final["summary"]["matched"] == 1
    assert final["post_hls_records"][0]["synthesis_contract"] == {
        "backend": "vitis_hls",
        "tool_version": "2022.1",
        "target_part": "xcu280-test",
        "clock_period_ns": 5.0,
        "hierarchy_policy": "vitis_dataflow_process",
    }
    assert (project / "asic-manifest-final.json").is_file()
    assert (project / "asic-manifest-final.tcl").is_file()
    assert (project / "asic-debug/canonical/hls/pe.txt").is_file()
    assert (project / "asic-debug/canonical/rtl/top_pe.txt").is_file()


def test_emit_final_uses_the_integrated_systemc_path(tmp_path):
    project = tmp_path / "project"
    pre = _single_pe_pre("pe_0")
    pre.update(
        {
            "backend": "systemc",
            "top_arguments": [
                {
                    "ordinal": 0,
                    "name": "src",
                    "shape": [4],
                    "type": "i16",
                    "direction": "input",
                },
                {
                    "ordinal": 1,
                    "name": "dst",
                    "shape": [4],
                    "type": "i16",
                    "direction": "output",
                },
            ],
        }
    )
    _write(project / "asic-manifest.json", json.dumps(pre))
    rtl = _systemc_fixture(project)

    final = asic_manifest.emit_final(str(project), {
        "asic_manifest": dict(MANIFEST_CONFIG)
    }, "systemc")

    assert final["backend"] == "systemc"
    assert final["summary"]["matched"] == 1
    assert final["summary"]["unjoined_post_hls_records"] == 0
    record = final["post_hls_records"][0]
    assert record["mapping_mode"] == "systemc_module"
    assert record["rtl_root_module"] == "pe_0"
    assert record["rtl_dependency_modules"] == ["pe_0", "pe_0_run"]
    assert record["synthesis_contract"] == {
        "backend": "systemc",
        "frontend": "allo_systemc_connections",
        "tool_version": "2024.3_16.10",
        "clock_period_ns": 8.0,
        "technology_library": "nangate-45nm_beh",
        "hierarchy_policy": "explicit_blocks",
    }

    src, dst = final["top_arguments"]
    assert (src["name"], src["catapult_argument"]) == ("src", "v10")
    assert src["interface_protocol"] == "systemc_sync_memory_read"
    assert src["interface"]["read_latency_cycles"] == 1
    assert src["interface"]["address_capacity"] == 4
    assert (dst["name"], dst["catapult_argument"]) == ("dst", "v11")
    assert dst["interface_protocol"] == "systemc_sync_memory_write"
    assert final["top_interface"]["protocol"] == "systemc_connections"
    assert final["top_interface"]["completion"]["port"] == "done"
    assert final["top_interface"]["reset"]["polarity"] == "active_low"
    assert final["rtl_artifact"]["path"] == str(rtl / "concat_rtl.v")
    assert final["rtl_artifact"]["self_contained"] is True
    assert (project / "asic-manifest-final.json").is_file()
    assert (project / "asic-manifest-final.tcl").is_file()
    assert (project / "asic-debug/systemc-rtl-path.txt").read_text().strip() == (
        str(rtl / "concat_rtl.v")
    )


def test_systemc_rtl_directory_prefers_top_concat(tmp_path):
    project = tmp_path / "project"
    top = project / "build/Catapult/top.v1"
    _write(top / "concat_rtl.v", "module top(); endmodule\n")
    for name in ("load_a", "load_b", "consume_c"):
        _write(
            project / f"build/Catapult/td_ccore_solutions/{name}/rtl.v",
            f"module {name}(); endmodule\n",
        )

    assert asic_manifest.rtl_directory("systemc", project) == str(top)


def test_split_instance_prefers_the_regions_own_kernel_names():
    """A kernel whose name ends in a number is only safe with the mapping.

    `stage_2` replicated over a 1-D grid is spelled `stage_2_0`, which is
    indistinguishable from a kernel `stage` at pid `(2, 0)` unless the region
    says which kernels it declared -- so the region's `mappings` is passed in
    and preferred, and the fallback is documented rather than silent.
    """
    mappings = {"stage_2": [4], "pe": [4, 4]}
    assert asic_manifest.split_instance("stage_2_3", mappings) == ("stage_2", (3,))
    assert asic_manifest.split_instance("pe_1_2", mappings) == ("pe", (1, 2))
    assert asic_manifest.split_instance("stage_2_3") == ("stage", (2, 3))


if __name__ == "__main__":
    pytest.main([__file__])
