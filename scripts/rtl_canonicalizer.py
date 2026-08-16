#!/usr/bin/env python3
"""Conservative structural fingerprints for HLS-generated Verilog.

This is intentionally a small normalizer for the RTL emitted by the supported
HLS flows, not a general Verilog equivalence checker.  It removes spelling and
statement-order accidents while retaining widths, constants, operators, and
connectivity.  Unsupported behavioral text remains in the fingerprint, making
false negatives preferable to unsafe merges.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


TOKEN_RE = re.compile(
    r'"(?:\\.|[^"\\])*"|'
    r"(?:\d+)'[sS]?[bBoOdDhH][0-9A-Fa-f_xXzZ?]+|"
    r"\d+(?:\.\d+)?|[A-Za-z_$][\w$]*|===|!==|==|!=|<=|>=|<<|>>|&&|\|\||"
    r"::|->|\+\+|--|\S"
)

KEYWORDS = {
    "always", "always_comb", "always_ff", "always_latch", "and", "assign",
    "automatic", "begin", "buf", "case", "casex", "casez", "default",
    "else", "end", "endcase", "endfunction", "endgenerate", "endmodule",
    "endtask", "for", "force", "forever", "fork", "function", "generate",
    "genvar", "if", "initial", "inout", "input", "integer", "localparam",
    "logic", "module", "nand", "negedge", "nor", "not", "or", "output",
    "parameter", "posedge", "reg", "release", "repeat", "signed", "supply0",
    "supply1", "task", "tri", "unsigned", "wait", "wand", "wire", "wor",
    "xnor", "xor",
}

DECL_RE = re.compile(
    r"^\s*(input|output|inout|wire|reg|logic|integer|parameter|localparam)\b"
)
IDENT_RE = re.compile(r"^[A-Za-z_$][\w$]*$")
SIMPLE_ALIAS_RE = re.compile(
    r"^\s*assign\s+([A-Za-z_$][\w$]*)\s*=\s*([A-Za-z_$][\w$]*)\s*;\s*$"
)


@dataclass(frozen=True)
class CanonicalRTL:
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    inouts: tuple[str, ...]
    declarations: tuple[str, ...]
    constants: tuple[str, ...]
    combinational: tuple[str, ...]
    sequential: tuple[str, ...]
    instances: tuple[str, ...]

    def serialize(self) -> str:
        buckets = (
            ("inputs", self.inputs),
            ("outputs", self.outputs),
            ("inouts", self.inouts),
            ("declarations", self.declarations),
            ("constants", self.constants),
            ("combinational", self.combinational),
            ("sequential", self.sequential),
            ("instances", self.instances),
        )
        return "\n".join(f"{name}:{'|'.join(values)}" for name, values in buckets)


class RTLCanonicalizer:
    """Build a stable, connectivity-preserving view of generated RTL."""

    def __init__(self, backend: str | None = None):
        self.backend = backend

    @staticmethod
    def _strip_comments(text: str) -> str:
        return re.sub(r"//[^\n]*|/\*.*?\*/", " ", text, flags=re.DOTALL)

    def _normalize_backend_metadata(self, text: str) -> str:
        if self.backend == "catapult":
            # Resource IDs name Catapult connectivity objects but do not alter
            # their logic.  Normalize only the value of the named parameter.
            text = re.sub(
                r"(\.\s*rscid\s*\(\s*)(?:\d+'[sS]?[dDhHbBoO])?[0-9A-Fa-f_xXzZ?]+(\s*\))",
                r"\1RSCID_VALUE\2",
                text,
            )
        return text

    @staticmethod
    def _units(text: str) -> list[str]:
        """Split top-level semicolon units, keeping procedural blocks intact."""
        units, start, depth = [], 0, 0
        tokens = list(re.finditer(r"\bbegin\b|\bend\b|;", text))
        for token in tokens:
            word = token.group(0)
            if word == "begin":
                depth += 1
            elif word == "end":
                depth = max(0, depth - 1)
            elif depth == 0:
                units.append(text[start : token.end()].strip())
                start = token.end()
        tail = text[start:].strip()
        if tail:
            units.append(tail)
        return [unit for unit in units if unit]

    @staticmethod
    def _declared_names(unit: str) -> list[str]:
        if not DECL_RE.match(unit):
            return []
        # Names are identifiers followed by a comma, dimension, initializer,
        # or semicolon.  Type/range identifiers are filtered by keywords.
        names = re.findall(r"\b([A-Za-z_$][\w$]*)\b\s*(?=\[|,|=|;)", unit)
        return [name for name in names if name not in KEYWORDS]

    @staticmethod
    def _instance_types(text: str, own_modules: set[str]) -> set[str]:
        found = re.findall(
            r"(?:^|;)\s*([A-Za-z_$][\w$]*)\s+(?:#\s*\(.*?\)\s*)?"
            r"[A-Za-z_$][\w$]*\s*\(",
            text,
            flags=re.DOTALL,
        )
        return set(found) - own_modules - KEYWORDS

    @staticmethod
    def _replace_alias(token: str, aliases: dict[str, str]) -> str:
        seen = set()
        while token in aliases and token not in seen:
            seen.add(token)
            token = aliases[token]
        return token

    def canonicalize(self, text: str) -> CanonicalRTL:
        text = self._normalize_backend_metadata(self._strip_comments(text))
        own_modules = set(re.findall(r"\bmodule\s+([A-Za-z_$][\w$]*)", text))
        fixed = KEYWORDS | self._instance_types(text, own_modules) | {"RSCID_VALUE"}
        units = self._units(text)

        declaration_kinds: dict[str, str] = {}
        for unit in units:
            declaration = DECL_RE.match(unit)
            if declaration:
                for name in self._declared_names(unit):
                    declaration_kinds[name] = declaration.group(1)

        aliases: dict[str, str] = {}
        for unit in units:
            match = SIMPLE_ALIAS_RE.match(unit)
            if match and declaration_kinds.get(match.group(1)) == "wire":
                aliases[match.group(1)] = match.group(2)

        names: dict[str, str] = {}
        port_index = local_index = 0
        for unit in units:
            declaration = DECL_RE.match(unit)
            for name in self._declared_names(unit):
                root = self._replace_alias(name, aliases)
                if root in names or root in fixed:
                    continue
                if declaration and declaration.group(1) in {"input", "output", "inout"}:
                    names[root] = f"port{port_index}"
                    port_index += 1
                else:
                    names[root] = f"net{local_index}"
                    local_index += 1
        for module_name in own_modules:
            names[module_name] = "MODULE"

        def normalize(unit: str) -> str:
            result = []
            nonlocal local_index
            for token in TOKEN_RE.findall(unit):
                if IDENT_RE.match(token) and token not in fixed:
                    token = self._replace_alias(token, aliases)
                    if token not in names:
                        names[token] = f"net{local_index}"
                        local_index += 1
                    token = names[token]
                result.append(token)
            return " ".join(result)

        inputs, outputs, inouts, declarations, constants = [], [], [], [], []
        combinational, sequential, instances = [], [], []
        alias_lhs = set(aliases)
        for unit in units:
            alias_match = SIMPLE_ALIAS_RE.match(unit)
            if alias_match and alias_match.group(1) in alias_lhs:
                continue
            declaration = DECL_RE.match(unit)
            if declaration:
                # Declarations of eliminated alias-only wires carry no behavior.
                declared = self._declared_names(unit)
                if declared and all(name in alias_lhs for name in declared):
                    continue
                normalized = normalize(unit)
                kind = declaration.group(1)
                if kind == "input":
                    inputs.append(normalized)
                elif kind == "output":
                    outputs.append(normalized)
                elif kind == "inout":
                    inouts.append(normalized)
                elif kind in {"parameter", "localparam"}:
                    constants.append(normalized)
                else:
                    declarations.append(normalized)
            elif re.match(r"^\s*assign\b", unit):
                combinational.append(normalize(unit))
            elif re.search(r"\b(?:always|initial)\b", unit):
                sequential.append(normalize(unit))
            elif re.search(r"[A-Za-z_$][\w$]*\s*(?:#\s*\(.*\)\s*)?[A-Za-z_$][\w$]*\s*\(", unit, re.S):
                instances.append(normalize(unit))
            else:
                # Preserve unknown constructs conservatively and in source order.
                sequential.append(normalize(unit))

        return CanonicalRTL(
            tuple(inputs), tuple(outputs), tuple(inouts),
            tuple(declarations), tuple(constants),
            tuple(sorted(combinational)), tuple(sequential), tuple(sorted(instances)),
        )

    def serialization(self, text: str) -> str:
        return self.canonicalize(text).serialize()

    def fingerprint(self, text: str) -> str:
        return hashlib.sha256(self.serialization(text).encode()).hexdigest()
