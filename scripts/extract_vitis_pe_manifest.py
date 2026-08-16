#!/usr/bin/env python3
"""Extract cross-layer PE identities from Allo/Vitis HLS artifacts.

The extractor supports two common Vitis transformations:

* Allo mapped functions such as ``gemm_2_3_...`` are inlined into a parent.
  Their unique C++ loop labels are then used to find Vitis pipeline modules.
* Repeated calls to one HLS function become processes such as ``pe``, ``pe.1``,
  and ``pe.2`` and legalized RTL modules such as ``top_pe``, ``top_pe_1``, and
  ``top_pe_2``.

This is an artifact inspection tool.  It does not require Allo or Vitis Python
packages and can run after synthesis on a different machine.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


def _load_rtl_canonicalizer():
    path = Path(__file__).with_name("rtl_canonicalizer.py")
    spec = importlib.util.spec_from_file_location("_allo_rtl_canonicalizer", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RTLCanonicalizer = _load_rtl_canonicalizer().RTLCanonicalizer


DEFAULT_MAPPED_RE = (
    r"^(?P<kernel>[A-Za-z_]\w*?)_(?P<pid0>\d+)_(?P<pid1>\d+)(?:_|$)"
)


@dataclass(frozen=True)
class CFunction:
    name: str
    parameters: tuple[tuple[str, str], ...]
    body: str
    loop_labels: tuple[str, ...]


def _match_delimiter(text: str, start: int, opening: str, closing: str) -> int:
    """Return the matching delimiter while ignoring comments and strings."""
    depth = 0
    state = "code"
    quote = ""
    i = start
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if state == "line_comment":
            if ch == "\n":
                state = "code"
        elif state == "block_comment":
            if ch == "*" and nxt == "/":
                state = "code"
                i += 1
        elif state == "string":
            if ch == "\\":
                i += 1
            elif ch == quote:
                state = "code"
        elif ch == "/" and nxt == "/":
            state = "line_comment"
            i += 1
        elif ch == "/" and nxt == "*":
            state = "block_comment"
            i += 1
        elif ch in {'"', "'"}:
            state = "string"
            quote = ch
        elif ch == opening:
            depth += 1
        elif ch == closing:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError(f"unmatched {opening!r} at byte {start}")


def parse_cpp_functions(text: str) -> list[CFunction]:
    """Parse C/C++ function definitions sufficiently for generated HLS code."""
    header_re = re.compile(
        r"(?m)^[ \t]*(?:[\w:<>,*&\[\] ]+\s+)?(?P<name>[A-Za-z_]\w*)\s*\("
    )
    ignored = {"if", "for", "while", "switch", "catch"}
    functions: list[CFunction] = []
    for match in header_re.finditer(text):
        name = match.group("name")
        if name in ignored:
            continue
        open_paren = text.find("(", match.start("name"))
        try:
            close_paren = _match_delimiter(text, open_paren, "(", ")")
        except ValueError:
            continue
        brace = close_paren + 1
        while brace < len(text) and text[brace].isspace():
            brace += 1
        if brace >= len(text) or text[brace] != "{":
            continue
        try:
            close_brace = _match_delimiter(text, brace, "{", "}")
        except ValueError:
            continue
        body = text[brace + 1 : close_brace]
        parameter_text = text[open_paren + 1 : close_paren]
        parameters = []
        for declaration in _split_top_level(parameter_text, ","):
            identifiers = re.findall(r"[A-Za-z_]\w*", declaration)
            if not identifiers or declaration.strip() == "void":
                continue
            parameter = identifiers[-1]
            parameter_type = re.sub(
                rf"\b{re.escape(parameter)}\b(?=\s*(?:\[[^]]*\]\s*)*$)",
                "",
                declaration.strip(),
            )
            parameter_type = re.sub(r"\s+", " ", parameter_type).strip()
            parameters.append((parameter, parameter_type))
        labels = tuple(
            re.findall(r"(?m)^[ \t]*([A-Za-z_]\w*)\s*:\s*for\s*\(", body)
        )
        functions.append(CFunction(name, tuple(parameters), body, labels))
    return functions


def _split_top_level(text: str, delimiter: str) -> list[str]:
    parts = []
    start = 0
    depths = {"(": 0, "[": 0, "<": 0, "{": 0}
    closing = {")": "(", "]": "[", ">": "<", "}": "{"}
    for index, char in enumerate(text):
        if char in depths:
            depths[char] += 1
        elif char in closing and depths[closing[char]]:
            depths[closing[char]] -= 1
        elif char == delimiter and not any(depths.values()):
            parts.append(text[start:index].strip())
            start = index + 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


CPP_FIXED_IDENTIFIERS = {
    "alignas", "alignof", "and", "asm", "auto", "bool", "break", "case",
    "catch", "char", "class", "const", "constexpr", "continue", "default",
    "delete", "do", "double", "else", "enum", "explicit", "extern", "false",
    "float", "for", "friend", "goto", "if", "inline", "int", "long",
    "namespace", "new", "noexcept", "not", "nullptr", "operator", "or",
    "private", "protected", "public", "register", "return", "short", "signed",
    "sizeof", "static", "struct", "switch", "template", "this", "throw",
    "true", "try", "typedef", "typename", "union", "unsigned", "using",
    "virtual", "void", "volatile", "while", "read", "write", "read_nb",
    "write_nb", "empty", "full", "size", "hls", "stream", "ap_int", "ap_uint",
    "int8_t", "int16_t", "int32_t", "int64_t", "uint8_t", "uint16_t",
    "uint32_t", "uint64_t",
}


TOKEN_RE = re.compile(
    r'"(?:\\.|[^"\\])*"|'
    r"'(?:[sS]?[bBoOdDhH])?[0-9A-Fa-f_xXzZ?]+|"
    r"'(?:\\.|[^'\\])*'|"
    r"(?:0[xX][0-9A-Fa-f]+|\d+)'[sS]?[bBoOdDhH][0-9A-Fa-f_xXzZ?]+|"
    r"\d+(?:\.\d+)?|[A-Za-z_$][\w$]*|==|!=|<=|>=|<<|>>|&&|\|\||"
    r"::|->|\+\+|--|\S"
)


def _alpha_tokens(text: str, fixed: set[str]) -> tuple[list[str], dict[str, str]]:
    identifiers = {}
    tokens = []
    for token in TOKEN_RE.findall(text):
        if re.fullmatch(r"[A-Za-z_$][\w$]*", token) and token not in fixed:
            identifiers.setdefault(token, f"id{len(identifiers)}")
            token = identifiers[token]
        tokens.append(token)
    return tokens, identifiers


def canonical_hls_serialization(function: CFunction) -> tuple[str, dict]:
    """Return the exact stable HLS serialization consumed by SHA-256."""
    body = re.sub(r"//[^\n]*|/\*.*?\*/", " ", function.body, flags=re.DOTALL)
    body = re.sub(r"(?m)^\s*[A-Za-z_]\w*\s*:\s*(?=for\s*\()", "", body)
    tokens, _ = _alpha_tokens(body, CPP_FIXED_IDENTIFIERS)
    ports = []
    for name, parameter_type in function.parameters:
        reads = bool(
            re.search(rf"\b{re.escape(name)}\s*\.\s*(?:read|read_nb|empty)\b", body)
        )
        writes = bool(
            re.search(rf"\b{re.escape(name)}\s*\.\s*(?:write|write_nb|full)\b", body)
        )
        direction = "inout" if reads and writes else "in" if reads else "out" if writes else "other"
        ports.append({"type": parameter_type, "direction": direction})
    # Sorting makes the signature independent of absolute N/S/E/W port order.
    ports.sort(key=lambda item: (item["direction"], item["type"]))
    serialized = json.dumps({"ports": ports, "tokens": tokens}, sort_keys=True)
    return serialized, {"ports": ports}


def canonical_hls_fingerprint(function: CFunction) -> tuple[str, dict]:
    serialized, interface = canonical_hls_serialization(function)
    return hashlib.sha256(serialized.encode()).hexdigest(), interface


def _implementation_contract(record: dict) -> dict:
    """Return the complete available contract used to share one implementation."""
    return {
        "schema_version": 1,
        "pre_hls_contract_hash": record.get(
            "pre_hls_implementation_contract_hash"
        ),
        "emitted_hls_hash": record.get("hls_equivalence_hash"),
        "emitted_hls_interface": record.get(
            "hls_interface_signature", record.get("hls_interface")
        ),
        "hierarchy_mode": record.get("mapping_mode"),
        "synthesis": record.get("synthesis_contract", {}),
    }


def attach_implementation_contract(record: dict) -> None:
    contract = _implementation_contract(record)
    serialized = json.dumps(contract, sort_keys=True, separators=(",", ":"))
    record["implementation_contract"] = contract
    record["implementation_equivalence_hash"] = hashlib.sha256(
        serialized.encode()
    ).hexdigest()


VERILOG_KEYWORDS = {
    "always", "and", "assign", "automatic", "begin", "buf", "case", "casex",
    "casez", "default", "else", "end", "endcase", "endfunction", "endgenerate",
    "endmodule", "endtask", "for", "force", "forever", "fork", "function",
    "generate", "genvar", "if", "initial", "inout", "input", "integer", "localparam",
    "module", "negedge", "or", "output", "parameter", "posedge", "reg", "release",
    "repeat", "signed", "task", "tri", "unsigned", "wait", "wand", "wire", "wor",
}


def canonical_verilog_serialization(text: str) -> str:
    """Return a conservative structural serialization of generated RTL."""
    return RTLCanonicalizer().serialization(text)


def canonical_verilog_fingerprint(text: str, backend: str | None = None) -> str:
    """Hash RTL modulo comments and generated identifiers."""
    return RTLCanonicalizer(backend=backend).fingerprint(text)


def parse_vitis_log(text: str) -> dict:
    inline = {}
    for function, parent in re.findall(
        r"Inlining function '([^']+)' into '([^']+)'", text
    ):
        inline[function] = parent

    legalizations = dict(
        re.findall(r"Legalizing function name '([^']+)' to '([^']+)'", text)
    )
    generated = list(
        dict.fromkeys(re.findall(r"Generating RTL for module '([^']+)'", text))
    )

    processes = []
    for block in re.findall(
        r"detected/extracted \d+ process function\(s\):(.*?)(?=\n(?:INFO|WARNING|ERROR):)",
        text,
        re.DOTALL,
    ):
        processes.extend(re.findall(r"'([^']+)'", block))
    return {
        "inline": inline,
        "legalizations": legalizations,
        "generated_modules": generated,
        "dataflow_processes": list(dict.fromkeys(processes)),
    }


def parse_vitis_synthesis_contract(text: str) -> dict:
    version = re.search(r"Vitis HLS.*?\bv([0-9][^\s(]*)", text)
    part = re.search(r"Running:\s+set_part\s+\{?([^\s}]+)", text)
    clock = re.search(r"Running:\s+create_clock\s+-period\s+([0-9.]+)", text)
    return {
        "backend": "vitis_hls",
        "tool_version": version.group(1) if version else "unknown",
        "target_part": part.group(1) if part else "unknown",
        "clock_period_ns": float(clock.group(1)) if clock else None,
        "hierarchy_policy": "vitis_dataflow_process",
    }


def extract_rtl_hierarchy(artifacts: dict[str, str], top: str | None = None) -> dict:
    """Parse module definitions and realized instance paths from Verilog text."""
    module_re = re.compile(
        r"(?ms)^\s*module\s+([A-Za-z_][\w$]*)\b.*?\bendmodule\b"
    )
    blocks = {}
    sources = {}
    for source, text in sorted(artifacts.items()):
        for match in module_re.finditer(text):
            name = match.group(1)
            blocks[name] = match.group(0)
            sources[name] = source

    instances = []
    known = set(blocks)
    instance_re = re.compile(
        r"(?m)(?:^|;)\s*([A-Za-z_][\w$]*)"
        r"(?:\s*#\s*\([^;]*?\))?\s+([A-Za-z_][\w$]*)\s*\(",
        re.DOTALL,
    )
    for parent, text in blocks.items():
        for child, instance_name in instance_re.findall(text):
            if child not in known:
                continue
            instances.append(
                {
                    "parent_module": parent,
                    "module": child,
                    "instance_name": instance_name,
                    "source_artifact": sources[parent],
                }
            )

    children = {}
    for instance in instances:
        children.setdefault(instance["parent_module"], []).append(instance)
    instantiated = {instance["module"] for instance in instances}
    roots = [top] if top in blocks else sorted(set(blocks) - instantiated)
    realized = []

    def expand(parent: str, parent_path: str, ancestors: tuple[str, ...]):
        for instance in children.get(parent, []):
            path = f"{parent_path}/{instance['instance_name']}"
            realized.append({**instance, "instance_path": path})
            child = instance["module"]
            if child not in ancestors:
                expand(child, path, (*ancestors, child))

    for root in roots:
        expand(root, root, (root,))

    definitions = []
    for name in sorted(blocks):
        definitions.append(
            {
                "name": name,
                "source_artifact": sources[name],
                "direct_dependencies": sorted(
                    {item["module"] for item in children.get(name, [])}
                ),
            }
        )
    return {
        "schema_version": 1,
        "top": top if top in blocks else None,
        "roots": roots,
        "module_definitions": definitions,
        "module_instances": instances,
        "realized_instances": realized,
    }


def rtl_dependency_closure(root: str, hierarchy: dict) -> list[str]:
    """Return the deterministic module-definition closure rooted at ``root``."""
    dependencies = {
        item["name"]: item.get("direct_dependencies", [])
        for item in hierarchy.get("module_definitions", [])
    }
    result = []
    pending = [root]
    while pending:
        module = pending.pop()
        if module in result or module not in dependencies:
            continue
        result.append(module)
        pending.extend(reversed(dependencies[module]))
    return result


def parse_verilog_modules(rtl_dir: Path) -> tuple[dict[str, str], list[dict]]:
    """Return module-to-file mapping and parent instantiation records."""
    definitions: dict[str, str] = {}
    file_text: dict[str, str] = {}
    module_re = re.compile(r"(?m)^\s*module\s+([A-Za-z_][\w$]*)\b")
    for path in sorted(rtl_dir.glob("*.v")):
        text = path.read_text(encoding="utf-8", errors="replace")
        file_text[str(path)] = text
        for module in module_re.findall(text):
            definitions[module] = str(path)

    # Generated Vitis RTL uses one-line "type instance(" declarations.  Limit
    # matches to known module types to avoid interpreting language constructs.
    instances = []
    for path, text in file_text.items():
        for module in definitions:
            pattern = re.compile(
                rf"(?m)^\s*{re.escape(module)}(?:\s*#\s*\([^;]*?\))?\s+"
                rf"([A-Za-z_][\w$]*)\s*\(",
                re.DOTALL,
            )
            for instance in pattern.findall(text):
                instances.append(
                    {"module": module, "instance": instance, "parent_file": path}
                )
    return definitions, instances


def _rtl_modules_for_process(
    process: str, definitions: dict[str, str]
) -> list[str]:
    candidates = [process, process.replace(".", "_")]
    matches = []
    for module in definitions:
        if any(module == item or module.endswith("_" + item) for item in candidates):
            matches.append(module)
    return sorted(matches, key=len)


def _loop_process_candidates(
    function: CFunction, generated_modules: Iterable[str]
) -> list[str]:
    """Find pipeline processes containing the function's loop-label prefix."""
    if not function.loop_labels:
        return []
    labels = function.loop_labels
    candidates = []
    for process in generated_modules:
        if "Pipeline_" not in process:
            continue
        # Vitis usually names a pipeline after the outer loop, but may flatten
        # it and retain only a unique inner-loop label (for example ``k1``).
        if any(
            re.search(rf"(?:^|_){re.escape(label)}(?:_|$)", process)
            for label in labels
        ):
            candidates.append(process)
    return candidates


def _clone_ordinal(process: str, function_name: str) -> int | None:
    if process == function_name:
        return 0
    match = re.fullmatch(re.escape(function_name) + r"[._](\d+)", process)
    return int(match.group(1)) if match else None


def build_manifest(
    kernel_cpp: Path,
    log_path: Path,
    rtl_dir: Path,
    function_pattern: str,
    function_name: str | None = None,
    kernel_filter: str | None = None,
    function_names: set[str] | None = None,
) -> dict:
    functions = parse_cpp_functions(kernel_cpp.read_text(encoding="utf-8"))
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    log_info = parse_vitis_log(log_text)
    synthesis_contract = parse_vitis_synthesis_contract(log_text)
    definitions, instances = parse_verilog_modules(rtl_dir)
    hierarchy = extract_rtl_hierarchy(
        {
            str(path): path.read_text(encoding="utf-8", errors="replace")
            for path in sorted(rtl_dir.glob("*.v"))
        }
    )
    selector = re.compile(function_pattern)
    selected = []
    normalized_names = (
        {_normalized_hls_function(name) for name in function_names}
        if function_names is not None
        else None
    )
    for function in functions:
        if normalized_names is not None:
            keep = _normalized_hls_function(function.name) in normalized_names
        elif function_name:
            keep = function.name == function_name
        else:
            match = selector.match(function.name)
            keep = bool(match)
            if keep and kernel_filter:
                keep = match.groupdict().get("kernel") == kernel_filter
        if keep:
            selected.append(function)

    records = []
    for function in selected:
        hls_hash, hls_signature = canonical_hls_fingerprint(function)
        match = selector.match(function.name)
        groups = match.groupdict() if match else {}
        pid = None
        if groups.get("pid0") is not None and groups.get("pid1") is not None:
            pid = [int(groups["pid0"]), int(groups["pid1"])]
        kernel = groups.get("kernel") or function.name
        semantic_base = (
            f"{kernel}/pid={pid[0]},{pid[1]}" if pid else function.name
        )

        process_candidates = _loop_process_candidates(
            function, log_info["generated_modules"]
        )
        mode = "inlined_pipeline" if process_candidates else "direct_or_cloned"
        if not process_candidates:
            # Prefer pre-legalization dataflow names.  Combining all three
            # sources would count ``pe.1`` and its legalized ``pe_1`` twice.
            ordinal_processes = []
            for source in (
                log_info["dataflow_processes"],
                log_info["legalizations"],
                log_info["generated_modules"],
            ):
                ordinal_processes = [
                    (_clone_ordinal(process, function.name), process)
                    for process in source
                    if _clone_ordinal(process, function.name) is not None
                ]
                if ordinal_processes:
                    break
            process_candidates = [p for _, p in sorted(ordinal_processes)]

        if not process_candidates:
            empty_source = not re.sub(r"\s|//[^\n]*|/\*.*?\*/", "", function.body, flags=re.DOTALL)
            records.append(
                {
                    "semantic_id": semantic_base,
                    "kernel": kernel,
                    "pid": pid,
                    "hls_function": function.name,
                    "loop_labels": list(function.loop_labels),
                    "hls_equivalence_hash": hls_hash,
                    "hls_interface_signature": hls_signature,
                    "mapping_mode": mode,
                    "synthesis_contract": synthesis_contract,
                    "status": "empty_source" if empty_source else "no_rtl_process",
                }
            )
            continue

        for candidate_index, process in enumerate(process_candidates):
            ordinal = _clone_ordinal(process, function.name)
            semantic_id = semantic_base
            if pid is None and len(process_candidates) > 1:
                semantic_id = f"{semantic_base}/call={ordinal or candidate_index}"
            legalized = log_info["legalizations"].get(process, process.replace(".", "_"))
            rtl_modules = _rtl_modules_for_process(legalized, definitions)
            matching_instances = [
                item for item in instances if item["module"] in rtl_modules
            ]
            hierarchy_instances = [
                item
                for item in hierarchy["module_instances"]
                if item["module"] in rtl_modules
            ]
            parent_modules = sorted(
                {item["parent_module"] for item in hierarchy_instances}
            )
            rtl_root_module = (
                parent_modules[0]
                if len(parent_modules) == 1
                else rtl_modules[0]
                if len(rtl_modules) == 1
                else None
            )
            rtl_hash = None
            if len(rtl_modules) == 1:
                rtl_text = Path(definitions[rtl_modules[0]]).read_text(
                    encoding="utf-8", errors="replace"
                )
                rtl_hash = canonical_verilog_fingerprint(rtl_text)
            status = "matched"
            if not rtl_modules:
                status = "no_rtl_module"
            elif len(rtl_modules) > 1:
                status = "ambiguous_rtl_module"
            records.append(
                {
                    "semantic_id": semantic_id,
                    "kernel": kernel,
                    "pid": pid,
                    "hls_function": function.name,
                    "loop_labels": list(function.loop_labels),
                    "hls_equivalence_hash": hls_hash,
                    "hls_interface_signature": hls_signature,
                    "mapping_mode": mode,
                    "synthesis_contract": synthesis_contract,
                    "vitis_parent": log_info["inline"].get(function.name),
                    "vitis_process": process,
                    "legalized_process": legalized,
                    "rtl_modules": [
                        {"name": module, "file": definitions[module]}
                        for module in rtl_modules
                    ],
                    "rtl_instances": matching_instances,
                    "rtl_root_module": rtl_root_module,
                    "rtl_root_instances": [
                        item
                        for item in hierarchy["realized_instances"]
                        if item["module"] == rtl_root_module
                    ],
                    "rtl_equivalence_hash": rtl_hash,
                    "status": status,
                }
            )

    macro_groups, candidate_groups = build_equivalence_groups(records)
    return {
        "schema_version": 1,
        "inputs": {
            "kernel_cpp": str(kernel_cpp),
            "vitis_log": str(log_path),
            "rtl_dir": str(rtl_dir),
        },
        "records": records,
        "rtl_hierarchy": hierarchy,
        "macro_groups": macro_groups,
        "candidate_groups": candidate_groups,
        "summary": {
            "selected_functions": len(selected),
            "records": len(records),
            "matched": sum(record["status"] == "matched" for record in records),
            "empty_source": sum(
                record["status"] == "empty_source" for record in records
            ),
            "unmatched_or_ambiguous": sum(
                record["status"] not in {"matched", "empty_source"}
                for record in records
            ),
            "macro_classes": len(macro_groups),
            "repeated_macro_classes": sum(
                group["member_count"] > 1 for group in macro_groups
            ),
            "instances_in_repeated_classes": sum(
                group["member_count"]
                for group in macro_groups
                if group["member_count"] > 1
            ),
            "candidate_classes": len(candidate_groups),
        },
    }


def build_equivalence_groups(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """Group interchangeable blocks by their specialized MLIR/HLS contract.

    Generated RTL fingerprints are retained as audit evidence. They no longer
    define macro identity: every member is intentionally replaced by the one
    selected representative implementation.
    """
    by_contract: dict[str, list[dict]] = {}
    for record in records:
        if record.get("status") != "matched":
            continue
        attach_implementation_contract(record)
        by_contract.setdefault(
            record["implementation_equivalence_hash"], []
        ).append(record)

    macro_groups = []
    audits = []
    for contract_hash, members in sorted(by_contract.items()):
        group_id = f"macro_hls_{contract_hash[:16]}"
        member_entries = []
        for member in members:
            member_entries.append(
                {
                    "semantic_id": member["semantic_id"],
                    "rtl_module": member["rtl_modules"][0]["name"],
                    # Directional placement is intentionally excluded from the
                    # proof.  A later channel-graph pass chooses a legal D4
                    # transform and corresponding pin-bundle permutation.
                    "orientation": "unassigned",
                }
            )
        macro_groups.append(
            {
                "macro_class_id": group_id,
                "representative": members[0]["semantic_id"],
                "members": member_entries,
                "member_count": len(members),
                "proof": {
                    "status": "proven",
                    "method": "specialized_mlir_emitted_hls_contract",
                    "implementation_contract_hash": contract_hash,
                    "scope": "representative implementation substitution",
                    "contract": members[0]["implementation_contract"],
                },
                "rtl_audit": {
                    "authority": False,
                    "distinct_hashes": sorted(
                        {
                            member.get("rtl_equivalence_hash")
                            for member in members
                            if member.get("rtl_equivalence_hash")
                        }
                    ),
                },
                "orientation_policy": {
                    "direction_in_fingerprint": False,
                    "allowed_transform_family": "D4",
                    "selected_per_member": False,
                },
            }
        )
        rtl_hashes = macro_groups[-1]["rtl_audit"]["distinct_hashes"]
        macro_groups[-1]["rtl_audit"]["status"] = (
            "agree" if len(rtl_hashes) <= 1 else "generated_rtl_diverged"
        )
        if len(rtl_hashes) > 1:
            audits.append(
            {
                "audit_id": f"rtl_divergence_{contract_hash[:16]}",
                "members": [member["semantic_id"] for member in members],
                "implementation_contract_hash": contract_hash,
                "rtl_hashes": rtl_hashes,
                "status": "diagnostic_only_representative_will_be_used",
            }
        )
    return macro_groups, audits


def _normalized_hls_function(name: str) -> str:
    return name[: -len("_fixed")] if name.endswith("_fixed") else name


def merge_pre_hls_manifest(pre_manifest: dict, post_manifest: dict) -> dict:
    """Join compiler identities/connectivity to HLS/Vitis/RTL evidence."""
    merged = copy.deepcopy(pre_manifest)
    pe_by_function = {
        _normalized_hls_function(pe["specialized_function"]): pe
        for pe in merged.get("pe_instances", [])
    }
    joined_records = []
    unmatched_records = []
    for original in post_manifest["records"]:
        record = copy.deepcopy(original)
        pe = pe_by_function.get(_normalized_hls_function(record["hls_function"]))
        if pe is None:
            unmatched_records.append(record)
            joined_records.append(record)
            continue
        record["semantic_id"] = pe["semantic_id"]
        record["kernel"] = pe["kernel"]
        record["pid"] = pe["pid"]
        record["pre_hls_implementation_contract"] = pe.get(
            "pre_hls_implementation_contract"
        )
        record["pre_hls_implementation_contract_hash"] = pe.get(
            "pre_hls_implementation_contract_hash"
        )
        pe.setdefault("post_hls_records", []).append(record)
        joined_records.append(record)

    macro_groups, candidate_groups = build_equivalence_groups(joined_records)
    merged.update(
        {
            "schema_version": max(2, merged.get("schema_version", 1)),
            "stage": "post_hls_enriched",
            "post_hls_inputs": post_manifest["inputs"],
            "post_hls_records": joined_records,
            "macro_groups": macro_groups,
            "post_hls_candidate_groups": candidate_groups,
            "rtl_hierarchy": post_manifest.get("rtl_hierarchy", {}),
            "summary": {
                **post_manifest["summary"],
                "pre_hls_pe_instances": len(merged.get("pe_instances", [])),
                "joined_post_hls_records": len(joined_records)
                - len(unmatched_records),
                "unjoined_post_hls_records": len(unmatched_records),
            },
        }
    )
    return merged


def write_debug_artifacts(
    debug_dir: Path,
    kernel_cpp: Path,
    log_path: Path,
    rtl_dir: Path,
    manifest: dict,
) -> None:
    """Write exact post-HLS canonical inputs and parsed naming evidence."""
    debug_dir.mkdir(parents=True, exist_ok=True)
    functions = {
        function.name: function
        for function in parse_cpp_functions(kernel_cpp.read_text(encoding="utf-8"))
    }
    log_info = parse_vitis_log(
        log_path.read_text(encoding="utf-8", errors="replace")
    )
    (debug_dir / "parsed-vitis-log.json").write_text(
        json.dumps(log_info, indent=2) + "\n", encoding="utf-8"
    )
    hls_dir = debug_dir / "canonical" / "hls"
    rtl_debug_dir = debug_dir / "canonical" / "rtl"
    hls_dir.mkdir(parents=True, exist_ok=True)
    rtl_debug_dir.mkdir(parents=True, exist_ok=True)
    written_hls = set()
    written_rtl = set()
    records = manifest.get("post_hls_records", manifest.get("records", []))
    for record in records:
        function_name = record["hls_function"]
        if function_name not in written_hls and function_name in functions:
            serialized, _ = canonical_hls_serialization(functions[function_name])
            (hls_dir / f"{function_name}.txt").write_text(
                serialized + "\n", encoding="utf-8"
            )
            written_hls.add(function_name)
        for module in record.get("rtl_modules", []):
            module_name = module["name"]
            if module_name in written_rtl:
                continue
            rtl_path = Path(module["file"])
            if not rtl_path.is_absolute():
                rtl_path = rtl_dir / rtl_path.name
            text = rtl_path.read_text(encoding="utf-8", errors="replace")
            (rtl_debug_dir / f"{module_name}.txt").write_text(
                canonical_verilog_serialization(text) + "\n",
                encoding="utf-8",
            )
            written_rtl.add(module_name)


def _find_log(solution: Path) -> Path:
    candidates = [solution / "solution1.log", solution.parent / "vitis_hls.log"]
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("no Vitis log found; pass --vitis-log explicitly")


def _tcl_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def _to_tcl(value, indent: int = 0) -> str:
    """Render JSON-compatible data as dependency-free Tcl dicts/lists."""
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _tcl_string(value)
    pad = " " * indent
    child_pad = " " * (indent + 2)
    if isinstance(value, list):
        if not value:
            return "[list]"
        rendered = [f"{child_pad}{_to_tcl(item, indent + 2)}" for item in value]
        return "[list \\\n" + " \\\n".join(rendered) + f"\n{pad}]"
    if isinstance(value, dict):
        if not value:
            return "[dict create]"
        rendered = []
        for key, item in value.items():
            rendered.append(
                f"{child_pad}{_tcl_string(str(key))} {_to_tcl(item, indent + 2)}"
            )
        return "[dict create \\\n" + " \\\n".join(rendered) + f"\n{pad}]"
    raise TypeError(f"unsupported Tcl manifest value: {type(value).__name__}")


def render_tcl_manifest(manifest: dict, variable: str = "allo_asic_manifest") -> str:
    return (
        "# Generated by extract_vitis_pe_manifest.py.\n"
        "# This file requires only Tcl's built-in dict and list commands.\n"
        f"set {variable} {_to_tcl(manifest)}\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel-cpp", required=True, type=Path)
    parser.add_argument(
        "--solution-dir",
        required=True,
        type=Path,
        help="Vitis solution directory containing syn/verilog",
    )
    parser.add_argument("--vitis-log", type=Path)
    parser.add_argument(
        "--pre-manifest",
        type=Path,
        help="pre-HLS ASIC manifest to enrich with HLS/Vitis/RTL evidence",
    )
    parser.add_argument(
        "--debug-dir",
        type=Path,
        help="write parsed log and exact canonical HLS/RTL hash inputs here",
    )
    parser.add_argument(
        "--function",
        help="select one exact HLS function (useful for Vitis clone analysis)",
    )
    parser.add_argument(
        "--kernel",
        help="select one kernel captured by the regex's named 'kernel' group",
    )
    parser.add_argument(
        "--function-regex",
        default=DEFAULT_MAPPED_RE,
        help="regex for mapped functions; named kernel/pid0/pid1 groups are used",
    )
    parser.add_argument("--output", "-o", type=Path)
    parser.add_argument(
        "--tcl-output",
        type=Path,
        help="Tcl output path (defaults to OUTPUT with a .tcl suffix)",
    )
    args = parser.parse_args(argv)

    solution = args.solution_dir.resolve()
    rtl_dir = solution / "syn" / "verilog"
    log_path = args.vitis_log.resolve() if args.vitis_log else _find_log(solution)
    if not rtl_dir.is_dir():
        parser.error(f"RTL directory does not exist: {rtl_dir}")

    pre_manifest = None
    function_names = None
    if args.pre_manifest:
        pre_manifest = json.loads(args.pre_manifest.read_text(encoding="utf-8"))
        function_names = {
            pe["specialized_function"]
            for pe in pre_manifest.get("pe_instances", [])
        }
    post_manifest = build_manifest(
        args.kernel_cpp.resolve(),
        log_path,
        rtl_dir,
        args.function_regex,
        args.function,
        args.kernel,
        function_names,
    )
    if pre_manifest is not None:
        manifest = merge_pre_hls_manifest(pre_manifest, post_manifest)
    else:
        manifest = post_manifest
    if args.debug_dir:
        write_debug_artifacts(
            args.debug_dir.resolve(),
            args.kernel_cpp.resolve(),
            log_path,
            rtl_dir,
            manifest,
        )
    rendered = json.dumps(manifest, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
        tcl_output = args.tcl_output or args.output.with_suffix(".tcl")
        tcl_output.write_text(render_tcl_manifest(manifest), encoding="utf-8")
    elif args.tcl_output:
        args.tcl_output.write_text(render_tcl_manifest(manifest), encoding="utf-8")
        sys.stdout.write(rendered)
    else:
        sys.stdout.write(rendered)
    return 1 if manifest["summary"]["unmatched_or_ambiguous"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
