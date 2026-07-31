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
    path = os.path.abspath(path)
    default_final = os.path.splitext(path)[0] + "-final.json"
    final_path = os.path.expanduser(value.get("final_path", default_final))
    if not os.path.isabs(final_path):
        final_path = os.path.join(project, final_path)
    debug_dir = os.path.expanduser(value.get("debug_dir", "asic-debug"))
    if not os.path.isabs(debug_dir):
        debug_dir = os.path.join(project, debug_dir)
    return {
        **value,
        "enabled": True,
        "path": path,
        "final_path": os.path.abspath(final_path),
        "debug_artifacts": bool(value.get("debug_artifacts", False)),
        "debug_dir": os.path.abspath(debug_dir),
    }


def _sha256(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_pre_hls_ir(text):
    """Canonicalize an MLIR operation body modulo port order and SSA names."""
    text = re.sub(r"loc\([^\n]*\)", "", text)
    body_start = re.search(r"\)\s*(?:attributes\s*\{[^{}]*\}\s*)?\{", text)
    if body_start and "}" in text:
        text = text[body_start.end() : text.rfind("}")]
    text = re.sub(r"\bfunc\.return\b", "", text)
    # These attributes are frontend/debug identifiers.  Operations, types,
    # constants, control attributes, and stream behavior remain unchanged.
    text = re.sub(
        r'\b(loop_name|op_name|name|from|to)\s*=\s*"[^"]*"',
        r'\1 = "<generated>"',
        text,
    )
    names = {}

    def rename(match):
        name = match.group(0)
        names.setdefault(name, f"%v{len(names)}")
        return names[name]

    text = re.sub(r"%[A-Za-z_$][\w$.-]*|%\d+", rename, text)
    return " ".join(text.split())


def _semantic_identity(top, function_name, mappings, identity=None):
    if identity:
        kernel = identity["kernel"]
        pid = identity.get("pid")
        parent = identity.get("parent_region")
        path = [top]
        if parent and parent != top:
            path.append(parent)
        path.append(kernel)
        if pid is not None:
            path.append(f"pid={','.join(map(str, pid))}")
        return "/".join(path), kernel, pid
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


def build_pre_hls_manifest(
    *, top, functions, stream_info, stream_types, extra_stream_info,
    func_instances=None, mappings=None, identities=None, project=None,
):
    """Build the serializable region graph from compiler-owned data.

    ``functions`` maps specialized symbols to their textual pre-HLS MLIR.
    The other arguments are the concrete products of
    ``move_stream_to_interface``.
    """
    func_instances = func_instances or {}
    mappings = mappings or {}
    identities = identities or {}
    extra_stream_info = extra_stream_info or {}
    pes = []
    channels = {}
    for function_name, endpoints in stream_info.items():
        identity = identities.get(function_name)
        semantic_id, kernel, pid = _semantic_identity(
            top, function_name, mappings, identity
        )
        body = canonical_pre_hls_ir(functions.get(function_name, ""))
        interface = sorted(
            (direction, str(stream_types.get(stream_name, "unknown")))
            for stream_name, direction in endpoints
        )
        pe = {
            "semantic_id": semantic_id,
            "region": (identity or {}).get("parent_region", top),
            "kernel": kernel,
            "specialized_function": function_name,
            "pid": pid,
            "mapping": _json_value(
                (identity or {}).get("mapping", mappings.get(kernel))
            ),
            "specialization_suffix": (identity or {}).get(
                "specialization_suffix"
            ),
            "predicate_tag": (identity or {}).get("predicate_tag"),
            "selected_branch_trace": (identity or {}).get(
                "selected_branch_trace", []
            ),
            "pre_hls_equivalence_hash": _sha256(body),
            "interface_hash": _sha256(json.dumps(interface, sort_keys=True)),
            "ports": [],
        }
        if pid is not None and pe["predicate_tag"] is None:
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
            endpoint_key = (semantic_id, direction)
            endpoint = channel.setdefault("_endpoint_map", {}).setdefault(
                endpoint_key,
                {
                    "pe": semantic_id,
                    "direction": direction,
                    "role": "consumer" if direction == "in" else "producer",
                    "accesses": [],
                },
            )
            endpoint["accesses"].append(
                {"port_ordinal": ordinal, "operation": port["operation"]}
            )
        pes.append(pe)
    for channel in channels.values():
        channel["endpoints"] = list(channel.pop("_endpoint_map").values())
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


def write_debug_artifact(config, relative_path, text):
    """Write one optional compiler/debug artifact under the configured root."""
    if not config or not config.get("debug_artifacts"):
        return None
    path = os.path.join(config["debug_dir"], relative_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as outfile:
        outfile.write(text)
        if text and not text.endswith("\n"):
            outfile.write("\n")
    return path


def write_pre_hls_debug_artifacts(config, functions, identities):
    """Write raw/canonical PE functions and the explicit identity side table."""
    if not config or not config.get("debug_artifacts"):
        return
    identity_path = os.path.join(config["debug_dir"], "identity-map.json")
    os.makedirs(os.path.dirname(identity_path), exist_ok=True)
    with open(identity_path, "w", encoding="utf-8") as outfile:
        json.dump(_json_value(identities), outfile, indent=2)
        outfile.write("\n")
    for function_name, text in functions.items():
        filename = re.sub(r"[^A-Za-z0-9_.-]", "_", function_name)
        write_debug_artifact(config, f"functions/{filename}.mlir", text)
        write_debug_artifact(
            config,
            f"canonical/pre-hls/{filename}.txt",
            canonical_pre_hls_ir(text),
        )
