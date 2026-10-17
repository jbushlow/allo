# Copyright Allo authors. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The ``asic_manifest`` that ``allo/backend/asic`` consumes.

``allo/backend/asic`` -- Julian Bushlow's mflowgen flow, vendored here (see its
``PROVENANCE.md``) -- has two entry points. The *flat flow* takes a directory of
Verilog and a file list, and knows nothing about Allo. **AAAH**, the ``allo-*``
nodes, instead loads an Allo design and calls ``build(project, target, mode,
configs)`` with ``configs["asic_manifest"] = {...}``, asking the compiler to
emit the architecture it just compiled: unit boundaries, semantic equivalence
classes, stream bundles with widths and directions, PE grid coordinates. Its
planners then place macros on a grid from PID deltas and weight the edges by
pre-HLS stream bit width -- none of which survives in emitted Verilog.

This module is that emitter. Everything here is **derived from the realized
dataflow IR**, never from a table someone typed: a hand-written manifest would
be a second editing surface for the architecture, which is exactly what having
one Allo specification is meant to remove. The kernel instances, their grid
coordinates, their stream ports and the channels between them are read back out
of the module that Allo is about to hand to HLS, so the manifest cannot drift
from what is built without the build changing too.

Two files, two stages, matching what ``nodes/allo-asic-compilation`` validates:

``asic-manifest.json`` (``stage: "pre_hls"``)
    The architecture. Written at ``build()`` time, before any HLS tool runs, so
    it exists whether or not a licence does.

``asic-manifest-final.json`` (``stage: "post_hls_enriched"``)
    The same records joined to the RTL the backend produced -- each PE's root
    module and its split-process children, an equivalence hash per PE, and the
    macro classes those hashes induce. Written after synthesis succeeds.

Each has a ``.tcl`` sibling, because the flow's nodes stage both.

Backend-specific enrichment stays behind ``emit_final``. Vitis joins its HLS
process log to split RTL modules; SystemC joins emitted ``SC_MODULE`` kernels
to Catapult's self-contained RTL and captures the realized boundary-memory pin
protocol. Both produce the same common implementation and equivalence records.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 2

# `analyze_arg_load_store` vocabulary -> the manifest's `direction` vocabulary.
DIRECTION_MAP = {
    "in": "input",
    "out": "output",
    "both": "inout",
    "scalar": "input",
}

# The ops a `@df.region()` uses to declare a link. `allo.stream_construct` is
# the Stream; wire/channel are the SystemC emitter's additions.
_LINK_OPS = {
    "allo.stream_construct",
    "allo.wire_construct",
    "allo.channel_construct",
}

_STYPE_DIRECTION = {"i": "in", "o": "out", "b": "inout"}

# A generated design is more than its kernels: the backend also emits FIFOs,
# `m_axi` adapters, the control slave and arithmetic cores, and those belong to
# no `@df.kernel`. They are not enumerated by a vendor-specific name list here
# -- which would rot -- but are simply what is left once every kernel has
# claimed its own modules. What IS an error is an unclaimed module that is
# named after one of *this design's* kernels: that means the join missed.


# ---------------------------------------------------------------------------
# Reading the design back out of the IR
# ---------------------------------------------------------------------------


def _find_func(module, name):
    for op in module.body.operations:
        if op.operation.name == "func.func" and op.attributes["sym_name"].value == name:
            return op
    return None


def split_instance(instance, mappings=None):
    """``"pe_0_3"`` -> ``("pe", (0, 3))``.

    A ``@df.kernel(mapping=[...])`` is realized as one function per grid point,
    named ``<kernel>_<i>_<j>...``. When the region's ``mappings`` dict is
    available its keys give the split exactly; a kernel whose own name ends in
    ``_<digits>`` is otherwise indistinguishable from one grid axis, so without
    it we strip every trailing numeric group and say so by construction.
    """
    if mappings:
        for kernel in sorted(mappings, key=len, reverse=True):
            if instance == kernel:
                return kernel, (0,)
            if instance.startswith(kernel + "_"):
                tail = instance[len(kernel) + 1 :]
                if re.fullmatch(r"\d+(_\d+)*", tail):
                    return kernel, tuple(int(p) for p in tail.split("_"))
    base, pid = instance, []
    while True:
        match = re.fullmatch(r"(.+?)_(\d+)", base)
        if match is None:
            break
        base = match.group(1)
        pid.insert(0, int(match.group(2)))
    return base, tuple(pid) if pid else (0,)


def semantic_id(top, kernel, pid):
    """``top/kernel/pid=i,j`` -- the flow's primary key for a PE.

    ``plan_physical_intent`` splits this on ``/`` (first two segments are the
    kernel) and regex-matches ``pid=([0-9,-]+)`` for the grid coordinate, so
    the shape is not ours to vary.
    """
    return f"{top}/{kernel}/pid={','.join(str(p) for p in pid)}"


def _sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


def canonical_pre_hls_ir(text):
    """Normalize one realized function modulo debug and SSA spelling."""
    text = re.sub(r"loc\([^\n]*\)", "", text)
    body = re.search(r"\)\s*(?:attributes\s*\{[^{}]*\}\s*)?\{", text)
    if body and "}" in text:
        text = text[body.end() : text.rfind("}")]
    text = re.sub(r"\bfunc\.return\b", "", text)
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


def _instance_identity(func, instance, mappings):
    """Read compiler-owned identity, falling back to the generated symbol."""
    attrs = func.attributes
    if "df.kernel_name" not in attrs:
        kernel, pid = split_instance(instance, mappings)
        mapping = (mappings or {}).get(kernel)
        return {
            "kernel": kernel,
            "pid": list(pid),
            "mapping": list(mapping) if mapping is not None else None,
            "parent_region": None,
            "specialization_suffix": None,
            "predicate_tag": None,
            "selected_branch_trace": [],
            "pid_generalization_policy": None,
            "pid_generalized_axes": [],
        }

    required = ("df.pid", "df.mapping")
    missing = [name for name in required if name not in attrs]
    if missing:
        raise RuntimeError(
            f"{instance}: explicit dataflow identity is missing {', '.join(missing)}"
        )

    def integers(name):
        return [int(value) for value in attrs[name].value.split(",") if value]

    def optional(name):
        return attrs[name].value if name in attrs else None

    trace = optional("df.selected_branch_trace")
    axes = optional("df.pid_generalized_axes")
    return {
        "kernel": attrs["df.kernel_name"].value,
        "pid": integers("df.pid"),
        "mapping": integers("df.mapping"),
        "parent_region": optional("df.parent_region"),
        "specialization_suffix": optional("df.specialization_suffix"),
        "predicate_tag": optional("df.predicate_tag"),
        "selected_branch_trace": ast.literal_eval(trace) if trace else [],
        "pid_generalization_policy": optional("df.pid_generalization_policy"),
        "pid_generalized_axes": (
            [int(value) for value in axes.split(",") if value] if axes else []
        ),
    }


def _stream_access(func, arg_index, direction):
    """Return the realized operation and blocking contract for one link port."""
    names = {
        use.owner.operation.name.rsplit(".", 1)[-1]
        for use in func.arguments[arg_index].uses
    }
    reads = any(name.endswith("get") for name in names)
    writes = any(name.endswith("put") for name in names)
    if reads and writes:
        operation = "get_put"
    elif reads:
        operation = "get"
    elif writes:
        operation = "put"
    else:
        operation = {"in": "get", "out": "put"}.get(direction, "get_put")
    return operation, not any("try_" in name for name in names)


def _stream_ports(func):
    """The kernel's stream interface: ``(arg_index, ordinal, direction, type)``.

    ``stypes`` is stamped per argument by ``move_stream_to_interface``: ``i``
    read, ``o`` written, ``_`` not a link. The ordinal counts only link
    arguments, because that is the interface the flow zips against the RTL's
    handshake bundles -- an ``m_axi`` memory argument is not one of them.
    """
    stypes = func.attributes["stypes"].value if "stypes" in func.attributes else ""
    ports, ordinal = [], 0
    for index, arg_type in enumerate(func.type.inputs):
        char = stypes[index] if index < len(stypes) else "_"
        if char not in _STYPE_DIRECTION:
            continue
        ports.append((index, ordinal, _STYPE_DIRECTION[char], str(arg_type)))
        ordinal += 1
    return ports


def _pre_hls_evidence(func, ports, identity):
    """Build the implementation contract used to propose macro candidates."""
    body_hash = _sha256(canonical_pre_hls_ir(str(func)))
    interface = sorted(
        (
            {
                "direction": port["direction"],
                "operation": port["operation"],
                "blocking": port["blocking"],
                "type": port["type"],
            }
            for port in ports
        ),
        key=lambda item: json.dumps(item, sort_keys=True),
    )
    contract = {
        "schema_version": 1,
        "canonical_specialized_mlir_hash": body_hash,
        "stream_interface": interface,
        "pid_generalization_policy": identity["pid_generalization_policy"],
        "pid_generalized_axes": identity["pid_generalized_axes"],
    }
    return {
        "pre_hls_equivalence_hash": body_hash,
        "interface_hash": _sha256(json.dumps(interface, sort_keys=True)),
        "pre_hls_implementation_contract": contract,
        "pre_hls_implementation_contract_hash": _sha256(
            json.dumps(contract, sort_keys=True)
        ),
    }


def _memref_args(func):
    """Argument indices of the kernel's non-link (memory) arguments."""
    stypes = func.attributes["stypes"].value if "stypes" in func.attributes else ""
    return [
        index
        for index in range(len(func.type.inputs))
        if (stypes[index] if index < len(stypes) else "_") not in _STYPE_DIRECTION
    ]


def _memref_shape_and_bits(text):
    """``memref<4096xi8>`` -> ``([4096], 8)``; ``memref<56xi64>`` -> ``([56], 64)``."""
    match = re.fullmatch(r"memref<([0-9x]*)([a-z]+)(\d+)>", text)
    if match is None:
        return None, None
    dims = [int(d) for d in match.group(1).split("x") if d]
    return dims, int(match.group(3))


# ---------------------------------------------------------------------------
# The pre-HLS manifest
# ---------------------------------------------------------------------------


def collect(
    module,
    top_func_name,
    top_arguments=None,
    mappings=None,
    backend="vitis",
    clock_period_ns=None,
):
    """Build the ``pre_hls`` manifest from a realized dataflow module.

    ``module`` is the MLIR module after ``allo.dataflow.customize``: one
    ``func.func`` per kernel instance, a top function whose body constructs
    every stream and then calls each instance with the streams it is wired to.
    That body *is* the netlist, so the manifest is a transcription of it.
    """
    top_func = _find_func(module, top_func_name)
    if top_func is None:
        raise RuntimeError(f"top function {top_func_name!r} is not in the module")

    # The links, in declaration order, keyed by the SSA value each one defines.
    link_names, link_types, link_values = [], {}, []
    for op in top_func.entry_block.operations:
        if op.operation.name in _LINK_OPS:
            name = op.attributes["name"].value
            link_names.append(name)
            link_types[name] = str(op.result.type)
            link_values.append((op.result, name))

    def link_of(value):
        for other, name in link_values:
            if other == value:
                return name
        return None

    block_args = list(top_func.arguments)

    def top_arg_of(value):
        for index, arg in enumerate(block_args):
            if arg == value:
                return index
        return None

    pe_instances = []
    channels = {name: [] for name in link_names}
    memory_owner = {}
    for op in top_func.entry_block.operations:
        if op.operation.name != "func.call":
            continue
        instance = str(op.attributes["callee"]).lstrip("@")
        func = _find_func(module, instance)
        if func is None or "df.kernel" not in func.attributes:
            continue
        identity = _instance_identity(func, instance, mappings)
        pe_id = semantic_id(top_func_name, identity["kernel"], identity["pid"])
        ports = []
        for arg_index, ordinal, direction, type_text in _stream_ports(func):
            stream = link_of(op.operands[arg_index])
            operation, blocking = _stream_access(func, arg_index, direction)
            ports.append(
                {
                    "ordinal": ordinal,
                    "channel_id": stream,
                    "stream": stream,
                    "direction": direction,
                    "operation": operation,
                    "blocking": blocking,
                    "type": type_text,
                    "argument_index": arg_index,
                    "symbol_bindings": {},
                    "desired_compass_direction": "unassigned",
                }
            )
            if stream is not None:
                channels[stream].append(
                    {
                        "pe": pe_id,
                        "direction": direction,
                        "role": "producer" if direction == "out" else "consumer",
                        "accesses": [{"port_ordinal": ordinal, "operation": operation}],
                    }
                )
        for arg_index in _memref_args(func):
            ordinal = top_arg_of(op.operands[arg_index])
            if ordinal is not None:
                memory_owner[ordinal] = (pe_id, instance)
        pe_instances.append(
            {
                "semantic_id": pe_id,
                **identity,
                "rtl_instance": instance,
                "ports": ports,
                **_pre_hls_evidence(func, ports, identity),
            }
        )

    channel_records = [
        {
            "channel_id": name,
            "stream": name,
            "type": link_types[name],
            "endpoints": channels[name],
        }
        for name in link_names
    ]

    arguments = list(top_arguments or [])
    memories = []
    for ordinal, argument in enumerate(arguments):
        owner, instance = memory_owner.get(ordinal, (None, None))
        shape, element_bits = None, None
        if ordinal < len(block_args):
            shape, element_bits = _memref_shape_and_bits(str(block_args[ordinal].type))
        # `postprocess_hls_code` pragmas argument i onto `bundle=gmem{i}`, and
        # Vitis names the adapter it generates for that bundle after it.
        memories.append(
            {
                "ordinal": ordinal,
                "name": argument.get("name"),
                "direction": argument.get("direction"),
                "shape": shape if shape is not None else argument.get("shape"),
                "element_bits": element_bits,
                "bundle": f"gmem{ordinal}" if backend == "vitis" else None,
                "rtl_adapter_module": (
                    f"{top_func_name}_gmem{ordinal}_m_axi"
                    if backend == "vitis"
                    else None
                ),
                "pe": owner,
                "rtl_instance": instance,
            }
        )

    kernels = sorted({pe["kernel"] for pe in pe_instances})
    candidates = {}
    for pe in pe_instances:
        digest = pe["pre_hls_implementation_contract_hash"]
        candidates.setdefault(digest, []).append(pe["semantic_id"])
    candidate_groups = [
        {
            "candidate_class_id": f"pre_hls_candidate_{digest[:16]}",
            "members": members,
            "member_count": len(members),
            "evidence": {"pre_hls_implementation_contract_hash": digest},
            "status": "awaiting_emitted_hls_contract",
        }
        for digest, members in sorted(candidates.items())
    ]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "stage": "pre_hls",
        "backend": backend,
        "top": top_func_name,
        "producer": "allo.backend.asic_manifest",
        "top_arguments": arguments,
        "top_interface": top_interface(backend, clock_period_ns),
        "pe_instances": pe_instances,
        "channels": channel_records,
        "memories": memories,
        "kernel_grids": {kernel: _grid(pe_instances, kernel) for kernel in kernels},
        "macro_groups": [],
        "candidate_groups": candidate_groups,
        "capture": {
            "point": "after_stream_interface_expansion",
            "orientation_status": (
                "logical_graph_captured_physical_assignment_pending"
            ),
        },
        "summary": {
            "unmatched_or_ambiguous": 0,
            "unjoined_post_hls_records": 0,
            "kernels": len(kernels),
            "pe_instances": len(pe_instances),
            "channels": len(channel_records),
            # A stream ARRAY declares an element per grid point, and a chain
            # leaves its terminal elements unwired -- TinyTPU's last column
            # forwards nothing east. Those are real and are kept, because the
            # count moving is how a wiring change shows up.
            "unconnected_channels": sum(
                1 for channel in channel_records if not channel["endpoints"]
            ),
            "memories": len(memories),
            "candidate_classes": len(candidate_groups),
        },
    }
    _check(manifest)
    return manifest


def _grid(pe_instances, kernel):
    """The extent of one kernel's ``mapping=``, recovered from its instances."""
    pids = [pe["pid"] for pe in pe_instances if pe["kernel"] == kernel]
    rank = max(len(pid) for pid in pids)
    return [max(pid[axis] for pid in pids) + 1 for axis in range(rank)]


def top_interface(backend, clock_period_ns=None):
    """The control contract the backend gives the top module."""
    if backend == "vitis":
        interface = {
            "protocol": "vitis_ap_ctrl",
            "clock": {"name": "ap_clk", "edge": "rising"},
            "reset": {
                "name": "ap_rst_n",
                "polarity": "active_low",
                "default_asserted_cycles": 2,
            },
            "completion": {"kind": "ap_done", "port": "ap_done", "active_level": 1},
        }
    elif backend == "systemc":
        interface = {
            "protocol": "systemc_connections",
            "clock": {"name": "clk", "edge": "rising"},
            "reset": {
                "name": "rst",
                "polarity": "active_low",
                "default_asserted_cycles": 2,
            },
            "completion": {"kind": "top_done", "port": "done", "active_level": 1},
        }
    else:
        interface = {
            "protocol": "catapult_direct_array",
            "clock": {"name": "clk", "edge": "rising"},
            "reset": {
                "name": "rst",
                "polarity": "active_high",
                "default_asserted_cycles": 2,
            },
            "completion": {"kind": "per_argument_triosy", "active_level": 1},
        }
    if clock_period_ns:
        interface["clock"]["period_ns"] = float(clock_period_ns)
    return interface


def _check(manifest):
    """The invariants the flow's planners assume but do not all state.

    Every one of these is a `KeyError`, a `StopIteration` or a silent
    mis-placement several nodes downstream if it is violated, so it is checked
    where the manifest is written rather than where it is read.
    """
    ids = [pe["semantic_id"] for pe in manifest["pe_instances"]]
    duplicates = {name for name in ids if ids.count(name) > 1}
    if duplicates:
        raise RuntimeError(f"duplicate PE semantic_id: {sorted(duplicates)}")
    known = set(ids)
    for pe in manifest["pe_instances"]:
        ordinals = [port["ordinal"] for port in pe["ports"]]
        if ordinals != list(range(len(ordinals))):
            raise RuntimeError(
                f"{pe['semantic_id']}: stream port ordinals must be dense and "
                f"start at 0, got {ordinals}"
            )
    for channel in manifest["channels"]:
        roles = [endpoint["role"] for endpoint in channel["endpoints"]]
        # Allo allows one writer and one reader per stream, and the flow's
        # placer takes the first non-self endpoint as *the* peer -- a third
        # endpoint would be silently dropped rather than rejected.
        if roles.count("producer") > 1 or roles.count("consumer") > 1:
            raise RuntimeError(
                f"channel {channel['channel_id']} has endpoints {roles}; a "
                "stream has at most one writer and one reader"
            )
        if roles and sorted(roles) != ["consumer", "producer"]:
            raise RuntimeError(
                f"channel {channel['channel_id']} is half-wired: {roles}"
            )
        for endpoint in channel["endpoints"]:
            if endpoint["pe"] not in known:
                raise RuntimeError(
                    f"channel {channel['channel_id']} names unknown PE "
                    f"{endpoint['pe']}"
                )
            for access in endpoint["accesses"]:
                pe = next(
                    item
                    for item in manifest["pe_instances"]
                    if item["semantic_id"] == endpoint["pe"]
                )
                if not any(
                    port["ordinal"] == access["port_ordinal"] for port in pe["ports"]
                ):
                    raise RuntimeError(
                        f"channel {channel['channel_id']} names ordinal "
                        f"{access['port_ordinal']} that {endpoint['pe']} "
                        f"does not have"
                    )


# ---------------------------------------------------------------------------
# Joining compiler identity to emitted HLS and RTL
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CFunction:
    name: str
    parameters: tuple
    body: str
    loop_labels: tuple


def _match_delimiter(text, start, opening, closing):
    """Find a matching C/C++ delimiter without counting comments or strings."""
    depth, state, quote, index = 0, "code", "", start
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if state == "line_comment":
            if char == "\n":
                state = "code"
        elif state == "block_comment":
            if char == "*" and following == "/":
                state = "code"
                index += 1
        elif state == "string":
            if char == "\\":
                index += 1
            elif char == quote:
                state = "code"
        elif char == "/" and following == "/":
            state = "line_comment"
            index += 1
        elif char == "/" and following == "*":
            state = "block_comment"
            index += 1
        elif char in {'"', "'"}:
            state, quote = "string", char
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise ValueError(f"unmatched {opening!r} at byte {start}")


def _split_top_level(text, delimiter):
    parts, start = [], 0
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
    return parts + ([tail] if tail else [])


def parse_cpp_functions(text):
    """Parse generated HLS function definitions without requiring a C++ parser."""
    header = re.compile(
        r"(?m)^[ \t]*(?:[\w:<>,*&\[\] ]+\s+)?(?P<name>[A-Za-z_]\w*)\s*\("
    )
    functions = []
    for match in header.finditer(text):
        name = match.group("name")
        if name in {"if", "for", "while", "switch", "catch"}:
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
        parameters = []
        for declaration in _split_top_level(text[open_paren + 1 : close_paren], ","):
            identifiers = re.findall(r"[A-Za-z_]\w*", declaration)
            if not identifiers or declaration.strip() == "void":
                continue
            parameter = identifiers[-1]
            parameter_type = re.sub(
                rf"\b{re.escape(parameter)}\b(?=\s*(?:\[[^]]*\]\s*)*$)",
                "",
                declaration.strip(),
            )
            parameters.append((parameter, " ".join(parameter_type.split())))
        body = text[brace + 1 : close_brace]
        labels = re.findall(r"(?m)^[ \t]*([A-Za-z_]\w*)\s*:\s*for\s*\(", body)
        functions.append(_CFunction(name, tuple(parameters), body, tuple(labels)))
    return functions


_CPP_FIXED = set(
    """alignas alignof and asm auto bool break case catch char class const
    constexpr continue default delete do double else enum explicit extern false
    float for friend goto if inline int long namespace new noexcept not nullptr
    operator or private protected public register return short signed sizeof
    static struct switch template this throw true try typedef typename union
    unsigned using virtual void volatile while read write read_nb write_nb empty
    full size hls stream ap_int ap_uint int8_t int16_t int32_t int64_t uint8_t
    uint16_t uint32_t uint64_t""".split()
)
_CPP_TOKENS = re.compile(
    r'"(?:\\.|[^"\\])*"|'
    r"'(?:[sS]?[bBoOdDhH])?[0-9A-Fa-f_xXzZ?]+|"
    r"'(?:\\.|[^'\\])*'|"
    r"(?:0[xX][0-9A-Fa-f]+|\d+)'[sS]?[bBoOdDhH][0-9A-Fa-f_xXzZ?]+|"
    r"\d+(?:\.\d+)?|[A-Za-z_$][\w$]*|==|!=|<=|>=|<<|>>|&&|\|\||"
    r"::|->|\+\+|--|\S"
)


def canonical_hls_serialization(function):
    """Return the exact emitted-HLS implementation input consumed by SHA-256."""
    body = re.sub(r"//[^\n]*|/\*.*?\*/", " ", function.body, flags=re.DOTALL)
    body = re.sub(r"(?m)^\s*[A-Za-z_]\w*\s*:\s*(?=for\s*\()", "", body)
    identifiers, tokens = {}, []
    for token in _CPP_TOKENS.findall(body):
        if re.fullmatch(r"[A-Za-z_$][\w$]*", token) and token not in _CPP_FIXED:
            identifiers.setdefault(token, f"id{len(identifiers)}")
            token = identifiers[token]
        tokens.append(token)
    ports = []
    for name, parameter_type in function.parameters:
        reads = re.search(rf"\b{re.escape(name)}\s*\.\s*(?:read|read_nb|empty)\b", body)
        writes = re.search(
            rf"\b{re.escape(name)}\s*\.\s*(?:write|write_nb|full)\b", body
        )
        direction = (
            "inout"
            if reads and writes
            else "in" if reads else "out" if writes else "other"
        )
        ports.append({"type": parameter_type, "direction": direction})
    ports.sort(key=lambda item: (item["direction"], item["type"]))
    return json.dumps({"ports": ports, "tokens": tokens}, sort_keys=True), {
        "ports": ports
    }


def canonical_hls_fingerprint(function):
    serialized, interface = canonical_hls_serialization(function)
    return _sha256(serialized), interface


def _synthesis_view(text):
    """Select ``__SYNTHESIS__`` branches before parsing emitted SystemC."""
    output, stack = [], []
    directive = re.compile(r"^\s*#\s*(ifdef|ifndef|if|else|endif)\b(.*)$")
    for line in text.splitlines(keepends=True):
        match = directive.match(line)
        if match:
            kind, argument = match.group(1), match.group(2).strip()
            if kind in {"ifdef", "ifndef", "if"}:
                resolved = "__SYNTHESIS__" in argument
                selected = not resolved or (
                    kind != "ifndef" and not (kind == "if" and argument.startswith("!"))
                )
                stack.append([resolved, selected])
                if resolved:
                    continue
            elif kind == "else" and stack and stack[-1][0]:
                stack[-1][1] = not stack[-1][1]
                continue
            elif kind == "endif" and stack:
                resolved, _ = stack.pop()
                if resolved:
                    continue
        if all(selected for resolved, selected in stack if resolved):
            output.append(line)
    return "".join(output)


def parse_systemc_modules(text):
    """Return one fingerprintable pseudo-function per generated ``SC_MODULE``."""
    text = _synthesis_view(text)
    modules = {}
    header = re.compile(r"(?m)^\s*SC_MODULE\s*\(\s*([A-Za-z_]\w*)\s*\)\s*\{")
    for match in header.finditer(text):
        opening = text.find("{", match.start())
        closing = _match_delimiter(text, opening, "{", "}")
        body = text[opening + 1 : closing]
        functions = {function.name: function for function in parse_cpp_functions(body)}
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
        name = match.group(1)
        modules[name] = _CFunction(name, tuple(ports), run.body, run.loop_labels)
    return modules


_RTL_KEYWORDS = set(
    """always always_comb always_ff always_latch and assign automatic begin buf
    case casex casez default else end endcase endfunction endgenerate endmodule
    endtask for force forever fork function generate genvar if initial inout input
    integer localparam logic module nand negedge nor not or output parameter
    posedge reg release repeat signed supply0 supply1 task tri unsigned wait wand
    wire wor xnor xor""".split()
)
_RTL_TOKENS = re.compile(
    r'"(?:\\.|[^"\\])*"|'
    r"(?:\d+)'[sS]?[bBoOdDhH][0-9A-Fa-f_xXzZ?]+|"
    r"\d+(?:\.\d+)?|[A-Za-z_$][\w$]*|===|!==|==|!=|<=|>=|<<|>>|&&|\|\||"
    r"::|->|\+\+|--|\S"
)
_RTL_DECL = re.compile(
    r"^\s*(input|output|inout|wire|reg|logic|integer|parameter|localparam)\b"
)
_RTL_ALIAS = re.compile(
    r"^\s*assign\s+([A-Za-z_$][\w$]*)\s*=\s*([A-Za-z_$][\w$]*)\s*;\s*$"
)


def canonical_rtl_serialization(text):
    """Normalize generated RTL while retaining widths, behavior, and topology."""
    text = re.sub(r"//[^\n]*|/\*.*?\*/", " ", text, flags=re.DOTALL)
    own_modules = set(re.findall(r"\bmodule\s+([A-Za-z_$][\w$]*)", text))
    instance_types = set(
        re.findall(
            r"(?:^|;)\s*([A-Za-z_$][\w$]*)\s+(?:#\s*\(.*?\)\s*)?"
            r"[A-Za-z_$][\w$]*\s*\(",
            text,
            flags=re.DOTALL,
        )
    )
    fixed = _RTL_KEYWORDS | (instance_types - own_modules - _RTL_KEYWORDS)
    units, start, depth = [], 0, 0
    for token in re.finditer(r"\bbegin\b|\bend\b|;", text):
        word = token.group(0)
        if word == "begin":
            depth += 1
        elif word == "end":
            depth = max(0, depth - 1)
        elif depth == 0:
            units.append(text[start : token.end()].strip())
            start = token.end()
    if text[start:].strip():
        units.append(text[start:].strip())

    def declared_names(unit):
        if not _RTL_DECL.match(unit):
            return []
        names = re.findall(r"\b([A-Za-z_$][\w$]*)\b\s*(?=\[|,|=|;)", unit)
        return [name for name in names if name not in _RTL_KEYWORDS]

    declaration_kinds = {
        name: _RTL_DECL.match(unit).group(1)
        for unit in units
        if _RTL_DECL.match(unit)
        for name in declared_names(unit)
    }
    aliases = {
        match.group(1): match.group(2)
        for unit in units
        if (match := _RTL_ALIAS.match(unit))
        and declaration_kinds.get(match.group(1)) == "wire"
    }

    def root_alias(name):
        seen = set()
        while name in aliases and name not in seen:
            seen.add(name)
            name = aliases[name]
        return name

    names, port_index, local_index = {}, 0, 0
    for unit in units:
        declaration = _RTL_DECL.match(unit)
        for name in declared_names(unit):
            name = root_alias(name)
            if name in names or name in fixed:
                continue
            if declaration.group(1) in {"input", "output", "inout"}:
                names[name] = f"port{port_index}"
                port_index += 1
            else:
                names[name] = f"net{local_index}"
                local_index += 1
    names.update({module: "MODULE" for module in own_modules})

    def normalize(unit):
        nonlocal local_index
        result = []
        for token in _RTL_TOKENS.findall(unit):
            if re.fullmatch(r"[A-Za-z_$][\w$]*", token) and token not in fixed:
                token = root_alias(token)
                if token not in names:
                    names[token] = f"net{local_index}"
                    local_index += 1
                token = names[token]
            result.append(token)
        return " ".join(result)

    buckets = {
        name: []
        for name in (
            "inputs",
            "outputs",
            "inouts",
            "declarations",
            "constants",
            "combinational",
            "sequential",
            "instances",
        )
    }
    for unit in units:
        alias = _RTL_ALIAS.match(unit)
        if alias and alias.group(1) in aliases:
            continue
        declaration = _RTL_DECL.match(unit)
        if declaration:
            declared = declared_names(unit)
            if declared and all(name in aliases for name in declared):
                continue
            kind = declaration.group(1)
            bucket = {
                "input": "inputs",
                "output": "outputs",
                "inout": "inouts",
                "parameter": "constants",
                "localparam": "constants",
            }.get(kind, "declarations")
        elif re.match(r"^\s*assign\b", unit):
            bucket = "combinational"
        elif re.search(r"\b(?:always|initial)\b", unit):
            bucket = "sequential"
        elif re.search(
            r"[A-Za-z_$][\w$]*\s*(?:#\s*\(.*\)\s*)?" r"[A-Za-z_$][\w$]*\s*\(",
            unit,
            re.DOTALL,
        ):
            bucket = "instances"
        else:
            bucket = "sequential"
        buckets[bucket].append(normalize(unit))
    for bucket in ("combinational", "instances"):
        buckets[bucket].sort()
    return "\n".join(f"{name}:{'|'.join(values)}" for name, values in buckets.items())


def canonical_rtl_fingerprint(text):
    return _sha256(canonical_rtl_serialization(text))


def parse_vitis_log(text):
    """Extract the inlining, legalization, and process evidence Vitis reports."""
    processes = []
    for block in re.findall(
        r"detected/extracted \d+ process function\(s\):(.*?)"
        r"(?=\n(?:INFO|WARNING|ERROR):|\Z)",
        text,
        re.DOTALL,
    ):
        processes.extend(re.findall(r"'([^']+)'", block))
    return {
        "inline": dict(re.findall(r"Inlining function '([^']+)' into '([^']+)'", text)),
        "legalizations": dict(
            re.findall(r"Legalizing function name '([^']+)' to '([^']+)'", text)
        ),
        "generated_modules": list(
            dict.fromkeys(re.findall(r"Generating RTL for module '([^']+)'", text))
        ),
        "dataflow_processes": list(dict.fromkeys(processes)),
    }


def parse_vitis_synthesis_contract(text):
    version = re.search(r"Vitis HLS.*?\bv([0-9][^\s(]*)", text)
    part = re.search(r"(?:Running:\s+)?set_part\s+\{?([^\s}]+)", text)
    clock = re.search(r"(?:Running:\s+)?create_clock\s+-period\s+([0-9.]+)", text)
    return {
        "backend": "vitis_hls",
        "tool_version": version.group(1) if version else "unknown",
        "target_part": part.group(1) if part else "unknown",
        "clock_period_ns": float(clock.group(1)) if clock else None,
        "hierarchy_policy": "vitis_dataflow_process",
    }


def parse_systemc_synthesis_contract(project):
    """Capture the Catapult settings that realized a SystemC design."""
    project = Path(project)
    run_tcl = project / "run.tcl"
    text = run_tcl.read_text(encoding="utf-8", errors="replace") if run_tcl.is_file() else ""
    clock = re.search(r"-CLOCK_PERIOD\s+([0-9]+(?:\.[0-9]+)?)", text)
    library = re.search(r"solution\s+library\s+add\s+(\S+)", text)
    logs = sorted(project.glob("build/**/catapult.log"))
    version = None
    if logs:
        match = re.search(
            r"Catapult Ultra Synthesis\s+([^\s]+)",
            logs[-1].read_text(encoding="utf-8", errors="replace"),
        )
        version = match.group(1) if match else None
    return {
        "backend": "systemc",
        "frontend": "allo_systemc_connections",
        "tool_version": version or "unknown",
        "clock_period_ns": float(clock.group(1)) if clock else None,
        "technology_library": library.group(1) if library else "unknown",
        "hierarchy_policy": "explicit_blocks",
    }


def _verilog_width(declaration):
    match = re.search(r"\[(\d+)\s*:\s*(\d+)\]", declaration)
    return abs(int(match.group(1)) - int(match.group(2))) + 1 if match else 1


def _semantic_type_width(type_name):
    match = re.search(r"(\d+)$", type_name or "")
    return int(match.group(1)) if match else None


def _systemc_top_arguments(top_rtl, semantic_args):
    """Match semantic arrays to Catapult's realized SystemC memory-pin families."""
    declarations = {}
    declaration_re = re.compile(
        r"\b(input|output|inout)\s*(?:wire\s+|reg\s+)?"
        r"(?:\[[^]]+\]\s*)?([A-Za-z_]\w*)\s*;"
    )
    for match in declaration_re.finditer(top_rtl):
        declarations[match.group(2)] = {
            "direction": match.group(1),
            "width": _verilog_width(match.group(0)),
        }

    families = {}
    for name, info in declarations.items():
        match = re.fullmatch(r"(v\d+)_(radr|re|q|rrdy|wadr|d|we|wrdy)", name)
        if match:
            families.setdefault(match.group(1), {})[match.group(2)] = {
                "name": name,
                **info,
            }
    ordered = sorted(families, key=lambda name: int(name[1:]))
    if len(ordered) != len(semantic_args):
        raise RuntimeError(
            "SystemC top argument count does not match realized RTL memory "
            f"families: semantic={len(semantic_args)}, rtl={len(ordered)}"
        )

    results = []
    for ordinal, (root, semantic) in enumerate(zip(ordered, semantic_args)):
        ports = families[root]
        direction = semantic.get("direction", "unknown")
        if direction == "input":
            suffix_roles = {
                "radr": "read_address",
                "re": "read_enable",
                "q": "read_data",
                "rrdy": "read_ready",
            }
            protocol, data_role, latency = (
                "systemc_sync_memory_read",
                "read_data",
                1,
            )
        elif direction == "output":
            suffix_roles = {
                "wadr": "write_address",
                "d": "write_data",
                "we": "write_enable",
                "wrdy": "write_ready",
            }
            protocol, data_role, latency = (
                "systemc_sync_memory_write",
                "write_data",
                None,
            )
        else:
            raise RuntimeError(
                f"unsupported SystemC top argument direction {direction!r} "
                f"for {semantic.get('name', root)}"
            )
        if set(ports) != set(suffix_roles):
            raise RuntimeError(
                f"SystemC argument {semantic.get('name', root)!r} realized as "
                f"{root} has ports {sorted(ports)}; expected {sorted(suffix_roles)}"
            )
        roles = {role: ports[suffix] for suffix, role in suffix_roles.items()}
        shape = semantic.get("shape", [])
        element_count = math.prod(shape) if shape else 1
        element_bits = _semantic_type_width(semantic.get("type", ""))
        address_width = roles[
            "read_address" if direction == "input" else "write_address"
        ]["width"]
        data_width = roles[data_role]["width"]
        if element_bits != data_width:
            raise RuntimeError(
                f"SystemC argument {semantic.get('name', root)!r} element width "
                f"{element_bits} does not match realized RTL width {data_width}"
            )
        results.append(
            {
                **semantic,
                "ordinal": ordinal,
                "name": semantic.get("name", f"arg{ordinal}"),
                "catapult_argument": root,
                "semantic_direction": direction,
                "rtl_direction": direction,
                "interface_protocol": protocol,
                "interface": {
                    "roles": roles,
                    "resource_root": root,
                    "matching": "exact_systemc_memory_pin_family",
                    "layout": "row_major",
                    "element_bits": element_bits,
                    "element_count": element_count,
                    "address_width": address_width,
                    "address_capacity": 1 << address_width,
                    "data_width": data_width,
                    "read_latency_cycles": latency,
                },
                "data_ports": list(ports.values()),
                "triosy_ports": [],
                "rtl_ports": list(ports.values()),
                "packing": None,
            }
        )
    return results


def _rtl_artifacts(rtl_dir):
    """Parse each RTL module once and construct its definition graph."""
    blocks, sources = {}, {}
    module_re = re.compile(r"(?ms)^\s*module\s+([A-Za-z_][\w$]*)\b.*?\bendmodule\b")
    for path in sorted(Path(rtl_dir).iterdir()):
        if path.suffix not in {".v", ".sv"} or path.name.endswith("_stub.v"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in module_re.finditer(text):
            blocks.setdefault(match.group(1), match.group(0))
            sources.setdefault(match.group(1), str(path))
    known = set(blocks)
    instance_re = re.compile(
        r"(?m)(?:^|;)\s*([A-Za-z_][\w$]*)"
        r"(?:\s*#\s*\([^;]*?\))?\s+([A-Za-z_][\w$]*)\s*\(",
        re.DOTALL,
    )
    instances = [
        {
            "parent_module": parent,
            "module": child,
            "instance_name": instance,
            "source_artifact": sources[parent],
        }
        for parent, text in blocks.items()
        for child, instance in instance_re.findall(text)
        if child in known
    ]
    children = {}
    for instance in instances:
        children.setdefault(instance["parent_module"], []).append(instance)
    instantiated = {instance["module"] for instance in instances}
    roots = sorted(known - instantiated)
    realized = []

    def expand(parent, path, ancestors):
        for instance in children.get(parent, []):
            instance_path = f"{path}/{instance['instance_name']}"
            realized.append({**instance, "instance_path": instance_path})
            child = instance["module"]
            if child not in ancestors:
                expand(child, instance_path, (*ancestors, child))

    for root in roots:
        expand(root, root, (root,))
    hierarchy = {
        "schema_version": 1,
        "roots": roots,
        "module_definitions": [
            {
                "name": name,
                "source_artifact": sources[name],
                "direct_dependencies": sorted(
                    {item["module"] for item in children.get(name, [])}
                ),
            }
            for name in sorted(blocks)
        ],
        "module_instances": instances,
        "realized_instances": realized,
    }
    return blocks, sources, hierarchy


def rtl_dependency_closure(root, hierarchy):
    dependencies = {
        item["name"]: item.get("direct_dependencies", [])
        for item in hierarchy.get("module_definitions", [])
    }
    result, pending = [], [root]
    while pending:
        module = pending.pop()
        if module in result or module not in dependencies:
            continue
        result.append(module)
        pending.extend(reversed(dependencies[module]))
    return result


def _normalized_hls_name(name):
    return name[: -len("_fixed")] if name.endswith("_fixed") else name


def _loop_processes(function, generated):
    return [
        process
        for process in generated
        if "Pipeline_" in process
        and any(
            re.search(rf"(?:^|_){re.escape(label)}(?:_|$)", process)
            for label in function.loop_labels
        )
    ]


def _clone_ordinal(process, function):
    if process == function:
        return 0
    match = re.fullmatch(re.escape(function) + r"[._](\d+)", process)
    return int(match.group(1)) if match else None


def _direct_processes(function, log_info):
    for source in (
        log_info["dataflow_processes"],
        log_info["legalizations"],
        log_info["generated_modules"],
    ):
        found = [
            (_clone_ordinal(process, function), process)
            for process in source
            if _clone_ordinal(process, function) is not None
        ]
        if found:
            return [process for _, process in sorted(found)]
    return []


def _processes_for_function(function, log_info):
    """Prefer an enclosing dataflow process over its internal pipelines.

    Vitis emits both an RTL module for a dataflow process and separate modules
    for loops pipelined inside that process. Loop labels are therefore only a
    fallback for source functions that Vitis actually inlined and omitted as
    standalone processes.
    """
    direct = _direct_processes(function.name, log_info)
    if direct:
        return "direct_or_cloned", direct
    pipelines = _loop_processes(function, log_info["generated_modules"])
    if pipelines:
        return "inlined_pipeline", pipelines
    return "direct_or_cloned", []


def _modules_for_process(process, modules):
    candidates = (process, process.replace(".", "_"))
    return sorted(
        (
            module
            for module in modules
            if any(module == name or module.endswith("_" + name) for name in candidates)
        ),
        key=len,
    )


def _attach_implementation_contract(record):
    contract = {
        "schema_version": 1,
        "pre_hls_contract_hash": record.get("pre_hls_implementation_contract_hash"),
        "emitted_hls_hash": record.get("hls_equivalence_hash"),
        "emitted_hls_interface": record.get("hls_interface_signature"),
        "hierarchy_mode": record.get("mapping_mode"),
        "synthesis": record.get("synthesis_contract", {}),
    }
    record["implementation_contract"] = contract
    record["implementation_equivalence_hash"] = _sha256(
        json.dumps(contract, sort_keys=True, separators=(",", ":"))
    )


def build_equivalence_groups(records):
    """Use the MLIR/HLS contract as authority and RTL only as an audit."""
    grouped = {}
    for record in records:
        if record.get("status") != "matched":
            continue
        _attach_implementation_contract(record)
        grouped.setdefault(record["implementation_equivalence_hash"], []).append(record)
    groups, audits = [], []
    for digest, members in sorted(grouped.items()):
        rtl_hashes = sorted(
            {
                member["rtl_equivalence_hash"]
                for member in members
                if member.get("rtl_equivalence_hash")
            }
        )
        groups.append(
            {
                "macro_class_id": f"macro_hls_{digest[:16]}",
                "representative": members[0]["semantic_id"],
                "members": [
                    {
                        "semantic_id": member["semantic_id"],
                        "rtl_module": member["rtl_modules"][0]["name"],
                        "orientation": "unassigned",
                    }
                    for member in members
                ],
                "member_count": len(members),
                "proof": {
                    "status": "proven",
                    "method": "specialized_mlir_emitted_hls_contract",
                    "implementation_contract_hash": digest,
                    "scope": "representative implementation substitution",
                    "contract": members[0]["implementation_contract"],
                },
                "rtl_audit": {
                    "authority": False,
                    "status": (
                        "agree" if len(rtl_hashes) <= 1 else "generated_rtl_diverged"
                    ),
                    "distinct_hashes": rtl_hashes,
                },
                "orientation_policy": {
                    "direction_in_fingerprint": False,
                    "allowed_transform_family": "D4",
                    "selected_per_member": False,
                },
            }
        )
        if len(rtl_hashes) > 1:
            audits.append(
                {
                    "audit_id": f"rtl_divergence_{digest[:16]}",
                    "members": [member["semantic_id"] for member in members],
                    "implementation_contract_hash": digest,
                    "rtl_hashes": rtl_hashes,
                    "status": "diagnostic_only_representative_will_be_used",
                }
            )
    return groups, audits


def enrich(
    pre, rtl_dir, hls_source, synthesis_log, synthesis_tcl=None, published_path=None
):
    """Join compiler PEs to emitted HLS functions, Vitis processes, and RTL."""
    hls_text = Path(hls_source).read_text(encoding="utf-8", errors="replace")
    log_text = Path(synthesis_log).read_text(encoding="utf-8", errors="replace")
    if synthesis_tcl and Path(synthesis_tcl).is_file():
        contract_text = (
            log_text
            + "\n"
            + Path(synthesis_tcl).read_text(encoding="utf-8", errors="replace")
        )
    else:
        contract_text = log_text
    functions = {
        _normalized_hls_name(function.name): function
        for function in parse_cpp_functions(hls_text)
    }
    log_info = parse_vitis_log(log_text)
    synthesis = parse_vitis_synthesis_contract(contract_text)
    blocks, sources, hierarchy = _rtl_artifacts(rtl_dir)
    parents = {}
    for instance in hierarchy["module_instances"]:
        parents.setdefault(instance["module"], set()).add(instance["parent_module"])

    merged = copy.deepcopy(pre)
    post_records, claimed = [], set()
    for pe in merged.get("pe_instances", []):
        function_name = pe.get("rtl_instance")
        function = functions.get(_normalized_hls_name(function_name or ""))
        records = []
        if function is None:
            records.append(
                {
                    "semantic_id": pe["semantic_id"],
                    "hls_function": function_name,
                    "synthesis_contract": synthesis,
                    "status": "no_hls_function",
                }
            )
        else:
            hls_hash, interface = canonical_hls_fingerprint(function)
            mapping_mode, processes = _processes_for_function(function, log_info)
            if len(processes) > 1:
                records.append(
                    {
                        "semantic_id": pe["semantic_id"],
                        "hls_function": function.name,
                        "loop_labels": list(function.loop_labels),
                        "hls_equivalence_hash": hls_hash,
                        "hls_interface_signature": interface,
                        "mapping_mode": mapping_mode,
                        "synthesis_contract": synthesis,
                        "vitis_process_candidates": processes,
                        "status": "ambiguous_hls_process",
                    }
                )
                processes = []
            if not processes:
                empty = not re.sub(
                    r"\s|//[^\n]*|/\*.*?\*/", "", function.body, flags=re.DOTALL
                )
                if not records:
                    records.append(
                        {
                            "semantic_id": pe["semantic_id"],
                            "hls_function": function.name,
                            "loop_labels": list(function.loop_labels),
                            "hls_equivalence_hash": hls_hash,
                            "hls_interface_signature": interface,
                            "mapping_mode": mapping_mode,
                            "synthesis_contract": synthesis,
                            "status": "empty_source" if empty else "no_rtl_process",
                        }
                    )
            for process in processes:
                legalized = log_info["legalizations"].get(
                    process, process.replace(".", "_")
                )
                modules = _modules_for_process(legalized, blocks)
                root = None
                if len(modules) == 1:
                    direct_parents = sorted(parents.get(modules[0], []))
                    if mapping_mode == "inlined_pipeline":
                        root = direct_parents[0] if len(direct_parents) == 1 else None
                    else:
                        root = modules[0]
                status = (
                    "no_rtl_module"
                    if not modules
                    else "ambiguous_rtl_module" if len(modules) > 1 else "matched"
                )
                if len(modules) == 1 and root is None:
                    status = "ambiguous_rtl_root"
                if root:
                    claimed.update(rtl_dependency_closure(root, hierarchy))
                records.append(
                    {
                        "semantic_id": pe["semantic_id"],
                        "kernel": pe["kernel"],
                        "pid": pe["pid"],
                        "hls_function": function.name,
                        "loop_labels": list(function.loop_labels),
                        "hls_equivalence_hash": hls_hash,
                        "hls_interface_signature": interface,
                        "mapping_mode": mapping_mode,
                        "synthesis_contract": synthesis,
                        "vitis_parent": log_info["inline"].get(function.name),
                        "vitis_process": process,
                        "legalized_process": legalized,
                        "rtl_modules": [
                            {"name": module, "file": sources[module]}
                            for module in modules
                        ],
                        "rtl_root_module": root,
                        "rtl_dependency_modules": (
                            rtl_dependency_closure(root, hierarchy) if root else []
                        ),
                        "rtl_root_instances": [
                            item
                            for item in hierarchy["realized_instances"]
                            if item["module"] == root
                        ],
                        "rtl_equivalence_hash": (
                            canonical_rtl_fingerprint(blocks[modules[0]])
                            if len(modules) == 1
                            else None
                        ),
                        "status": status,
                    }
                )
        for record in records:
            record["pre_hls_implementation_contract"] = pe.get(
                "pre_hls_implementation_contract"
            )
            record["pre_hls_implementation_contract_hash"] = pe.get(
                "pre_hls_implementation_contract_hash"
            )
        pe["post_hls_records"] = records
        post_records.extend(records)

    macro_groups, rtl_audits = build_equivalence_groups(post_records)
    unmatched = [
        record
        for record in post_records
        if record["status"] not in {"matched", "empty_source"}
    ]
    merged.update(
        {
            "stage": "post_hls_enriched",
            "post_hls_inputs": {
                "kernel_cpp": str(hls_source),
                "vitis_log": str(synthesis_log),
                "rtl_dir": str(rtl_dir),
            },
            "post_hls_records": post_records,
            "macro_groups": macro_groups,
            "rtl_audits": rtl_audits,
            "rtl_hierarchy": hierarchy,
            "rtl_artifact": {
                "published_path": published_path or "backend-rtl",
                "module_count": len(blocks),
            },
            "support_modules": sorted(set(blocks) - claimed - {pre.get("top")}),
        }
    )
    summary = dict(merged.get("summary", {}))
    summary.update(
        {
            "post_hls_records": len(post_records),
            "matched": sum(record["status"] == "matched" for record in post_records),
            "empty_source": sum(
                record["status"] == "empty_source" for record in post_records
            ),
            "unmatched_or_ambiguous": len(unmatched),
            "unjoined_post_hls_records": len(unmatched),
            "macro_classes": len(macro_groups),
            "rtl_modules": len(blocks),
        }
    )
    if unmatched:
        summary["unmatched_pe_instances"] = sorted(
            {record["semantic_id"] for record in unmatched}
        )
    merged["summary"] = summary
    return merged


def enrich_systemc(pre, project, rtl_dir, rtl_path, hls_source):
    """Join emitted SystemC kernel modules to Catapult's self-contained RTL."""
    source = Path(hls_source).read_text(encoding="utf-8", errors="replace")
    functions = parse_systemc_modules(source)
    blocks, sources, hierarchy = _rtl_artifacts(rtl_dir)
    synthesis = parse_systemc_synthesis_contract(project)

    merged = copy.deepcopy(pre)
    post_records, claimed = [], set()
    for pe in merged.get("pe_instances", []):
        name = pe.get("rtl_instance")
        function = functions.get(name)
        status = "matched"
        if function is None:
            status = "no_systemc_module"
        elif name not in blocks:
            status = "no_rtl_module"
        closure = rtl_dependency_closure(name, hierarchy) if status == "matched" else []
        if closure:
            claimed.update(closure)
        hls_hash, interface = (
            canonical_hls_fingerprint(function) if function else (None, None)
        )
        rtl_text = "\n".join(blocks[module] for module in closure)
        record = {
            "semantic_id": pe["semantic_id"],
            "kernel": pe["kernel"],
            "pid": pe["pid"],
            "hls_function": name,
            "loop_labels": list(function.loop_labels) if function else [],
            "hls_equivalence_hash": hls_hash,
            "hls_interface_signature": interface,
            "mapping_mode": "systemc_module",
            "synthesis_contract": synthesis,
            "rtl_modules": [
                {"name": module, "file": sources[module]} for module in closure
            ],
            "rtl_root_module": name if status == "matched" else None,
            "rtl_dependency_modules": closure,
            "rtl_root_instances": [
                item
                for item in hierarchy["realized_instances"]
                if item["module"] == name
            ],
            "rtl_equivalence_hash": (
                canonical_rtl_fingerprint(rtl_text) if rtl_text else None
            ),
            "status": status,
            "pre_hls_implementation_contract": pe.get(
                "pre_hls_implementation_contract"
            ),
            "pre_hls_implementation_contract_hash": pe.get(
                "pre_hls_implementation_contract_hash"
            ),
        }
        pe["post_hls_records"] = [record]
        post_records.append(record)

    macro_groups, rtl_audits = build_equivalence_groups(post_records)
    unmatched = [record for record in post_records if record["status"] != "matched"]
    top = pre.get("top")
    top_arguments = (
        _systemc_top_arguments(blocks[top], pre.get("top_arguments", []))
        if top in blocks
        else []
    )
    top_contract = top_interface("systemc", synthesis.get("clock_period_ns"))
    top_contract.update(
        {
            "argument_protocols": sorted(
                {argument["interface_protocol"] for argument in top_arguments}
            ),
            "launch": "connections_handshake_after_reset_deassertion",
            "cycle_trace_available": False,
        }
    )
    top_contract["reset"]["evidence"] = "systemc_reset_signal_is_false"
    merged.update(
        {
            "stage": "post_hls_enriched",
            "backend": "systemc",
            "post_hls_inputs": {
                "kernel_cpp": str(hls_source),
                "rtl_artifact": str(rtl_path),
                "rtl_artifact_kind": "systemc_catapult_rtl",
            },
            "post_hls_records": post_records,
            "macro_groups": macro_groups,
            "rtl_audits": rtl_audits,
            "rtl_hierarchy": hierarchy,
            "rtl_artifact": {
                "path": str(rtl_path),
                "project_relative_path": str(
                    Path(rtl_path).relative_to(Path(project))
                ),
                "published_path": "backend-rtl/concat_rtl.v",
                "kind": "systemc_catapult_rtl",
                "self_contained": True,
                "module_count": len(blocks),
            },
            "top_arguments": top_arguments,
            "top_interface": top_contract,
            "support_modules": sorted(set(blocks) - claimed - {top}),
        }
    )
    summary = dict(merged.get("summary", {}))
    summary.update(
        {
            "post_hls_records": len(post_records),
            "matched": sum(record["status"] == "matched" for record in post_records),
            "unmatched_or_ambiguous": len(unmatched),
            "unjoined_post_hls_records": len(unmatched),
            "macro_classes": len(macro_groups),
            "rtl_modules": len(blocks),
        }
    )
    if unmatched:
        summary["unmatched_pe_instances"] = sorted(
            {record["semantic_id"] for record in unmatched}
        )
    merged["summary"] = summary
    return merged


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


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
    """Render JSON-compatible data with Tcl's built-in dict/list commands."""
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
        return (
            "[list \\\n"
            + " \\\n".join(f"{child}{_to_tcl(item, indent + 2)}" for item in value)
            + f"\n{pad}]"
        )
    if isinstance(value, dict):
        if not value:
            return "[dict create]"
        return (
            "[dict create \\\n"
            + " \\\n".join(
                f"{child}{_tcl_string(str(key))} {_to_tcl(item, indent + 2)}"
                for key, item in value.items()
            )
            + f"\n{pad}]"
        )
    raise TypeError(f"unsupported Tcl manifest value: {type(value).__name__}")


def to_tcl(manifest):
    """Serialize the complete manifest for Tcl consumers without Tcllib."""
    return (
        "# Generated by allo.backend.asic_manifest -- do not edit.\n"
        "# Requires only Tcl built-in dict and list commands.\n"
        f"set allo_asic_manifest {_to_tcl(manifest)}\n"
    )


def _write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def write(manifest, project, name, debug_dir=None, module=None):
    """Write ``<name>.json`` and ``<name>.tcl`` under ``project``."""
    os.makedirs(project, exist_ok=True)
    stem = name[:-5] if name.endswith(".json") else name
    _write(
        os.path.join(project, f"{stem}.json"),
        json.dumps(manifest, indent=2, sort_keys=False) + "\n",
    )
    _write(os.path.join(project, f"{stem}.tcl"), to_tcl(manifest))
    if debug_dir:
        write_debug(manifest, os.path.join(project, debug_dir), module)
    return os.path.join(project, f"{stem}.json")


def write_debug(manifest, directory, module=None):
    """Flat, greppable views of the same records, for reading by eye."""
    os.makedirs(directory, exist_ok=True)
    rows = ["semantic_id\tkernel\tpid\trtl_instance\tports"]
    for pe in manifest.get("pe_instances", []):
        rows.append(
            "\t".join(
                [
                    pe["semantic_id"],
                    pe["kernel"],
                    ",".join(str(p) for p in pe["pid"]),
                    str(pe.get("rtl_instance")),
                    str(len(pe.get("ports", []))),
                ]
            )
        )
    _write(os.path.join(directory, "pe-instances.tsv"), "\n".join(rows) + "\n")
    rows = ["channel_id\ttype\tproducer\tconsumer"]
    for channel in manifest.get("channels", []):
        ends = channel.get("endpoints", [])
        producer = next((e["pe"] for e in ends if e.get("role") == "producer"), "")
        consumer = next((e["pe"] for e in ends if e.get("role") == "consumer"), "")
        rows.append(
            "\t".join(
                [channel["channel_id"], channel.get("type", ""), producer, consumer]
            )
        )
    _write(os.path.join(directory, "channels.tsv"), "\n".join(rows) + "\n")
    _write(
        os.path.join(directory, "summary.json"),
        json.dumps(manifest.get("summary", {}), indent=2) + "\n",
    )
    if module is None:
        return

    identity = {
        pe["rtl_instance"]: {
            key: pe[key]
            for key in (
                "kernel",
                "pid",
                "mapping",
                "parent_region",
                "specialization_suffix",
                "predicate_tag",
                "selected_branch_trace",
                "pid_generalization_policy",
                "pid_generalized_axes",
            )
        }
        for pe in manifest.get("pe_instances", [])
    }
    _write(
        os.path.join(directory, "identity-map.json"),
        json.dumps(identity, indent=2) + "\n",
    )
    for instance in identity:
        func = _find_func(module, instance)
        if func is None:
            continue
        filename = re.sub(r"[^A-Za-z0-9_.-]", "_", instance)
        raw = str(func)
        for relative, text in (
            (f"functions/{filename}.mlir", raw),
            (f"canonical/pre-hls/{filename}.txt", canonical_pre_hls_ir(raw)),
        ):
            path = os.path.join(directory, relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _write(path, text + ("" if text.endswith("\n") else "\n"))


def write_post_hls_debug(manifest, directory, hls_source, synthesis_log, rtl_dir):
    """Write the exact emitted-HLS/RTL serializations consumed by hashes."""
    functions = {
        function.name: function
        for function in parse_cpp_functions(
            Path(hls_source).read_text(encoding="utf-8", errors="replace")
        )
    }
    log_info = parse_vitis_log(
        Path(synthesis_log).read_text(encoding="utf-8", errors="replace")
    )
    _write(
        os.path.join(directory, "parsed-vitis-log.json"),
        json.dumps(log_info, indent=2) + "\n",
    )
    blocks, _, _ = _rtl_artifacts(rtl_dir)
    written_hls, written_rtl = set(), set()
    for record in manifest.get("post_hls_records", []):
        function_name = record.get("hls_function")
        if function_name in functions and function_name not in written_hls:
            serialized, _ = canonical_hls_serialization(functions[function_name])
            _write(
                os.path.join(directory, "canonical", "hls", f"{function_name}.txt"),
                serialized + "\n",
            )
            written_hls.add(function_name)
        for rtl_module in record.get("rtl_modules", []):
            module_name = rtl_module["name"]
            if module_name in blocks and module_name not in written_rtl:
                _write(
                    os.path.join(directory, "canonical", "rtl", f"{module_name}.txt"),
                    canonical_rtl_serialization(blocks[module_name]) + "\n",
                )
                written_rtl.add(module_name)


# ---------------------------------------------------------------------------
# The `configs["asic_manifest"]` contract
# ---------------------------------------------------------------------------


def options(configs):
    """``configs["asic_manifest"]`` if it asks for a manifest, else ``None``."""
    requested = (configs or {}).get("asic_manifest")
    if not isinstance(requested, dict) or not requested.get("enabled", False):
        return None
    return {
        "path": requested.get("path", "asic-manifest.json"),
        "debug_artifacts": requested.get("debug_artifacts", False),
        "debug_dir": requested.get("debug_dir", "asic-debug"),
    }


def final_name(path):
    """``asic-manifest.json`` -> ``asic-manifest-final`` (the node's names)."""
    stem = path[:-5] if path.endswith(".json") else path
    return f"{stem}-final"


def rtl_directory(backend, project):
    """Where the backend left the RTL it just synthesized.

    The same layouts ``nodes/allo-asic-compilation/backend.py`` publishes from,
    so the manifest is enriched against exactly the files the flow will read.
    """
    import glob  # noqa: PLC0415

    if backend == "vitis":
        candidates = sorted(
            path
            for path in glob.glob(os.path.join(project, "*.prj", "*", "syn", "verilog"))
            if os.path.isdir(path)
        )
    elif backend == "systemc":
        rtl_files = sorted(
            path
            for path in glob.glob(
                os.path.join(project, "build", "**", "concat_rtl.v"),
                recursive=True,
            )
            if os.path.isfile(path)
        )
        if not rtl_files:
            rtl_files = sorted(
                path
                for path in glob.glob(
                    os.path.join(project, "build", "**", "rtl.v"), recursive=True
                )
                if os.path.isfile(path)
            )
        candidates = sorted({os.path.dirname(path) for path in rtl_files})
    else:
        candidates = sorted(
            {
                os.path.dirname(path)
                for name in ("concat_rtl.v", "rtl.v")
                for path in glob.glob(os.path.join(project, "**", name), recursive=True)
                if os.path.isfile(path)
            }
        )
    if len(candidates) != 1:
        raise RuntimeError(
            f"expected exactly one {backend} RTL directory under {project}, "
            f"found {len(candidates)}: {candidates}"
        )
    return candidates[0]


def _vitis_artifact_paths(project, rtl_dir):
    """Resolve the HLS source, synthesis log, and Tcl used by one Vitis run."""
    source = os.path.join(project, "kernel.cpp")
    solution = Path(rtl_dir).parents[1]
    logs = [solution / "solution1.log", Path(project) / "vitis_hls.log"]
    log = next((path for path in logs if path.is_file()), None)
    missing = []
    if not os.path.isfile(source):
        missing.append(source)
    if log is None:
        missing.append(" or ".join(str(path) for path in logs))
    if missing:
        raise RuntimeError(f"missing Vitis manifest artifacts: {', '.join(missing)}")
    synthesis_tcl = os.path.join(project, "run.tcl")
    return source, str(log), synthesis_tcl


def _systemc_artifact_paths(project, rtl_dir):
    """Resolve the emitted SystemC source and preferred self-contained RTL."""
    source = Path(project) / "kernel.cpp"
    rtl_dir = Path(rtl_dir)
    rtl = next(
        (path for name in ("concat_rtl.v", "rtl.v") if (path := rtl_dir / name).is_file()),
        None,
    )
    missing = []
    if not source.is_file():
        missing.append(str(source))
    if rtl is None:
        missing.append(f"{rtl_dir}/concat_rtl.v or {rtl_dir}/rtl.v")
    if missing:
        raise RuntimeError(f"missing SystemC manifest artifacts: {', '.join(missing)}")
    return str(source), str(rtl)


def emit_final(project, configs, backend):
    """Enrich the pre-HLS manifest with the RTL that has just been produced.

    Called by the backend once synthesis has succeeded: only then do the
    module names exist to join the architecture to. No-op unless ``configs``
    asked for a manifest and the pre-HLS one is where it was written.
    """
    requested = options(configs)
    if requested is None:
        return None
    pre_path = os.path.join(project, requested["path"])
    if not os.path.isfile(pre_path):
        raise RuntimeError(
            f"asic_manifest was requested but {pre_path} is missing; the "
            "pre-HLS manifest is written at build() time"
        )
    with open(pre_path, "r", encoding="utf-8") as handle:
        pre = json.load(handle)
    rtl_dir = rtl_directory(backend, project)
    if backend == "vitis":
        hls_source, synthesis_log, synthesis_tcl = _vitis_artifact_paths(
            project, rtl_dir
        )
        final = enrich(
            pre,
            rtl_dir,
            hls_source,
            synthesis_log,
            synthesis_tcl=synthesis_tcl,
        )
    elif backend == "systemc":
        hls_source, rtl_path = _systemc_artifact_paths(project, rtl_dir)
        final = enrich_systemc(pre, project, rtl_dir, rtl_path, hls_source)
    else:
        raise RuntimeError(
            f"post-HLS ASIC manifest enrichment is not implemented for {backend!r}"
        )
    write(
        final,
        project,
        final_name(requested["path"]),
        debug_dir=requested["debug_dir"] if requested["debug_artifacts"] else None,
    )
    if requested["debug_artifacts"]:
        debug_dir = os.path.join(project, requested["debug_dir"])
        if backend == "vitis":
            write_post_hls_debug(
                final, debug_dir, hls_source, synthesis_log, rtl_dir
            )
        else:
            _write(os.path.join(debug_dir, "systemc-rtl-path.txt"), rtl_path + "\n")
    return final
