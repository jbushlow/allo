# Copyright Allo authors. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Dependency-free helpers for Allo's ASIC PE/channel manifest."""

from __future__ import annotations

import hashlib
import json
import os
import re

def normalize_manifest_config(configs, project):
    """Validate and resolve ``configs["asic_manifest"]``."""
    value = (configs or {}).get("asic_manifest")
    if not value:
        return None
    if value is True:
        value = {"enabled": True}
    if not isinstance(value, dict):
        raise TypeError("configs['asic_manifest'] must be a bool or dictionary")
    if not value.get("enabled", True):
        return None
    path = os.path.expanduser(value.get("path", "asic-manifest.json"))
    if not os.path.isabs(path):
        path = os.path.join(project, path)
    return {**value, "enabled": True, "path": os.path.abspath(path)}


def _sha256(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_pre_hls_ir(text):
    """Canonicalize an MLIR operation body modulo port order and SSA names."""
    text = re.sub(r"loc\([^\n]*\)", "", text)
    body_start = re.search(r"\)\s*(?:attributes\s*\{[^{}]*\}\s*)?\{", text)
    if body_start and "}" in text:
        text = text[body_start.end() : text.rfind("}")]
    text = re.sub(r"\bfunc\.return\b", "", text)
    names = {}

    def rename(match):
        name = match.group(0)
        names.setdefault(name, f"%v{len(names)}")
        return names[name]

    text = re.sub(r"%[A-Za-z_$][\w$.-]*|%\d+", rename, text)
    return " ".join(text.split())


def _semantic_identity(top, function_name, mappings):
    match = re.match(r"(.+?)(_\d+(?:_\d+)*)$", function_name)
    if match:
        kernel = match.group(1).rstrip("_")
        pid = tuple(int(item) for item in match.group(2).split("_") if item)
    else:
        kernel, pid = function_name, ()
    # Specialization can append compile-time parameters after spatial indices.
    # The declared mapping rank tells us how many leading suffix values are PID.
    mapping = mappings.get(kernel)
    if pid and isinstance(mapping, (list, tuple)):
        pid = pid[: len(mapping)]
    if pid:
        return f"{top}/{kernel}/pid={','.join(map(str, pid))}", kernel, list(pid)
    return f"{top}/{function_name}", kernel, None


def infer_mappings(func_instances):
    """Infer each specialized kernel's rectangular PID extent."""
    mappings = {}
    for kernel, instances in (func_instances or {}).items():
        pids = [pid for pid in instances if isinstance(pid, tuple)]
        if not pids:
            continue
        rank = len(pids[0])
        if rank and all(len(pid) == rank for pid in pids):
            mappings[kernel] = [max(pid[axis] for pid in pids) + 1 for axis in range(rank)]
    return mappings


def build_pre_hls_manifest(
    *, top, functions, stream_info, stream_types, extra_stream_info,
    func_instances=None, mappings=None, project=None,
):
    """Build the serializable region graph from compiler-owned data.

    ``functions`` maps specialized symbols to their textual pre-HLS MLIR.
    The other arguments are the concrete products of
    ``move_stream_to_interface``.
    """
    func_instances = func_instances or {}
    inferred_mappings = infer_mappings(func_instances)
    mappings = {**inferred_mappings, **(mappings or {})}
    extra_stream_info = extra_stream_info or {}
    pes = []
    channels = {}
    for function_name, endpoints in stream_info.items():
        semantic_id, kernel, pid = _semantic_identity(top, function_name, mappings)
        body = canonical_pre_hls_ir(functions.get(function_name, ""))
        interface = sorted(
            (direction, str(stream_types.get(stream_name, "unknown")))
            for stream_name, direction in endpoints
        )
        pe = {
            "semantic_id": semantic_id,
            "region": top,
            "kernel": kernel,
            "specialized_function": function_name,
            "pid": pid,
            "mapping": _json_value(mappings.get(kernel)),
            "predicate_tag": None,
            "pre_hls_equivalence_hash": _sha256(body),
            "interface_hash": _sha256(json.dumps(interface, sort_keys=True)),
            "ports": [],
        }
        if pid is not None:
            predicate = func_instances.get(kernel, {}).get(tuple(pid))
            if predicate is not None:
                pe["predicate_tag"] = str(predicate)
        for ordinal, (stream_name, direction) in enumerate(endpoints):
            port = {
                "ordinal": ordinal,
                "channel_id": f"{top}/channel={stream_name}",
                "stream": stream_name,
                "direction": direction,
                "operation": "get" if direction == "in" else "put",
                "blocking": True,
                "type": str(stream_types.get(stream_name, "unknown")),
                "symbol_bindings": _json_value(
                    extra_stream_info.get(function_name, {}).get(stream_name, {})
                ),
                "desired_compass_direction": "unassigned",
            }
            pe["ports"].append(port)
            channel = channels.setdefault(
                stream_name,
                {
                    "channel_id": port["channel_id"],
                    "stream": stream_name,
                    "type": port["type"],
                    "endpoints": [],
                },
            )
            channel["endpoints"].append(
                {
                    "pe": semantic_id,
                    "port_ordinal": ordinal,
                    "direction": direction,
                    "operation": port["operation"],
                }
            )
        pes.append(pe)
    candidates = {}
    for pe in pes:
        key = (pe["pre_hls_equivalence_hash"], pe["interface_hash"])
        candidates.setdefault(key, []).append(pe["semantic_id"])
    candidate_groups = [
        {
            "candidate_class_id": f"pre_hls_candidate_{body_hash[:16]}",
            "members": members,
            "member_count": len(members),
            "evidence": {
                "pre_hls_equivalence_hash": body_hash,
                "interface_hash": interface_hash,
            },
            "status": "candidate_until_post_hls_rtl_equivalence",
        }
        for (body_hash, interface_hash), members in sorted(candidates.items())
    ]
    return {
        "schema_version": 2,
        "stage": "pre_hls",
        "top": top,
        "project": project,
        "pe_instances": pes,
        "channels": list(channels.values()),
        "macro_groups": [],
        "candidate_groups": candidate_groups,
        "capture": {
            "point": "after_stream_interface_expansion_before_build_top",
            "orientation_status": "logical_graph_captured_physical_assignment_pending",
        },
    }


def _json_value(value):
    """Convert compiler attributes and tuples to stable JSON-compatible data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)


def _tcl_string(value):
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


def _to_tcl(value, indent=0):
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _tcl_string(value)
    pad, child = " " * indent, " " * (indent + 2)
    if isinstance(value, list):
        if not value:
            return "[list]"
        return "[list \\\n" + " \\\n".join(
            f"{child}{_to_tcl(item, indent + 2)}" for item in value
        ) + f"\n{pad}]"
    if isinstance(value, dict):
        if not value:
            return "[dict create]"
        return "[dict create \\\n" + " \\\n".join(
            f"{child}{_tcl_string(str(key))} {_to_tcl(item, indent + 2)}"
            for key, item in value.items()
        ) + f"\n{pad}]"
    raise TypeError(f"unsupported Tcl manifest value: {type(value).__name__}")


def write_manifest(manifest, path):
    """Write paired JSON and dependency-free Tcl manifests."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as outfile:
        json.dump(manifest, outfile, indent=2)
        outfile.write("\n")
    tcl_path = os.path.splitext(path)[0] + ".tcl"
    with open(tcl_path, "w", encoding="utf-8") as outfile:
        outfile.write("# Generated by Allo's ASIC manifest capture.\n")
        outfile.write("# Requires only Tcl built-in dict and list commands.\n")
        outfile.write(f"set allo_asic_manifest {_to_tcl(manifest)}\n")
    return path, tcl_path
