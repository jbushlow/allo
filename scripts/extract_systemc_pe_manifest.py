#!/usr/bin/env python3
"""Enrich an Allo ASIC manifest from SystemC/Catapult RTL artifacts."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path


def _load(name: str):
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(f"_allo_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SHARED = _load("extract_vitis_pe_manifest.py")
CATAPULT = _load("extract_catapult_pe_manifest.py")


def _match_brace(text: str, opening: int) -> int:
    return SHARED._match_delimiter(text, opening, "{", "}")


def _synthesis_view(text: str) -> str:
    """Select __SYNTHESIS__ branches before structural C++ parsing."""
    output = []
    stack = []
    directive = re.compile(r"^\s*#\s*(ifdef|ifndef|if|else|endif)\b(.*)$")
    for line in text.splitlines(keepends=True):
        match = directive.match(line)
        if match:
            kind, argument = match.group(1), match.group(2).strip()
            if kind in {"ifdef", "ifndef", "if"}:
                mentions_synthesis = "__SYNTHESIS__" in argument
                selected = True
                if mentions_synthesis:
                    selected = kind != "ifndef" and not (
                        kind == "if" and argument.startswith("!")
                    )
                stack.append([mentions_synthesis, selected])
                if mentions_synthesis:
                    continue
            elif kind == "else" and stack:
                if stack[-1][0]:
                    stack[-1][1] = not stack[-1][1]
                    continue
            elif kind == "endif" and stack:
                resolved, _ = stack.pop()
                if resolved:
                    continue
        if all(selected for resolved, selected in stack if resolved):
            output.append(line)
    return "".join(output)


def parse_systemc_modules(text: str) -> dict[str, SHARED.CFunction]:
    """Return one fingerprintable pseudo-function per generated SC_MODULE."""
    text = _synthesis_view(text)
    result = {}
    for match in re.finditer(r"(?m)^\s*SC_MODULE\s*\(\s*([A-Za-z_]\w*)\s*\)\s*\{", text):
        name = match.group(1)
        opening = text.find("{", match.start())
        closing = _match_brace(text, opening)
        body = text[opening + 1 : closing]
        functions = {function.name: function for function in SHARED.parse_cpp_functions(body)}
        run = functions.get("run")
        if run is None:
            continue
        ports = []
        port_re = re.compile(
            r"(?m)^\s*(Connections::(?:In|Out)|sc_(?:in|out)(?:_clk)?)"
            r"\s*(?:<\s*(.*?)\s*>)?\s+([A-Za-z_]\w*)\s*;"
        )
        for port in port_re.finditer(body):
            ports.append((port.group(3), f"{port.group(1)}<{port.group(2) or ''}>"))
        result[name] = SHARED.CFunction(
            name=name,
            parameters=tuple(ports),
            body=run.body,
            loop_labels=run.loop_labels,
        )
    return result


def find_concat_rtl(project: Path, top: str) -> Path:
    candidates = list(project.glob(f"build/**/{top}.v*/concat_rtl.v"))
    candidates += list(project.glob("build/**/concat_rtl.v"))
    candidates += list(project.glob(f"build/**/{top}.v*/rtl.v"))
    candidates += list(project.glob("build/**/rtl.v"))
    candidates = sorted({path.resolve() for path in candidates if path.is_file()})
    if not candidates:
        raise FileNotFoundError(f"no SystemC/Catapult RTL under {project / 'build'}")
    concat = [path for path in candidates if path.name == "concat_rtl.v"]
    return max(concat or candidates, key=lambda path: path.stat().st_mtime)


def _synthesis_contract(project: Path) -> dict:
    contract = CATAPULT._catapult_synthesis_contract(project)
    logs = sorted(project.glob("build/**/catapult.log"))
    if logs:
        match = re.search(
            r"Catapult Ultra Synthesis\s+([^\s]+)",
            logs[-1].read_text(encoding="utf-8", errors="replace"),
        )
        if match:
            contract["tool_version"] = match.group(1)
    contract["backend"] = "systemc"
    contract["frontend"] = "allo_systemc_connections"
    return contract


def build_manifest(kernel_cpp: Path, pre_manifest: dict, rtl_path: Path, top: str):
    source = kernel_cpp.read_text(encoding="utf-8", errors="replace")
    functions = parse_systemc_modules(source)
    rtl_text_all = rtl_path.read_text(encoding="utf-8", errors="replace")
    modules = CATAPULT._parse_modules(rtl_text_all)
    hierarchy = SHARED.extract_rtl_hierarchy({str(rtl_path): rtl_text_all}, top=top)
    contract = _synthesis_contract(kernel_cpp.parent)
    records = []
    for pe in pre_manifest.get("pe_instances", []):
        name = pe["specialized_function"]
        function = functions.get(name)
        status = "matched" if name in modules else "unmatched_rtl_module"
        if function is None:
            status = "unmatched_systemc_module"
        closure = SHARED.rtl_dependency_closure(name, hierarchy) if status == "matched" else []
        hls_hash, interface = (
            SHARED.canonical_hls_fingerprint(function) if function else (None, None)
        )
        rtl_text = "\n".join(modules[module] for module in closure)
        records.append(
            {
                "semantic_id": pe["semantic_id"],
                "kernel": pe["kernel"],
                "pid": pe.get("pid"),
                "hls_function": name,
                "mapping_mode": "systemc_module",
                "synthesis_contract": contract,
                "loop_labels": list(function.loop_labels) if function else [],
                "hls_equivalence_hash": hls_hash,
                "hls_interface": interface,
                "rtl_modules": [{"name": module, "file": str(rtl_path)} for module in closure],
                "rtl_instances": [],
                "rtl_root_module": name if status == "matched" else None,
                "rtl_root_instances": [
                    item for item in hierarchy["realized_instances"] if item["module"] == name
                ],
                "rtl_equivalence_hash": (
                    SHARED.canonical_verilog_fingerprint(rtl_text, backend="catapult")
                    if rtl_text else None
                ),
                "status": status,
            }
        )
    macro_groups, candidates = SHARED.build_equivalence_groups(records)
    counts = {name: sum(r["status"] == name for r in records) for name in {r["status"] for r in records}}
    post = {
        "schema_version": 2,
        "stage": "post_hls",
        "backend": "systemc",
        "inputs": {
            "kernel_cpp": str(kernel_cpp),
            "rtl_artifact": str(rtl_path),
            "rtl_artifact_kind": "systemc_catapult_rtl",
        },
        "records": records,
        "macro_groups": macro_groups,
        "candidate_groups": candidates,
        "summary": {
            **counts,
            "total": len(records),
            "unmatched_or_ambiguous": sum(r["status"] != "matched" for r in records),
        },
    }
    merged = SHARED.merge_pre_hls_manifest(pre_manifest, post)
    merged["backend"] = "systemc"
    merged["rtl_hierarchy"] = hierarchy
    merged["rtl_artifact"] = {
        "path": str(rtl_path),
        "project_relative_path": str(rtl_path.relative_to(kernel_cpp.parent)),
        "published_path": "backend-rtl/concat_rtl.v",
        "kind": "systemc_catapult_rtl",
        "self_contained": True,
    }
    merged["top_interface"] = {
        "protocol": "systemc_connections",
        "clock": {"name": "clk", "edge": "rising", "period_ns": contract["clock_period_ns"]},
        "reset": {
            "name": "rst",
            "polarity": "active_low",
            "default_asserted_cycles": 2,
            "evidence": "systemc_reset_signal_is_false",
        },
        "launch": "connections_handshake_after_reset_deassertion",
        "completion": {"kind": "top_done", "active_level": 1},
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
    rtl = find_concat_rtl(args.project_dir.resolve(), args.top)
    pre = json.loads(args.pre_manifest.read_text(encoding="utf-8"))
    manifest = build_manifest(args.kernel_cpp.resolve(), pre, rtl, args.top)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    args.output.with_suffix(".tcl").write_text(SHARED.render_tcl_manifest(manifest), encoding="utf-8")
    if args.debug_dir:
        args.debug_dir.mkdir(parents=True, exist_ok=True)
        (args.debug_dir / "systemc-rtl-path.txt").write_text(str(rtl) + "\n", encoding="utf-8")
    return 1 if manifest["summary"]["unmatched_or_ambiguous"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
