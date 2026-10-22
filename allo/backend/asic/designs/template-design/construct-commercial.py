"""Template constructor for the hierarchical Allo ASIC flow."""

import os
from pathlib import Path

from mflowgen.components import Graph, Node


# Select exactly one HLS backend by uncommenting one block.
BACKEND = None
BACKEND_OPTIONS = None

# Vitis:
# BACKEND = "vitis"
# BACKEND_OPTIONS = "device=u280"

# SystemC with Catapult synthesis:
# BACKEND = "systemc"
# BACKEND_OPTIONS = (
#     "device=nangate-45nm_beh,"
#     "generalize_pid_specializations=true,"
#     "pid_generalization_policy=identity_only"
# )

# Replace this with a descriptive name for reports.
REPORT_DESIGN_NAME = None

# Change these when targeting a different ADK.
ADK = "freepdk-45nm"
ADK_VIEW = "view-standard"


def _runtime_path(env_var, description, *, executable=False):
    """Resolve a required host dependency from the environment."""
    value = os.environ.get(env_var)
    if not value:
        raise RuntimeError(f"Set {env_var} to the {description}")

    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"{description} does not exist: {path}")
    if executable and not os.access(path, os.X_OK):
        raise PermissionError(f"{description} is not executable: {path}")
    return str(path)


def _validate_template_choices():
    """Require users to replace the template-specific selections."""
    if BACKEND is None or BACKEND_OPTIONS is None:
        raise RuntimeError(
            "Select either the Vitis or SystemC backend at the top of "
            "construct-commercial.py"
        )
    if REPORT_DESIGN_NAME is None:
        raise RuntimeError(
            "Set REPORT_DESIGN_NAME at the top of construct-commercial.py"
        )


def construct():
    """Build the complete hierarchical Allo ASIC graph."""
    _validate_template_choices()

    graph = Graph()
    this_dir = Path(__file__).resolve().parent
    asic_dir = this_dir.parent.parent
    nodes_dir = asic_dir / "nodes"

    allo_python = _runtime_path(
        "ALLO_PYTHON",
        "Allo Python interpreter",
        executable=True,
    )
    allo_setup_script = _runtime_path(
        "ALLO_SETUP_SCRIPT",
        "Allo LLVM setup script",
    )

    graph.sys_path.append(str(asic_dir / "adks"))
    graph.set_adk(ADK)
    adk = graph.get_adk_node()

    flow_utilities = Node(str(nodes_dir / "asic-flow-utilities"))
    allo_build = Node(str(nodes_dir / "allo-asic-compilation"))
    testbench_generation = Node(
        str(nodes_dir / "allo-testbench-generation")
    )
    rtl_sim = Node(str(nodes_dir / "allo-rtl-sim"))
    ffgl_sim = Node(str(nodes_dir / "allo-ffgl-sim"))
    bagl_sim = Node(str(nodes_dir / "allo-bagl-sim"))
    power_analysis = Node(str(nodes_dir / "synopsys-pt-power"))
    macro_activity = Node(
        str(nodes_dir / "allo-macro-activity-extraction")
    )
    rtl_normalize = Node(str(nodes_dir / "sv2v-rtl-allo"))

    macro_plan = Node(str(nodes_dir / "allo-asic-macro-plan"))
    macro_synthesis = Node(
        str(nodes_dir / "commercial-batch-synthesis")
    )
    macro_pnr = Node(str(nodes_dir / "commercial-macro-pnr"))
    macro_physical_verify = Node(
        str(nodes_dir / "commercial-batch-physical-verify")
    )
    macro_signoff = Node(str(nodes_dir / "commercial-batch-signoff"))
    macro_publish = Node(str(nodes_dir / "commercial-macro-publish"))
    macro_power = Node(str(nodes_dir / "commercial-batch-macro-power"))

    assembly_plan = Node(str(nodes_dir / "allo-asic-assembly-plan"))
    rtl_assembly = Node(str(nodes_dir / "allo-asic-rtl-assembly"))
    full_chip_synthesis = Node(
        str(nodes_dir / "commercial-full-chip-synthesis")
    )
    physical_intent = Node(
        str(nodes_dir / "allo-asic-physical-intent")
    )
    full_chip_pnr = Node(str(nodes_dir / "commercial-full-chip-pnr"))
    full_chip_gdsmerge = Node(
        str(nodes_dir / "commercial-full-chip-gdsmerge")
    )
    full_chip_drc = Node(str(nodes_dir / "commercial-full-chip-drc"))
    full_chip_lvs = Node(str(nodes_dir / "commercial-full-chip-lvs"))
    flow_summary = Node(str(nodes_dir / "allo-asic-flow-summary"))
    flow_finalize = Node(str(nodes_dir / "asic-flow-finalize"))

    nodes = [
        flow_utilities,
        allo_build,
        testbench_generation,
        rtl_sim,
        ffgl_sim,
        bagl_sim,
        power_analysis,
        macro_activity,
        rtl_normalize,
        macro_plan,
        macro_synthesis,
        macro_pnr,
        macro_physical_verify,
        macro_signoff,
        macro_publish,
        macro_power,
        assembly_plan,
        rtl_assembly,
        full_chip_synthesis,
        physical_intent,
        full_chip_pnr,
        full_chip_gdsmerge,
        full_chip_drc,
        full_chip_lvs,
        flow_summary,
        flow_finalize,
    ]
    for node in nodes:
        graph.add_node(node)

    utility_consumers = [
        allo_build,
        rtl_sim,
        ffgl_sim,
        bagl_sim,
        macro_synthesis,
        macro_pnr,
        macro_physical_verify,
        macro_signoff,
        macro_power,
        full_chip_lvs,
    ]
    for node in utility_consumers:
        graph.connect_by_name(flow_utilities, node)

    # Compile once, then share the manifest, workload, and normalized RTL.
    graph.connect_by_name(allo_build, testbench_generation)
    graph.connect_by_name(allo_build, rtl_normalize)
    graph.connect_by_name(testbench_generation, rtl_sim)
    graph.connect_by_name(rtl_normalize, rtl_sim)

    # Stage 1: plan, harden, verify, characterize, and publish selected blocks.
    graph.connect_by_name(rtl_sim, macro_plan)
    for artifact in [
        "asic-manifest-final.json",
        "asic-manifest-final.tcl",
        "build-metadata.json",
    ]:
        graph.connect(rtl_normalize.o(artifact), macro_plan.i(artifact))
    graph.connect_by_name(macro_plan, macro_synthesis)
    graph.connect_by_name(macro_synthesis, macro_pnr)
    graph.connect_by_name(macro_pnr, macro_physical_verify)
    graph.connect_by_name(macro_physical_verify, macro_signoff)
    graph.connect_by_name(macro_signoff, macro_publish)
    graph.connect_by_name(rtl_normalize, macro_publish)

    for node in [
        macro_synthesis,
        macro_pnr,
        macro_physical_verify,
        macro_signoff,
    ]:
        graph.connect_by_name(adk, node)

    # Stage 2: substitute the published views and implement the full chip.
    graph.connect_by_name(rtl_normalize, assembly_plan)
    graph.connect_by_name(macro_publish, assembly_plan)
    graph.connect_by_name(rtl_normalize, rtl_assembly)
    graph.connect_by_name(assembly_plan, rtl_assembly)
    graph.connect_by_name(macro_publish, rtl_assembly)
    graph.connect_by_name(rtl_assembly, full_chip_synthesis)
    graph.connect_by_name(macro_publish, full_chip_synthesis)
    graph.connect_by_name(adk, full_chip_synthesis)
    graph.connect_by_name(assembly_plan, physical_intent)
    graph.connect_by_name(rtl_assembly, physical_intent)
    graph.connect_by_name(macro_publish, physical_intent)
    graph.connect_by_name(full_chip_synthesis, physical_intent)
    graph.connect_by_name(rtl_assembly, full_chip_pnr)
    graph.connect_by_name(macro_publish, full_chip_pnr)
    graph.connect_by_name(full_chip_synthesis, full_chip_pnr)
    graph.connect_by_name(physical_intent, full_chip_pnr)
    graph.connect_by_name(adk, full_chip_pnr)

    # Full-chip physical verification.
    graph.connect_by_name(full_chip_pnr, full_chip_gdsmerge)
    graph.connect_by_name(macro_publish, full_chip_gdsmerge)
    graph.connect_by_name(adk, full_chip_gdsmerge)
    graph.connect_by_name(full_chip_gdsmerge, full_chip_drc)
    graph.connect_by_name(adk, full_chip_drc)
    graph.connect_by_name(full_chip_gdsmerge, full_chip_lvs)
    graph.connect_by_name(full_chip_pnr, full_chip_lvs)
    graph.connect_by_name(macro_publish, full_chip_lvs)
    graph.connect_by_name(adk, full_chip_lvs)

    # Workload-driven simulation and power analysis.
    graph.connect_by_name(testbench_generation, ffgl_sim)
    graph.connect_by_name(full_chip_synthesis, ffgl_sim)
    graph.connect_by_name(macro_publish, ffgl_sim)
    graph.connect_by_name(adk, ffgl_sim)

    graph.connect_by_name(testbench_generation, bagl_sim)
    graph.connect_by_name(rtl_assembly, bagl_sim)
    graph.connect_by_name(full_chip_pnr, bagl_sim)
    graph.connect_by_name(macro_publish, bagl_sim)
    graph.connect_by_name(adk, bagl_sim)

    graph.connect_by_name(full_chip_pnr, power_analysis)
    graph.connect_by_name(bagl_sim, power_analysis)
    graph.connect_by_name(macro_publish, power_analysis)
    graph.connect_by_name(adk, power_analysis)

    graph.connect_by_name(bagl_sim, macro_activity)
    graph.connect_by_name(assembly_plan, macro_activity)
    graph.connect_by_name(macro_publish, macro_activity)
    graph.connect_by_name(macro_activity, macro_power)
    graph.connect_by_name(macro_publish, macro_power)
    graph.connect(
        power_analysis.o("power.rpt"),
        macro_power.i("power.rpt"),
    )
    graph.connect_by_name(adk, macro_power)

    # Collect every terminal result before final cleanup.
    graph.connect_by_name(macro_publish, flow_summary)
    for node in [
        allo_build,
        macro_synthesis,
        macro_pnr,
        macro_physical_verify,
        macro_signoff,
        full_chip_synthesis,
        full_chip_pnr,
        full_chip_gdsmerge,
        full_chip_drc,
        full_chip_lvs,
    ]:
        graph.connect_by_name(node, flow_summary)

    graph.connect_by_name(physical_intent, flow_summary)
    graph.connect(
        ffgl_sim.o("simulation-report.json"),
        flow_summary.i("ffgl-simulation-report.json"),
    )
    graph.connect(
        bagl_sim.o("simulation-report.json"),
        flow_summary.i("bagl-simulation-report.json"),
    )
    graph.connect_by_name(macro_activity, flow_summary)
    graph.connect_by_name(macro_power, flow_summary)
    graph.connect_by_name(flow_summary, flow_finalize)

    graph.update_params(
        {
            # Allo compilation
            "construct_path": __file__,
            "allo_design_file": str(this_dir / "allo_design.py"),
            "allo_entrypoint": "build",
            "build_mode": "csyn",
            "backend": BACKEND,
            "backend_options": BACKEND_OPTIONS,
            "python_bin": allo_python,
            "allo_setup_script": allo_setup_script,

            # Design and technology
            "top_module": "top",
            "design_name": "top",
            "report_design_name": REPORT_DESIGN_NAME,
            "adk": ADK,
            "adk_view": ADK_VIEW,

            # Clock targets
            "macro_clock_period": 8.0,
            "clock_period": 10.0,

            # Generated testbench
            "allo_testbench_top_function": "top",

            # Macro-selection policy
            "min_macro_reuse": 2,
            "harden_repeated_hls_submodules": False,
            "fold_fifos_into_macro": True,

            # Floorplanning
            "core_density_target": 0.50,
            "macro_separation_x": 5.0,
            "macro_separation_y": 5.0,
            "kernel_separation_x": 10.0,
            "kernel_separation_y": 10.0,
            "sram_separation": 15.0,
            "macro_halo": 0.5,

            # Technology-dependent macro routing limit
            "highest_macro_routing_layer": 5,

            # Local compute resources
            "enable_gui": False,
            "nthreads": 4,
            "local_cpus": 4,
            "postroute_max_local_cpus": 4,
            "drc_nthreads": 4,
            "lvs_nthreads": 4,
            "macro_power_max_workers": 4,

            # Verification policy
            "antenna_check_policy": "report",
            "drc_check_policy": "error",
            "bagl_failure_policy": "error",

            # Power and output retention
            "activity_source": "bagl_vcd",
            "cleanup_enabled": True,
        }
    )
    return graph


if __name__ == "__main__":
    construct()
