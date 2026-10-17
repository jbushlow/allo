# Copyright Allo authors. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Prepare selected PID data uses for runtime PE arguments."""

from __future__ import annotations

import ast
import copy
import math


POLICIES = {"identity_only", "all_operations"}


def _decorator_name(decorator):
    func = decorator.func if isinstance(decorator, ast.Call) else decorator
    return func.attr if isinstance(func, ast.Attribute) else None


def _kernel_mapping(node, global_vars):
    for decorator in node.decorator_list:
        if _decorator_name(decorator) != "kernel" or not isinstance(
            decorator, ast.Call
        ):
            continue
        for keyword in decorator.keywords:
            if keyword.arg == "mapping":
                expression = compile(ast.Expression(keyword.value), "<ast>", "eval")
                return list(eval(expression, global_vars))
    return None


def _pid_targets(node):
    for stmt in ast.walk(node):
        if not isinstance(stmt, ast.Assign) or not isinstance(stmt.value, ast.Call):
            continue
        func = stmt.value.func
        if not isinstance(func, ast.Attribute) or func.attr != "get_pid":
            continue
        target = stmt.targets[0]
        if isinstance(target, ast.Tuple):
            return [item.id for item in target.elts if isinstance(item, ast.Name)]
        if isinstance(target, ast.Name):
            return [target.id]
    return []


def _root_name(node):
    while isinstance(node, ast.Subscript):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


class _PIDUseGeneralizer(ast.NodeTransformer):
    def __init__(self, pid_names, policy, stream_names):
        self.pid_axes = {name: axis for axis, name in enumerate(pid_names)}
        self.policy = policy
        self.stream_names = stream_names
        self.parents = []
        self.generalized_axes = set()

    def generic_visit(self, node):
        self.parents.append(node)
        result = super().generic_visit(node)
        self.parents.pop()
        return result

    def _is_structural(self, node):
        for parent in reversed(self.parents):
            if isinstance(
                parent, (ast.For, ast.While, ast.comprehension, ast.withitem)
            ):
                return True
            if isinstance(parent, ast.Subscript) and node in ast.walk(parent.slice):
                if _root_name(parent.value) in self.stream_names:
                    return True
            if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.Expr, ast.If)):
                break
        return False

    def _is_identity(self, node):
        for parent in reversed(self.parents):
            if isinstance(parent, ast.Compare):
                return True
            if isinstance(parent, ast.AnnAssign):
                return parent.value is node
            if isinstance(parent, ast.Assign):
                return parent.value is node
            if isinstance(parent, (ast.BinOp, ast.UnaryOp, ast.Call, ast.Subscript)):
                return False
        return False

    def visit_Name(self, node):
        if not isinstance(node.ctx, ast.Load) or node.id not in self.pid_axes:
            return node
        if self._is_structural(node):
            return node
        if self.policy == "identity_only" and not self._is_identity(node):
            return node
        axis = self.pid_axes[node.id]
        self.generalized_axes.add(axis)
        return ast.copy_location(ast.Name(id=f"_df_pid{axis}", ctx=ast.Load()), node)


def generalize_pid_uses(tree, global_vars, policy):
    """Add runtime PID data arguments while retaining structural specialization."""
    if policy not in POLICIES:
        raise ValueError(
            f"pid_generalization_policy must be one of {sorted(POLICIES)}, "
            f"got {policy!r}"
        )
    tree = copy.deepcopy(tree)
    stream_names = {
        node.target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.annotation is not None
        and "Stream" in ast.unparse(node.annotation)
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        mapping = _kernel_mapping(node, global_vars)
        pid_names = _pid_targets(node)
        if mapping is None or not pid_names:
            continue
        generalizer = _PIDUseGeneralizer(pid_names, policy, stream_names)
        generalizer.visit(node)
        axes = sorted(generalizer.generalized_axes)
        widths = [max(1, math.ceil(math.log2(mapping[axis]))) for axis in axes]
        for axis, width in zip(axes, widths):
            annotation = ast.Call(
                func=ast.Name(id="UInt", ctx=ast.Load()),
                args=[ast.Constant(width)],
                keywords=[],
            )
            node.args.args.append(ast.arg(arg=f"_df_pid{axis}", annotation=annotation))
        node.pid_generalized_axes = axes
        node.pid_generalized_widths = widths
        node.pid_generalization_policy = policy
    ast.fix_missing_locations(tree)
    return tree
