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
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


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
                rf"\b{re.escape(parameter)}\b(?=\s*(?:\[[^]]*\]\s*)?$)",
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


def canonical_hls_fingerprint(function: CFunction) -> tuple[str, dict]:
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
    return hashlib.sha256(serialized.encode()).hexdigest(), {"ports": ports}


VERILOG_KEYWORDS = {
    "always", "and", "assign", "automatic", "begin", "buf", "case", "casex",
    "casez", "default", "else", "end", "endcase", "endfunction", "endgenerate",
    "endmodule", "endtask", "for", "force", "forever", "fork", "function",
    "generate", "genvar", "if", "initial", "inout", "input", "integer", "localparam",
    "module", "negedge", "or", "output", "parameter", "posedge", "reg", "release",
    "repeat", "signed", "task", "tri", "unsigned", "wait", "wand", "wire", "wor",
}


def canonical_verilog_fingerprint(text: str) -> str:
    """Hash RTL modulo comments, generated identifiers, and port permutations."""
    text = re.sub(r"//[^\n]*|/\*.*?\*/", " ", text, flags=re.DOTALL)
    own_modules = set(re.findall(r"(?m)^\s*module\s+([A-Za-z_][\w$]*)", text))
    instantiated_types = set(
        re.findall(
            r"(?m)(?:^|;)\s*([A-Za-z_][\w$]*)\s+(?:#\s*\([^;]*?\)\s*)?"
            r"[A-Za-z_][\w$]*\s*\(",
            text,
        )
    )
    # A referenced child-module type is a semantic global symbol, not a local
    # generated identifier.  Preserve it unless its definition is in this text;
    # this may miss some recursive-equivalence opportunities but cannot merge
    # modules merely because two different child types occur in the same place.
    fixed = VERILOG_KEYWORDS | (instantiated_types - own_modules)
    tokens, _ = _alpha_tokens(text, fixed)
    return hashlib.sha256(" ".join(tokens).encode()).hexdigest()


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
        # The outer label is the durable join.  Prefer candidates that also
        # contain the immediately nested label to avoid prefix collisions.
        if re.search(rf"(?:^|_){re.escape(labels[0])}(?:_|$)", process):
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
) -> dict:
    functions = parse_cpp_functions(kernel_cpp.read_text(encoding="utf-8"))
    log_info = parse_vitis_log(log_path.read_text(encoding="utf-8", errors="replace"))
    definitions, instances = parse_verilog_modules(rtl_dir)
    selector = re.compile(function_pattern)
    selected = []
    for function in functions:
        if function_name:
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
                    "vitis_parent": log_info["inline"].get(function.name),
                    "vitis_process": process,
                    "legalized_process": legalized,
                    "rtl_modules": [
                        {"name": module, "file": definitions[module]}
                        for module in rtl_modules
                    ],
                    "rtl_instances": matching_instances,
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
    """Build proven RTL groups and broader pre-HLS candidate groups.

    Equality of canonical RTL token streams is a conservative alpha-equivalence
    proof: the implementations differ only by generated identifiers.  HLS hash
    equality is used only for candidate discovery because downstream scheduling
    may still differ.
    """
    by_rtl: dict[str, list[dict]] = {}
    by_hls: dict[str, list[dict]] = {}
    for record in records:
        if record.get("status") != "matched":
            continue
        if record.get("rtl_equivalence_hash"):
            by_rtl.setdefault(record["rtl_equivalence_hash"], []).append(record)
        if record.get("hls_equivalence_hash"):
            by_hls.setdefault(record["hls_equivalence_hash"], []).append(record)

    macro_groups = []
    record_to_macro = {}
    for rtl_hash, members in sorted(by_rtl.items()):
        group_id = f"macro_alpha_{rtl_hash[:16]}"
        member_entries = []
        for member in members:
            record_to_macro[member["semantic_id"]] = group_id
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
                    "method": "canonical_rtl_alpha_equivalence",
                    "rtl_hash": rtl_hash,
                    "scope": "cycle-accurate generated RTL structure",
                },
                "orientation_policy": {
                    "direction_in_fingerprint": False,
                    "allowed_transform_family": "D4",
                    "selected_per_member": False,
                },
            }
        )

    candidate_groups = []
    for hls_hash, members in sorted(by_hls.items()):
        macro_ids = sorted(
            {record_to_macro.get(member["semantic_id"]) for member in members}
            - {None}
        )
        if len(members) < 2 or len(macro_ids) <= 1:
            continue
        candidate_groups.append(
            {
                "candidate_class_id": f"hls_candidate_{hls_hash[:16]}",
                "members": [member["semantic_id"] for member in members],
                "hls_hash": hls_hash,
                "rtl_macro_classes": macro_ids,
                "status": "requires_sequential_equivalence",
            }
        )
    return macro_groups, candidate_groups


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

    manifest = build_manifest(
        args.kernel_cpp.resolve(),
        log_path,
        rtl_dir,
        args.function_regex,
        args.function,
        args.kernel,
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
