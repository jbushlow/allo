#!/usr/bin/env python3
"""Enrich an Allo ASIC manifest from Catapult's self-contained concat RTL."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import re
import sys
from pathlib import Path


def _load_shared_extractor():
    path = Path(__file__).with_name("extract_vitis_pe_manifest.py")
    spec = importlib.util.spec_from_file_location("_allo_vitis_manifest", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SHARED = _load_shared_extractor()


def _find_concat_rtl(project: Path, top: str) -> Path:
    candidates = list(project.glob(f"Catapult*/{top}.v*/concat_rtl.v"))
    candidates += list(project.glob(f"{top}.v*/concat_rtl.v"))
    if not candidates:
        raise FileNotFoundError(f"no Catapult concat_rtl.v under {project}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _parse_modules(text: str) -> dict[str, str]:
    modules = {}
    pattern = re.compile(r"(?ms)^\s*module\s+([A-Za-z_][\w$]*)\b.*?\bendmodule\b")
    for match in pattern.finditer(text):
        modules[match.group(1)] = match.group(0)
    return modules


def _module_closure(root: str, modules: dict[str, str]) -> list[str]:
    known = sorted(modules, key=len, reverse=True)
    result, pending = [], [root]
    while pending:
        name = pending.pop()
        if name in result or name not in modules:
            continue
        result.append(name)
        body = modules[name]
        for child in known:
            if child != name and re.search(
                rf"\b{re.escape(child)}\s*(?:#\s*\([^;]*?\)\s*)?"
                rf"[A-Za-z_][\w$]*\s*\(",
                body,
                re.S,
            ):
                pending.append(child)
    return result


def _width(declaration: str) -> int:
    match = re.search(r"\[(\d+)\s*:\s*(\d+)\]", declaration)
    return abs(int(match.group(1)) - int(match.group(2))) + 1 if match else 1


def _type_width(type_name: str) -> int | None:
    match = re.search(r"(\d+)$", type_name or "")
    return int(match.group(1)) if match else None


MEMORY_ROLE_SUFFIXES = {
    "read_address": "_rsc_radr",
    "read_enable": "_rsc_re",
    "read_data": "_rsc_q",
    "write_address": "_rsc_wadr",
    "write_enable": "_rsc_we",
    "write_data": "_rsc_d",
}

# Catapult decorates floating-point array resources with ``_d`` while integer
# array resources normally retain the generated C argument name unchanged.
CATAPULT_RESOURCE_SUFFIXES = ("", "_d")


def _validated_resource_root(generated_name: str, port_name: str, suffix: str) -> str:
    root = port_name[: -len(suffix)]
    allowed = {
        f"{generated_name}{resource_suffix}"
        for resource_suffix in CATAPULT_RESOURCE_SUFFIXES
    }
    if root not in allowed:
        raise RuntimeError(
            f"Catapult argument {generated_name!r} has unsupported resource "
            f"root {root!r} in port {port_name!r}; expected one of "
            f"{sorted(allowed)}"
        )
    return root


def _match_argument_protocol(generated_name: str, ports: dict, semantic: dict) -> dict:
    """Match one realized Catapult argument against exact known port schemas."""
    non_triosy = {name: info for name, info in ports.items() if "triosy" not in name}
    triosy = [
        {"name": name, **info} for name, info in ports.items() if "triosy" in name
    ]
    suffix_roles = {"_rsc_dat": "data"}
    suffix_roles.update(
        {suffix: role for role, suffix in MEMORY_ROLE_SUFFIXES.items()}
    )
    suffix_roles = sorted(
        suffix_roles.items(), key=lambda item: len(item[0]), reverse=True
    )
    role_ports = {}
    resource_roots = set()
    for port_name in non_triosy:
        for suffix, role in suffix_roles:
            if port_name.endswith(suffix):
                root = _validated_resource_root(generated_name, port_name, suffix)
                if role in role_ports:
                    raise RuntimeError(
                        f"Catapult argument {semantic.get('name', generated_name)!r} "
                        f"has multiple ports for role {role!r}"
                    )
                role_ports[role] = port_name
                resource_roots.add(root)
                break
        else:
            raise RuntimeError(
                f"Catapult argument {semantic.get('name', generated_name)!r} has "
                f"unrecognized protocol port {port_name!r}"
            )

    if len(resource_roots) != 1:
        raise RuntimeError(
            f"Catapult argument {semantic.get('name', generated_name)!r} uses "
            f"inconsistent resource roots {sorted(resource_roots)}"
        )
    resource_root = next(iter(resource_roots))
    observed_roles = set(role_ports)
    read_roles = {"read_address", "read_enable", "read_data"}
    write_roles = {"write_address", "write_enable", "write_data"}

    matches = []
    if observed_roles == {"data"}:
        matches.append(("catapult_direct_array", role_ports))
    if observed_roles == read_roles:
        matches.append(("catapult_sync_memory_read", role_ports))
    if observed_roles == write_roles:
        matches.append(("catapult_sync_memory_write", role_ports))
    if observed_roles == read_roles | write_roles:
        matches.append(("catapult_sync_memory_readwrite", role_ports))
    if len(matches) != 1:
        expected = [
            sorted({"data"}), sorted(read_roles), sorted(write_roles),
            sorted(read_roles | write_roles),
        ]
        raise RuntimeError(
            f"Catapult argument {semantic.get('name', generated_name)!r} ports "
            f"{sorted(non_triosy)} match {len(matches)} known schemas; expected "
            f"role set exactly one of {expected}"
        )

    expected_triosy = f"{resource_root}_triosy_lz"
    unexpected_triosy = sorted(
        port["name"] for port in triosy if port["name"] != expected_triosy
    )
    if unexpected_triosy:
        raise RuntimeError(
            f"Catapult argument {semantic.get('name', generated_name)!r} has "
            f"triosy ports {unexpected_triosy} inconsistent with resource root "
            f"{resource_root!r}"
        )
    protocol, roles = matches[0]
    return {
        "protocol": protocol,
        "resource_root": resource_root,
        "roles": {
            role: {"name": name, **non_triosy[name]}
            for role, name in roles.items()
        },
        "triosy_ports": triosy,
        "matching": "exact_resource_root_port_family",
    }


def _top_interface(top_fn, top_rtl: str, semantic_args: list[dict]) -> list[dict]:
    declarations = {}
    declaration_re = re.compile(
        r"\b(input|output|inout)\s*(?:wire\s+|reg\s+)?"
        r"(?:\[[^]]+\]\s*)?([A-Za-z_]\w*)\s*;"
    )
    for match in declaration_re.finditer(top_rtl):
        declaration = match.group(0)
        declarations[match.group(2)] = {
            "direction": match.group(1),
            "width": _width(declaration),
        }
    result = []
    for ordinal, (generated_name, c_type) in enumerate(top_fn.parameters):
        semantic = semantic_args[ordinal] if ordinal < len(semantic_args) else {}
        ports = {
            name: info
            for name, info in declarations.items()
            if name == generated_name or name.startswith(generated_name + "_")
        }
        matched_interface = _match_argument_protocol(generated_name, ports, semantic)
        data_ports = [
            {"name": name, **info}
            for name, info in ports.items()
            if "triosy" not in name
        ]
        triosy_ports = [
            {"name": name, **info}
            for name, info in ports.items()
            if "triosy" in name
        ]
        rtl_direction = semantic.get("direction", "unknown")
        shape = semantic.get("shape", [])
        element_bits = _type_width(semantic.get("type", ""))
        element_count = math.prod(shape) if shape else 1
        data_width = sum(port["width"] for port in data_ports)
        packing = {
            "layout": "row_major",
            "element_order": "element_zero_at_lsb",
            "element_bits": element_bits,
            "element_count": element_count,
            "packed_width": data_width,
            "width_matches_shape": (
                element_bits is not None
                and data_width == element_bits * element_count
            ),
            "evidence": "catapult_scverify_array_transactor",
        }
        protocol = matched_interface["protocol"]
        if protocol != "catapult_direct_array":
            address_ports = [
                port for role, port in matched_interface["roles"].items()
                if role.endswith("address")
            ]
            data_role = "read_data" if "read_data" in matched_interface["roles"] else "write_data"
            data_width = matched_interface["roles"][data_role]["width"]
            address_width = max(port["width"] for port in address_ports)
            matched_interface.update(
                {
                    "element_bits": element_bits,
                    "element_count": element_count,
                    "address_width": address_width,
                    "address_capacity": 1 << address_width,
                    "data_width": data_width,
                    "read_latency_cycles": 1 if "read_data" in matched_interface["roles"] else None,
                    "layout": "row_major",
                }
            )
        result.append(
            {
                **semantic,
                "ordinal": ordinal,
                "name": semantic.get("name", f"arg{ordinal}"),
                "catapult_argument": generated_name,
                "catapult_c_type": c_type,
                "semantic_direction": semantic.get("direction", "unknown"),
                "rtl_direction": rtl_direction,
                "interface_protocol": protocol,
                "interface": matched_interface,
                "data_ports": data_ports,
                "triosy_ports": triosy_ports,
                "rtl_ports": [{"name": name, **info} for name, info in ports.items()],
                "packing": packing if protocol == "catapult_direct_array" else None,
            }
        )
    return result


def _clock_period(project: Path) -> float | None:
    run_tcl = project / "run.tcl"
    if not run_tcl.is_file():
        return None
    match = re.search(
        r"-CLOCK_PERIOD\s+([0-9]+(?:\.[0-9]+)?)",
        run_tcl.read_text(encoding="utf-8", errors="replace"),
    )
    return float(match.group(1)) if match else None


def _catapult_synthesis_contract(project: Path) -> dict:
    run_tcl = project / "run.tcl"
    text = run_tcl.read_text(encoding="utf-8", errors="replace") if run_tcl.is_file() else ""
    library = re.search(r"solution\s+library\s+add\s+(\S+)", text)
    log = project / "catapult.log"
    version = None
    if log.is_file():
        match = re.search(
            r"Catapult Ultra Synthesis\s+([^\s]+)",
            log.read_text(encoding="utf-8", errors="replace"),
        )
        version = match.group(1) if match else None
    return {
        "backend": "catapult",
        "tool_version": version or "unknown",
        "clock_period_ns": _clock_period(project),
        "technology_library": library.group(1) if library else "unknown",
        "hierarchy_policy": "explicit_blocks",
    }


def build_manifest(kernel_cpp: Path, pre_manifest: dict, rtl_path: Path, top: str):
    source = kernel_cpp.read_text(encoding="utf-8", errors="replace")
    functions = {fn.name: fn for fn in SHARED.parse_cpp_functions(source)}
    rtl_text_all = rtl_path.read_text(encoding="utf-8", errors="replace")
    modules = _parse_modules(rtl_text_all)
    hierarchy = SHARED.extract_rtl_hierarchy({str(rtl_path): rtl_text_all}, top=top)
    synthesis_contract = _catapult_synthesis_contract(kernel_cpp.parent)
    records = []
    for pe in pre_manifest.get("pe_instances", []):
        base = pe["specialized_function"]
        hls_name = base if base in functions else base + "_fixed"
        fn = functions.get(hls_name)
        status = "matched" if hls_name in modules else "unmatched_rtl_module"
        substantive_body = (
            re.sub(r"//[^\n]*|/\*.*?\*/", "", fn.body, flags=re.S).strip() if fn else ""
        )
        if fn is not None and not substantive_body:
            status = "empty_source"
        elif hls_name.startswith("wrapper_"):
            status = "orchestration_only"
        closure = (
            SHARED.rtl_dependency_closure(hls_name, hierarchy)
            if status == "matched"
            else []
        )
        hls_hash, interface = (
            SHARED.canonical_hls_fingerprint(fn) if fn else (None, None)
        )
        rtl_text = "\n".join(modules[name] for name in closure)
        record = {
            "semantic_id": pe["semantic_id"],
            "kernel": pe["kernel"],
            "pid": pe.get("pid"),
            "hls_function": hls_name,
            "mapping_mode": "catapult_block",
            "synthesis_contract": synthesis_contract,
            "loop_labels": list(fn.loop_labels) if fn else [],
            "hls_equivalence_hash": hls_hash,
            "hls_interface": interface,
            "rtl_modules": [{"name": name, "file": str(rtl_path)} for name in closure],
            "rtl_instances": [],
            "rtl_root_module": hls_name if status == "matched" else None,
            "rtl_root_instances": [
                item
                for item in hierarchy["realized_instances"]
                if item["module"] == hls_name
            ],
            "rtl_equivalence_hash": (
                SHARED.canonical_verilog_fingerprint(rtl_text, backend="catapult")
                if rtl_text
                else None
            ),
            "status": status,
        }
        records.append(record)
    macro_groups, candidates = SHARED.build_equivalence_groups(records)
    counts = {
        name: sum(r["status"] == name for r in records)
        for name in {r["status"] for r in records}
    }
    post = {
        "schema_version": 2,
        "stage": "post_hls",
        "backend": "catapult",
        "inputs": {
            "kernel_cpp": str(kernel_cpp),
            "rtl_artifact": str(rtl_path),
            "rtl_artifact_kind": "catapult_concat_rtl",
        },
        "records": records,
        "macro_groups": macro_groups,
        "candidate_groups": candidates,
        "summary": {
            **counts,
            "total": len(records),
            "unmatched_or_ambiguous": sum(
                r["status"] not in {"matched", "empty_source", "orchestration_only"}
                for r in records
            ),
        },
    }
    merged = SHARED.merge_pre_hls_manifest(pre_manifest, post)
    merged["backend"] = "catapult"
    merged["rtl_hierarchy"] = hierarchy
    merged["rtl_artifact"] = {
        "path": str(rtl_path),
        "project_relative_path": str(rtl_path.relative_to(kernel_cpp.parent)),
        "published_path": "backend-rtl/concat_rtl.v",
        "kind": "catapult_concat_rtl",
        "self_contained": True,
    }
    top_fn = functions.get(top)
    if top_fn and top in modules:
        merged["top_arguments"] = _top_interface(
            top_fn, modules[top], pre_manifest.get("top_arguments", [])
        )
        merged["top_interface"] = {
            "protocol": "catapult_argument_protocols",
            "argument_protocols": sorted(
                {argument["interface_protocol"] for argument in merged["top_arguments"]}
            ),
            "clock": {
                "name": "clk",
                "edge": "rising",
                "period_ns": _clock_period(kernel_cpp.parent),
            },
            "reset": {
                "name": "rst",
                "polarity": "active_high",
                "default_asserted_cycles": 2,
                "evidence": "catapult_scverify_generated_harness",
            },
            "launch": "drive_inputs_before_reset_deassertion",
            "completion": {
                "kind": "per_argument_triosy",
                "active_level": 1,
                "outputs_complete_when": "all_expected_output_triosy_observed",
            },
            "cycle_trace_available": False,
        }
    return merged


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel-cpp", required=True, type=Path)
    parser.add_argument("--pre-manifest", required=True, type=Path)
    parser.add_argument("--project-dir", required=True, type=Path)
    parser.add_argument("--top", required=True)
    parser.add_argument("--output", "-o", required=True, type=Path)
    parser.add_argument("--debug-dir", type=Path)
    args = parser.parse_args(argv)
    rtl = _find_concat_rtl(args.project_dir.resolve(), args.top)
    pre = json.loads(args.pre_manifest.read_text(encoding="utf-8"))
    manifest = build_manifest(args.kernel_cpp.resolve(), pre, rtl.resolve(), args.top)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    args.output.with_suffix(".tcl").write_text(
        SHARED.render_tcl_manifest(manifest), encoding="utf-8"
    )
    if args.debug_dir:
        args.debug_dir.mkdir(parents=True, exist_ok=True)
        (args.debug_dir / "catapult-rtl-path.txt").write_text(
            str(rtl) + "\n", encoding="utf-8"
        )
    return 1 if manifest["summary"]["unmatched_or_ambiguous"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
