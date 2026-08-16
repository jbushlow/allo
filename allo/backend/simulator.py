# Copyright Allo authors. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
# pylint: disable=no-name-in-module, super-init-not-called, too-many-nested-blocks, too-many-branches
# pylint: disable=consider-using-enumerate, no-value-for-parameter, too-many-function-args, redefined-variable-type

import os
import ctypes
import numpy as np
from ..backend.llvm import LLVMModule
from .._mlir.ir import (
    Location,
    UnitAttr,
    InsertionPoint,
    Module,
    Context,
    Region,
    RegionSequence,
    Block,
    BlockArgument,
    BlockArgumentList,
    OpView,
    OpResult,
    OpOperandList,
    Operation,
    Value,
    TypeAttr,
    StringAttr,
    AffineMapAttr,
    AffineMap,
    AffineExpr,
    FunctionType,
    MemRefType,
    RankedTensorType,
    DenseElementsAttr,
    IntegerAttr,
    IntegerType,
    FloatType,
    IndexType,
    FlatSymbolRefAttr,
)
from .._mlir.dialects import (
    allo as allo_d,
    func as func_d,
    memref as memref_d,
    openmp as openmp_d,
    arith as arith_d,
    index as index_d,
    affine as affine_d,
    scf as scf_d,
    llvm as llvm_d,
)
from .._mlir.passmanager import PassManager
from .._mlir.execution_engine import ExecutionEngine
from .._mlir.runtime import get_ranked_memref_descriptor
from ..ir.transform import find_func_in_module
from ..passes import decompose_library_function
from ..utils import get_func_inputs_outputs


class DeadlockError(RuntimeError):
    """The design provably cannot make progress, raised instead of hanging forever.
    `.detail` carries the localisation (which stream, which PE, which role)."""


# The `walk` function
def recursive_collect_ops(
    top_op: Operation, target_op_type: tuple[type], res_list: list
):
    if isinstance(top_op, target_op_type):
        res_list.append(top_op)
    for region in top_op.regions:
        for block in region.blocks:
            for op in block:
                recursive_collect_ops(op, target_op_type, res_list)


# Useful when searching for omp operations after lowering
def recursive_collect_ops_by_name(
    top_op: Operation, target_op_name: str, res_list: list
):
    if top_op.name == target_op_name:
        res_list.append(top_op)
    for region in top_op.regions:
        for block in region.blocks:
            for op in block:
                recursive_collect_ops_by_name(op, target_op_name, res_list)


# ---------------------------------------------------------------------------
# The cost model.
#
# Latencies are abstract cycles, roughly calibrated to Vitis-HLS FPGA operator
# latencies.  The model is deliberately *correct, not cycle-accurate*: op ordering
# and determinism are exact, magnitudes are approximate.  Its job is to let DSE
# score one design against another, not to predict silicon.
#
# The model runs on user-level IR (before stream lowering), so none of the
# simulator's own ring-buffer plumbing is charged.
#
# DSE can retune it by mutating OP_LATENCY / the constants below before df.build.
# ---------------------------------------------------------------------------

# Ops with no cost of their own: pure bookkeeping, or combinational logic that HLS
# chains into a neighbouring cycle.  Integer add/compare/select live here -- a chain
# of them is one cycle, and the per-block floor below keeps a block from going free.
_FREE_OPS = frozenset(
    [
        # structural / addressing
        "arith.constant", "arith.index_cast", "arith.index_castui",
        "arith.extsi", "arith.extui", "arith.trunci", "arith.bitcast",
        "affine.apply", "affine.yield", "scf.yield", "func.return",
        "memref.alloc", "memref.alloca", "memref.dealloc", "memref.get_global",
        "memref.cast", "memref.subview", "memref.expand_shape",
        "memref.collapse_shape", "memref.dim",
        "unrealized_conversion_cast",
        "allo.struct_construct", "allo.struct_get",
        # control flow: the branch is free, the bodies are charged as their own blocks
        "affine.if", "affine.for", "scf.if", "scf.for", "scf.while", "scf.condition",
        # combinational integer logic
        "arith.addi", "arith.subi", "arith.andi", "arith.ori", "arith.xori",
        "arith.shli", "arith.shrsi", "arith.shrui", "arith.cmpi", "arith.select",
        "arith.maxsi", "arith.minsi", "arith.maxui", "arith.minui",
        "arith.negf", "math.absf", "math.absi",
        # a stream status probe reads a signal; it does not move data
        "allo.stream_empty", "allo.stream_full",
    ]
)

# Fixed-latency ops that do not depend on operand width.
OP_LATENCY = {
    "arith.cmpf": 1,
    "arith.sitofp": 2, "arith.uitofp": 2,
    "arith.fptosi": 2, "arith.fptoui": 2,
    "arith.extf": 1, "arith.truncf": 1,
    "math.sqrt": 15, "math.rsqrt": 15,
    "math.exp": 20, "math.log": 20, "math.powf": 25,
    "math.sin": 25, "math.cos": 25, "math.tanh": 25,
    # the data movement itself; *waiting* is modelled by the timestamp barriers,
    # not charged here
    "allo.stream_put": 1, "allo.stream_get": 1,
    "allo.stream_try_put": 1, "allo.stream_try_get": 1,
}

# Width-dependent arithmetic: (latency at <=32 bits, latency at >32 bits).
_FLOAT_LATENCY = {
    "arith.addf": (4, 6), "arith.subf": (4, 6),
    "arith.mulf": (3, 5),
    "arith.divf": (14, 24), "arith.remf": (14, 24),
}
_INT_MUL_LATENCY = (1, 3)  # one DSP-registered cycle; wider needs a DSP cascade

# BRAM access: a read has a registered output, a write posts in one cycle.  Scalars
# that HLS keeps in registers cost nothing -- see _is_register_memref.
ARRAY_LOAD_LATENCY = 2
ARRAY_STORE_LATENCY = 1

# An opaque helper call. A sharper model would charge the callee's own latency;
# this is a known approximation.
CALL_LATENCY = 1

# Assumed initiation interval for loops with no explicit `pipeline_ii`.  Measured, not
# guessed: Vitis 2023.2 auto-pipelines these loops at II=1 with NO pragma in the emitted
# C++ (config_compile -pipeline_loops), so an unpipelined default overcharged a
# producer/consumer PE 2.7x (16 modelled vs 6 reported).  None = unpipelined.
DEFAULT_II = 1


def _bitwidth(ty):
    if isinstance(ty, IntegerType):
        return ty.width
    if isinstance(ty, FloatType):
        return ty.width
    return 32


def _is_register_memref(value):
    """True if this memref is a scalar cell -- rank 0 or a single element.  Allo
    materialises PE-local scalars (`c: float32 = 0`, `c += ...`) as memrefs, but HLS
    keeps them in registers, so their loads/stores are free rather than BRAM
    accesses."""
    try:
        ty = MemRefType(value.type)
    except (ValueError, TypeError):
        return False
    shape = list(ty.shape)
    return len(shape) == 0 or all(d == 1 for d in shape)


def _op_latency(op):
    """Static latency (abstract cycles) of one op under the cost model above."""
    name = op.operation.name

    if name in _FREE_OPS:
        # a pipelined loop still costs its fill/drain once, where the loop sits
        if name in ("affine.for", "scf.for"):
            return _loop_prologue_latency(op)
        return 0

    if name in OP_LATENCY:
        return OP_LATENCY[name]

    if name in _FLOAT_LATENCY:
        narrow, wide = _FLOAT_LATENCY[name]
        return narrow if _bitwidth(op.results[0].type) <= 32 else wide

    if name == "arith.muli":
        narrow, wide = _INT_MUL_LATENCY
        return narrow if _bitwidth(op.results[0].type) <= 32 else wide

    if name in ("arith.divsi", "arith.divui", "arith.remsi", "arith.remui"):
        # non-restoring division is roughly one cycle per result bit
        return max(_bitwidth(op.results[0].type), 8)

    if name in ("affine.load", "memref.load"):
        return 0 if _is_register_memref(op.operands[0]) else ARRAY_LOAD_LATENCY

    if name in ("affine.store", "memref.store"):
        # (value, memref, indices...)
        return 0 if _is_register_memref(op.operands[1]) else ARRAY_STORE_LATENCY

    if name == "func.call":
        return CALL_LATENCY

    # Unknown op: charge one cycle rather than silently zero, so a design using
    # something the table has not seen still advances its clock.
    return 1


def _loop_ii(loop_op):
    """The initiation interval of a pipelined loop, or None if it is not pipelined."""
    if "pipeline_ii" in loop_op.attributes:
        return max(int(IntegerAttr(loop_op.attributes["pipeline_ii"]).value), 1)
    return DEFAULT_II


def _loop_body_latency(loop_op):
    """Latency of one iteration of the loop body, ignoring pipelining."""
    total = 0
    for region in loop_op.regions:
        for block in region.blocks:
            total += sum(_op_latency(inner) for inner in block.operations)
    return total


def _loop_prologue_latency(loop_op):
    """Charged once where a pipelined loop sits: its fill/drain.  With the body block
    charged `II` per iteration, `n*II + (body - II)` = `(n-1)*II + body`, the standard
    pipelined-loop latency.  Zero for an unpipelined loop."""
    ii = _loop_ii(loop_op)
    if ii is None:
        return 0
    return max(0, _loop_body_latency(loop_op) - ii)


def _block_latency(block, parent_op=None):
    """Cost charged once per execution of this block.  Ops directly in the block only
    -- nested regions get their own increments, so a loop body is charged per
    iteration.  A pipelined loop body costs its II instead of its full latency."""
    if parent_op is not None and parent_op.operation.name in ("affine.for", "scf.for"):
        ii = _loop_ii(parent_op)
        if ii is not None:
            return ii
    total = sum(_op_latency(op) for op in block.operations)
    # Floor: any block doing real work takes at least a cycle. Keeps a body of purely
    # combinational ops from advancing the clock by zero, which would let a peer spin
    # forever in a time barrier waiting on a clock that never moves.
    if total == 0 and any(op.operation.name not in _FREE_OPS for op in block.operations):
        return 1
    return total


def _collect_blocks(op, res):
    """Collect (block, parent_op) for every Block in op's regions, recursively -- but
    NOT inside spin-wait scf.while loops: their iteration count is scheduler-dependent,
    so charging the clock per spin would make it non-deterministic. The while op still
    counts once in its enclosing block (via _block_latency)."""
    for region in op.regions:
        for block in region.blocks:
            res.append((block, op))
            for inner in block.operations:
                if inner.operation.name == "scf.while":
                    continue
                _collect_blocks(inner, res)


def _collect_top_loop_bodies(func, res):
    """Body blocks of loops sitting DIRECTLY in the function's entry block -- i.e. the
    PE's outermost (cycle) loop. Used by the shared-clock cost model, which treats one
    such iteration as one cycle rather than charging each nested scalar op."""
    for region in func.regions:
        for block in region.blocks:
            for op in block.operations:
                if op.operation.name in ("affine.for", "scf.for"):
                    for r in op.regions:
                        for b in r.blocks:
                            res.append(b)


def _insert_cycle_clock_increments(func, clock_arg, module):
    """Shared-clock cost model: charge +1 per iteration of the PE's OUTERMOST loop, so
    every PE's clock counts *cycles* and all PEs advance at the same rate.

    Rationale (see simulator_shared_clock.md): the per-op model charges a router's
    decode + 5x5 arbitration + crossbar ~270 cycles per pass while a collector pass
    charges ~2.  Since the read barrier compares clocks ACROSS PEs (ts[head] <= T), the
    heavy PE's output timestamps become unreachable and its data is never read -- packet
    written, never delivered, no deadlock.  In hardware that router body is one
    pipelined cycle (II=1), so per-iteration is both the fairer and the truer model."""
    i64 = IntegerType.get_signless(64, module.context)
    bodies = []
    _collect_top_loop_bodies(func, bodies)
    if not bodies:
        # No cycle loop (e.g. a one-shot loader): charge the whole body as one cycle so
        # the PE still advances and peers waiting on its clock are released.
        entry = func.body.blocks[0]
        bodies = [entry] if len(entry.operations) > 0 else []
    for block in bodies:
        if len(block.operations) == 0:
            continue
        ip = InsertionPoint(beforeOperation=block.operations[0])
        cur = memref_d.LoadOp(memref=clock_arg, indices=[], ip=ip)
        inc = arith_d.ConstantOp(i64, 1, ip=ip)
        nxt = arith_d.AddIOp(lhs=cur.result, rhs=inc.result, ip=ip)
        memref_d.StoreOp(nxt, clock_arg, [], ip=ip)


def _insert_clock_increments(func, clock_arg, module):
    """Insert `clock_arg += static_block_latency` at the start of every block of
    the PE function (recursively), so the PE's clock tracks simulated time under
    the cost model."""
    if os.getenv("ALLO_SIM_CLOCK") == "cycle":
        return _insert_cycle_clock_increments(func, clock_arg, module)
    i64 = IntegerType.get_signless(64, module.context)
    blocks = []
    _collect_blocks(func, blocks)
    for block, parent_op in blocks:
        lat = _block_latency(block, parent_op)
        if lat == 0:
            continue
        ip = InsertionPoint(beforeOperation=block.operations[0])
        cur = memref_d.LoadOp(memref=clock_arg, indices=[], ip=ip)
        inc = arith_d.ConstantOp(i64, lat, ip=ip)
        nxt = arith_d.AddIOp(lhs=cur.result, rhs=inc.result, ip=ip)
        memref_d.StoreOp(nxt, clock_arg, [], ip=ip)


def _add_pe_clock_args(module, top_func_name):
    """Reorder step: give each PE a trailing memref<i64> clock arg and thread it
    through the (recreated) calls, BEFORE streams are lowered -- so put/get lowering
    can stamp/advance the clock. Each clocked PE func is tagged 'sim.clock'; its
    clock is the last block arg. Per-block increments come later (after lowering).

    Returns the per-call-site clock cells as a list of (caller_func, callee_name,
    clock_value) in a deterministic order -- the index into this list is the PE index
    used by the cycle read-out (_emit_cycle_harvest)."""
    i64 = IntegerType.get_signless(64, module.context)
    clk_ty = MemRefType.get([], i64)
    funcs = {
        str(op.sym_name).strip('"'): op
        for op in module.body.operations
        if isinstance(op, func_d.FuncOp) and len(op.body.blocks) > 0
    }
    clocked = set()
    clock_cells = []
    for caller in funcs.values():
        calls = []
        recursive_collect_ops(caller, (func_d.CallOp,), calls)
        for call_op in calls:
            callee_name = str(call_op.callee)[1:]
            callee = funcs.get(callee_name)
            if callee is None or callee_name.startswith(
                ("load_buf", "store_res", "usleep")
            ):
                continue
            # PE kernels are void; a value-returning callee is a pure helper (e.g. an
            # index-calc func called inside a PE loop) -- not a PE, so don't clock it
            # (and don't recreate its call, which would need result rewiring).
            if len(callee.type.results) > 0:
                continue
            if callee_name not in clocked:
                clocked.add(callee_name)
                callee.body.blocks[0].add_argument(clk_ty, Location.unknown())
                old_ty = callee.type
                new_ty = FunctionType.get(
                    list(old_ty.inputs) + [clk_ty], list(old_ty.results)
                )
                callee.attributes["function_type"] = TypeAttr.get(new_ty)
                callee.attributes["sim.clock"] = UnitAttr.get()
            # Recompute the entry IP each iteration: at_block_begin captures the
            # current first op, and in a stream-less design the first op is a PE call
            # that we erase below -- reusing a stale IP inserts before a dead op. The
            # allocs stack at the block top, so they still dominate the (soon omp-
            # sectioned) calls.
            entry_ip = InsertionPoint.at_block_begin(caller.body.blocks[0])
            clk = memref_d.AllocOp(clk_ty, [], [], ip=entry_ip)
            c0 = arith_d.ConstantOp(i64, 0, ip=entry_ip)
            memref_d.StoreOp(c0, clk, [], ip=entry_ip)
            # Preserve the call's result types (value-returning helpers called from
            # inside a PE, e.g. an index-calc func, have results still in use) --
            # adding a clock arg adds no result, so recreate with the SAME results
            # and rewire uses, else erase() trips "op destroyed but still has uses".
            new_call = func_d.CallOp(
                [r.type for r in call_op.results], call_op.callee,
                list(call_op.operands_) + [clk.result],
                ip=InsertionPoint(beforeOperation=call_op),
            )
            for old_res, new_res in zip(call_op.results, new_call.results):
                old_res.replace_all_uses_with(new_res)
            call_op.operation.erase()
            clock_cells.append((caller, callee_name, clk.result))
    return clock_cells


# A finished PE ORs this bit into its clock: the value becomes larger than any real
# simulated time, so a peer stuck in a time-barrier on this PE unblocks -- while the
# PE's *final* cycle count stays recoverable by masking the bit off (see
# _emit_cycle_harvest).  Real clocks stay well under 2^62, and 2^62 is positive in
# i64, so the signed >= comparisons in the barriers keep working.
_CLOCK_DONE_BIT = 1 << 62
_CLOCK_VALUE_MASK = _CLOCK_DONE_BIT - 1

# Symbols for reading the per-PE cycle counts back out into Python.  The global has
# three extra trailing slots carrying the deadlock report, so detection needs no
# second global/reader pair: [.. N per-PE cycles .., flag, stream_id, role].
SIM_CYCLES_GLOBAL = "__allo_sim_cycles"
SIM_CYCLES_READER = "__allo_sim_cycles_read"
# [.. N cycles .., flag, stream_id, role, n_blocked, atomic_sink, cause]
_DL_SLOTS = 6
_DL_ROLE_PUT, _DL_ROLE_GET = 1, 2
# Which detector fired.  Worth distinguishing: PEER_DONE is a proof and names the
# guilty stream, CIRCULAR is a threshold and the reporting PE is just whichever
# noticed -- so the two need different wording and carry different confidence.
_DL_CAUSE_PEER_DONE, _DL_CAUSE_CIRCULAR = 1, 2
# Consecutive all-blocked polls before declaring circular wait.  Each poll costs one
# usleep(1) (~50-60us real), so the default is roughly a second of total quiescence.
# Unlike the peer-done proof this IS a threshold: too low false-positives a slow
# design, too high just delays the report.  Override with ALLO_SIM_DEADLOCK_POLLS.
_DL_POLLS = int(os.getenv("ALLO_SIM_DEADLOCK_POLLS", "16384"))


# Per-build state the deep stream-lowering call chain needs but does not thread:
# how wide the read-out global is, and a small integer id per stream (so a deadlock
# report can name the stream). Reset at the top of build_dataflow_simulator.
_SIM_CTX = {"n_pes": 0, "stream_ids": {}}


def _stream_id(name):
    """Stable small integer for a stream name, assigned in first-seen order."""
    return _SIM_CTX["stream_ids"].setdefault(str(name).strip('"'), len(_SIM_CTX["stream_ids"]))


def _sim_global_type(module, n_pes):
    """Type of the shared read-out global: N per-PE cycle slots + the deadlock triple."""
    i64 = IntegerType.get_signless(64, module.context)
    return MemRefType.get([n_pes + _DL_SLOTS], i64)


def _emit_report_store(module, n, sid, role, ip, cause):
    """Record [flag=1, stream_id, role] in the read-out global -- the deadlock report
    Python reads back after the run.  Shared by both detectors."""
    i64 = IntegerType.get_signless(64, module.context)
    idx_ty = IndexType.get(module.context)
    g = memref_d.GetGlobalOp(_sim_global_type(module, n), SIM_CYCLES_GLOBAL, ip=ip)
    for off, v in ((0, 1), (1, sid), (2, role), (5, cause)):
        memref_d.StoreOp(
            arith_d.ConstantOp(i64, v, ip=ip),
            g.result,
            [arith_d.ConstantOp(idx_ty, n + off, ip=ip).result],
            ip=ip,
        )


def _emit_blocked_delta(module, n, delta, ip):
    """Atomically add `delta` to the shared blocked-PE counter.  Must be atomic: every
    PE thread updates it concurrently, so a load/add/store would lose increments and
    the count would drift below n_pes, silently disabling circular-wait detection."""
    i64 = IntegerType.get_signless(64, module.context)
    idx_ty = IndexType.get(module.context)
    g = memref_d.GetGlobalOp(_sim_global_type(module, n), SIM_CYCLES_GLOBAL, ip=ip)
    slot = arith_d.ConstantOp(idx_ty, n + 3, ip=ip)
    amt = arith_d.ConstantOp(i64, delta, ip=ip)
    rmw = memref_d.AtomicRMWOp(
        arith_d.AtomicRMWKind.addi, amt.result, g.result, [slot.result], ip=ip
    )
    # MemRefDCE.cpp:28 erases ANY op with results and no uses -- it does not check for
    # side effects -- so an atomic whose old value we ignore is silently deleted (the
    # same trap as a try_put whose ok flag is unread).  Sink the result into a live
    # global slot: a store has no results, and the target is read back by the reader,
    # so neither this nor the atomic can be folded away.
    memref_d.StoreOp(
        rmw.result, g.result,
        [arith_d.ConstantOp(idx_ty, n + 4, ip=ip).result], ip=ip,
    )


def _emit_deadlock_guard(
    blocked, peer_ptr, role, stream_name, module, before_ip, after_block,
    head_ptr, tail_ptr, depth_val, while_op, outer_ip,
):
    """Make a blocking spin loop give up instead of hanging forever.

    A PE blocked on a stream whose peer PE has already *finished* can never be
    unblocked: no one will ever drain the full FIFO / fill the empty one.  That is a
    proof of deadlock, not a heuristic -- no timeout, no threshold, no false positives.

    Emits two things.  In the spin body: if the peer's clock has _CLOCK_DONE_BIT set,
    record (stream_id, role) in the read-out global and raise its flag.  Returns a
    replacement loop condition `blocked AND flag == 0`, so that once ANY PE reports,
    every spinning PE unwinds rather than hanging behind the one that noticed.

    The peer-done test alone would be a false positive: `blocked` is sampled in the
    loop condition, and the peer can supply the datum AND return before the body
    runs, so a healthy PE that is no longer blocked would still report.  The body
    therefore RE-checks the block condition after observing DONE.  That order is
    sound: DONE is set at function return, i.e. after the peer's last put, so once
    DONE is visible and we are still blocked, no datum can ever arrive.

    Returns `blocked` unchanged when there are no clocked PEs (no global exists)."""
    n = _SIM_CTX["n_pes"]
    if n == 0:
        return blocked
    i64 = IntegerType.get_signless(64, module.context)
    idx_ty = IndexType.get(module.context)
    arr_ty = _sim_global_type(module, n)
    # The spin body already ends in scf.yield by the time we get here, so insert
    # ahead of that terminator rather than at the end of the block.
    # Count this PE as blocked for as long as the spin runs.  The alloca and the +1
    # go before the loop; the -1 goes after it (outer_ip inserts after the while, so
    # we are already unblocked before the transfer that follows).
    pre_ip = InsertionPoint(beforeOperation=while_op)
    polls = memref_d.AllocaOp(MemRefType.get([], i64), [], [], ip=pre_ip)
    memref_d.StoreOp(arith_d.ConstantOp(i64, 0, ip=pre_ip), polls.memref, [], ip=pre_ip)
    _emit_blocked_delta(module, n, 1, pre_ip)
    _emit_blocked_delta(module, n, -1, outer_ip)
    ops = after_block.operations
    aip = InsertionPoint(beforeOperation=ops[len(ops) - 1])
    pc = memref_d.LoadOp(memref=peer_ptr, indices=[], ip=aip)
    bit = arith_d.ConstantOp(i64, _CLOCK_DONE_BIT, ip=aip)
    masked = arith_d.AndIOp(lhs=pc.result, rhs=bit.result, ip=aip)
    zero64 = arith_d.ConstantOp(i64, 0, ip=aip)
    peer_done = arith_d.CmpIOp(1, lhs=masked.result, rhs=zero64.result, ip=aip)
    if_op = scf_d.IfOp(peer_done.result, [], has_else=False, ip=aip)
    dip = InsertionPoint(if_op.then_block)
    # Re-check under a fresh flush: are we STILL blocked now that the peer is done?
    openmp_d.FlushOp([], ip=dip)
    h2 = memref_d.LoadOp(memref=head_ptr, indices=[], ip=dip)
    t2 = memref_d.LoadOp(memref=tail_ptr, indices=[], ip=dip)
    if role == _DL_ROLE_PUT:  # full == head equals the slot we are about to write
        # head/tail are the FIFO's own index type (i32), NOT the i64 clock type.
        one = arith_d.ConstantOp(t2.result.type, 1, ip=dip)
        nxt = arith_d.AddIOp(lhs=t2.result, rhs=one.result, ip=dip)
        t2cmp = arith_d.RemUIOp(lhs=nxt.result, rhs=depth_val, ip=dip)
    else:  # empty == head equals tail
        t2cmp = t2.result
    still = arith_d.CmpIOp(0, lhs=h2.result, rhs=t2cmp, ip=dip)
    still_if = scf_d.IfOp(still.result, [], has_else=False, ip=dip)
    scf_d.YieldOp(results_=[], ip=dip)
    tip = InsertionPoint(still_if.then_block)
    _emit_report_store(module, n, _stream_id(stream_name), role, tip,
                       _DL_CAUSE_PEER_DONE)
    scf_d.YieldOp(results_=[], ip=tip)
    # Circular-wait watchdog.  No PE ever finishes in a cycle of blocked PEs, so the
    # peer-done proof above never fires.  Instead count polls where EVERY PE is
    # blocked-or-finished; the loop body only runs while we are still blocked, so
    # reaching the threshold means nothing moved anywhere for that whole window.
    gw = memref_d.GetGlobalOp(arr_ty, SIM_CYCLES_GLOBAL, ip=aip)
    bslot = arith_d.ConstantOp(idx_ty, n + 3, ip=aip)
    nb = memref_d.LoadOp(memref=gw.result, indices=[bslot.result], ip=aip)
    n_all = arith_d.ConstantOp(i64, n, ip=aip)
    all_blocked = arith_d.CmpIOp(0, lhs=nb.result, rhs=n_all.result, ip=aip)
    cur = memref_d.LoadOp(memref=polls.memref, indices=[], ip=aip)
    one_i = arith_d.ConstantOp(i64, 1, ip=aip)
    inc = arith_d.AddIOp(lhs=cur.result, rhs=one_i.result, ip=aip)
    zero_i = arith_d.ConstantOp(i64, 0, ip=aip)
    # Any poll where someone was runnable resets the run to zero.
    nxt_polls = arith_d.SelectOp(all_blocked.result, inc.result, zero_i.result, ip=aip)
    memref_d.StoreOp(nxt_polls, polls.memref, [], ip=aip)
    limit = arith_d.ConstantOp(i64, _DL_POLLS, ip=aip)
    hit = arith_d.CmpIOp(5, lhs=nxt_polls.result, rhs=limit.result, ip=aip)  # sge
    wif = scf_d.IfOp(hit.result, [], has_else=False, ip=aip)
    wtip = InsertionPoint(wif.then_block)
    _emit_report_store(module, n, _stream_id(stream_name), role, wtip,
                       _DL_CAUSE_CIRCULAR)
    scf_d.YieldOp(results_=[], ip=wtip)

    # Loop condition: keep spinning only while nobody (this PE or any other) has
    # reported, so a single report unwinds every blocked PE.
    g2 = memref_d.GetGlobalOp(arr_ty, SIM_CYCLES_GLOBAL, ip=before_ip)
    fslot = arith_d.ConstantOp(idx_ty, n, ip=before_ip)
    flag = memref_d.LoadOp(memref=g2.result, indices=[fslot.result], ip=before_ip)
    clear = arith_d.CmpIOp(
        0, lhs=flag.result, rhs=arith_d.ConstantOp(i64, 0, ip=before_ip).result,
        ip=before_ip,
    )
    return arith_d.AndIOp(lhs=blocked, rhs=clear.result, ip=before_ip).result


def _insert_clock_termination(func, clock_arg, module):
    """Mark the PE's clock 'done' at each function return, so a peer waiting on this
    (finished) PE's clock in the time-barrier unblocks instead of hanging forever.
    The mark is an OR of _CLOCK_DONE_BIT rather than an overwrite, so the final cycle
    count survives for _emit_cycle_harvest to read."""
    i64 = IntegerType.get_signless(64, module.context)
    returns = []
    recursive_collect_ops(func, (func_d.ReturnOp,), returns)
    for ret in returns:
        ip = InsertionPoint(beforeOperation=ret)
        cur = memref_d.LoadOp(memref=clock_arg, indices=[], ip=ip)
        bit = arith_d.ConstantOp(i64, _CLOCK_DONE_BIT, ip=ip)
        done = arith_d.OrIOp(cur.result, bit.result, ip=ip)
        memref_d.StoreOp(done, clock_arg, [], ip=ip)
        # A finished PE never makes progress again, so it counts as permanently
        # blocked -- otherwise n_blocked could never reach n_pes once any PE exits
        # and the circular-wait watchdog would be dead code in mixed designs.
        if _SIM_CTX["n_pes"]:
            _emit_blocked_delta(module, _SIM_CTX["n_pes"], 1, ip)


def _insert_pe_clock_increments(module):
    """Insert per-block clock increments + a termination sentinel into every clocked
    PE (tagged sim.clock), using its last block arg as the clock. Runs BEFORE stream
    lowering, so the cost model sees user-level ops only -- none of the ring-buffer
    plumbing that lowering inserts, and none of its spin-wait blocks."""
    for op in module.body.operations:
        if isinstance(op, func_d.FuncOp) and "sim.clock" in op.attributes:
            clock_arg = op.body.blocks[0].arguments[-1]
            _insert_clock_increments(op, clock_arg, module)
            _insert_clock_termination(op, clock_arg, module)


def _declare_sim_global(module, n_pes):
    """Declare the read-out global. Must run BEFORE stream lowering: the spin loops
    emit `memref.get_global` against this symbol as they are generated."""
    i64 = IntegerType.get_signless(64, module.context)
    arr_ty = _sim_global_type(module, n_pes)
    memref_d.GlobalOp(
        sym_name=StringAttr.get(SIM_CYCLES_GLOBAL),
        type_=TypeAttr.get(arr_ty),
        sym_visibility=StringAttr.get("private"),
        initial_value=DenseElementsAttr.get_splat(
            RankedTensorType.get([n_pes + _DL_SLOTS], i64), IntegerAttr.get(i64, 0)
        ),
        ip=InsertionPoint(module.body),
    )


def _emit_cycle_harvest(module, clock_cells):
    """Make the per-PE simulated-cycle counts readable from Python.

    Emits (a) a module-level `memref<Nxi64>` global, (b) a copy of each PE's final
    clock into `global[pe_index]` just before its caller returns (all PE calls are
    joined by then -- they sit inside omp.parallel), masking off _CLOCK_DONE_BIT, and
    (c) an exported reader `__allo_sim_cycles_read(memref<Nxi64>)` that copies the
    global into a caller-provided buffer.  Returns the list of PE names, whose order
    matches the buffer."""
    n = len(clock_cells)
    if n == 0:
        return []
    i64 = IntegerType.get_signless(64, module.context)
    idx_ty = IndexType.get(module.context)
    arr_ty = _sim_global_type(module, n)

    # (b) harvest: written before the caller's return, once per call site.
    for pe_index, (caller, _name, clk) in enumerate(clock_cells):
        returns = []
        recursive_collect_ops(caller, (func_d.ReturnOp,), returns)
        for ret in returns:
            ip = InsertionPoint(beforeOperation=ret)
            g = memref_d.GetGlobalOp(arr_ty, SIM_CYCLES_GLOBAL, ip=ip)
            val = memref_d.LoadOp(memref=clk, indices=[], ip=ip)
            mask = arith_d.ConstantOp(i64, _CLOCK_VALUE_MASK, ip=ip)
            final = arith_d.AndIOp(val.result, mask.result, ip=ip)
            slot = arith_d.ConstantOp(idx_ty, pe_index, ip=ip)
            memref_d.StoreOp(final, g.result, [slot.result], ip=ip)

    # (c) reader
    reader = func_d.FuncOp(
        name=SIM_CYCLES_READER,
        type=FunctionType.get([arr_ty], []),
        ip=InsertionPoint(module.body),
    )
    reader.attributes["llvm.emit_c_interface"] = UnitAttr.get()
    entry = reader.add_entry_block()
    ip = InsertionPoint(entry)
    g = memref_d.GetGlobalOp(arr_ty, SIM_CYCLES_GLOBAL, ip=ip)
    # n cycle slots + the deadlock triple: the caller's buffer is sized to match, so
    # copying short would leave the report unreadable and copying long would overrun.
    for k in range(n + _DL_SLOTS):
        slot = arith_d.ConstantOp(idx_ty, k, ip=ip)
        v = memref_d.LoadOp(memref=g.result, indices=[slot.result], ip=ip)
        memref_d.StoreOp(v, entry.arguments[0], [slot.result], ip=ip)
    func_d.ReturnOp([], ip=ip)

    return [name for _caller, name, _clk in clock_cells]


def _clock_of(func):
    """The PE's clock block arg (its last arg) if func is a clocked PE (tagged
    sim.clock), else None."""
    if "sim.clock" in func.attributes:
        return func.body.blocks[0].arguments[-1]
    return None


def _stamp_put_ts(ts_ptr, slot_index, clock_arg, ip):
    """On put: ts_ring[slot] = clock -- stamp the element with the producer's
    current simulated time. No-op when the enclosing func has no clock."""
    if clock_arg is None:
        return
    clk_val = memref_d.LoadOp(memref=clock_arg, indices=[], ip=ip)
    memref_d.StoreOp(clk_val, ts_ptr, [slot_index], ip=ip)


def _advance_get_ts(ts_ptr, slot_index, clock_arg, ip):
    """On get: clock = max(clock, ts_ring[slot]) -- advance the consumer clock to
    when the dequeued element was produced. No-op when there is no clock."""
    if clock_arg is None:
        return
    ts_val = memref_d.LoadOp(memref=ts_ptr, indices=[slot_index], ip=ip)
    clk_val = memref_d.LoadOp(memref=clock_arg, indices=[], ip=ip)
    mx = arith_d.MaxSIOp(lhs=clk_val.result, rhs=ts_val.result, ip=ip)
    memref_d.StoreOp(mx, clock_arg, [], ip=ip)


def _tick_clock(clock_arg, module, ip):
    """Advance the PE clock by 1 (one cycle). Called on a FAILED non-blocking poll so
    that spin-wait loops (while not try_put / while ok==0: try_get) advance simulated
    time -- otherwise a PE polling a full/empty FIFO freezes its clock and a peer's
    time-barrier deadlocks. The failure (and thus the tick count) is made deterministic
    by the barriers, so the clock stays deterministic. No-op without a clock."""
    if clock_arg is None:
        return
    i64 = IntegerType.get_signless(64, module.context)
    cur = memref_d.LoadOp(memref=clock_arg, indices=[], ip=ip)
    one = arith_d.ConstantOp(i64, 1, ip=ip)
    nxt = arith_d.AddIOp(lhs=cur.result, rhs=one.result, ip=ip)
    memref_d.StoreOp(nxt, clock_arg, [], ip=ip)


def _stamp_free_ts(free_ts_ptr, slot_index, clock_arg, ip):
    """On get: free_ts_ring[slot] = clock -- record the consumer's simulated time
    when it frees this slot (the write barrier reads it). No-op without a clock."""
    if clock_arg is None:
        return
    clk_val = memref_d.LoadOp(memref=clock_arg, indices=[], ip=ip)
    memref_d.StoreOp(clk_val, free_ts_ptr, [slot_index], ip=ip)


def _mark_occupied(free_ts_ptr, slot_index, clock_arg, module, ip):
    """On put: free_ts_ring[slot] = INT64_MAX -- mark the slot occupied (NOT free)
    until the consumer frees it (which overwrites it with the free time). Without
    this, a re-used slot's stale free-time makes the write barrier think it is free
    and the producer overfills the ring. No-op without a clock."""
    if clock_arg is None:
        return
    i64 = IntegerType.get_signless(64, module.context)
    maxc = arith_d.ConstantOp(i64, (1 << 63) - 1, ip=ip)
    memref_d.StoreOp(maxc, free_ts_ptr, [slot_index], ip=ip)


def _stream_producer_consumer_clocks(stream_construct_op, pe_call_define_ops):
    """For a stream, return (prod_clock, cons_clock): the memref<i64> clock cells of
    the PE that PUTs to it (producer) and the PE that GETs from it (consumer). Each is
    the last operand of that PE's call (added by _add_pe_clock_args); either may be
    None if the role can't be identified (then the time-barrier just no-ops)."""
    prod_clock = None
    cons_clock = None
    s_result = Value(stream_construct_op.result)
    for call_op, callee in pe_call_define_ops.items():
        clock = call_op.operands_[-1]
        for i in range(len(call_op.operands_)):
            if not (s_result == call_op.operands_[i]):
                continue
            ops = []
            recursive_collect_ops(
                callee,
                (allo_d.StreamPutOp, allo_d.StreamTryPutOp,
                 allo_d.StreamGetOp, allo_d.StreamTryGetOp),
                ops,
            )
            for o in ops:
                try:
                    if BlockArgument(o.stream).arg_number != i:
                        continue
                except ValueError:
                    continue
                if isinstance(o, (allo_d.StreamPutOp, allo_d.StreamTryPutOp)):
                    prod_clock = clock
                else:
                    cons_clock = clock
    return prod_clock, cons_clock



# Back-off used by both time barriers.  usleep(1) really sleeps ~50-60us (Linux timer
# granularity), which looks like pure overhead -- but removing it is NOT a win: measured
# 2026-07-27, spinning on taskyield alone stopped even a 2x2 mesh from completing while
# burning ~1040% CPU.  taskyield is only a hint; without a real sleep a spinner can hold
# its core against the very peer it is waiting for.  ALLO_SIM_SPIN=hot opts out, but only
# do that after measuring that a run is sleep-bound rather than spin-bound.
def _spin_backoff(module, aip):
    """Emit the barrier's back-off: taskyield, plus usleep(1) unless spinning hot."""
    openmp_d.TaskyieldOp(ip=aip)
    # NOTE: the usleep is NOT optional by default.  Dropping it (spinning on taskyield
    # alone) made even a 2x2 mesh stop completing, at ~1040% CPU: taskyield is only a
    # hint, so a spinner can burn its slice waiting on a peer the runtime never
    # schedules.  The sleep forces a real yield.  Opt in with ALLO_SIM_SPIN=hot only
    # after measuring that a run is sleep-bound rather than spin-bound.
    if os.getenv("ALLO_SIM_SPIN") == "hot":
        return
    c1 = arith_d.ConstantOp(IntegerType.get_signless(32, module.context), 1, ip=aip)
    func_d.CallOp([], FlatSymbolRefAttr.get("usleep"), [c1], ip=aip)


def _emit_read_barrier(head_ptr, tail_ptr, ts_ptr, prod_ptr, t_val, module, ip):
    """Read-side time-barrier for try_get: spin until the result is DECIDED --
    either an element produced by time T (=t_val) is present, or the producer's
    clock has passed T (so no such element is still coming). It does NOT wait while
    data is already available, so it cannot deadlock against a producer that is
    itself blocked on a full FIFO. All quantities are deterministic, so the decision
    (and thus the sampled result) is reproducible."""
    i1 = IntegerType.get_signless(1, module.context)
    idx = IndexType.get(module.context)
    while_op = scf_d.WhileOp(results_=[], inits=[], ip=ip)
    before = Block.create_at_start(parent=while_op.before, arg_types=[])
    bip = InsertionPoint(before)
    openmp_d.FlushOp([], ip=bip)
    h = memref_d.LoadOp(memref=head_ptr, indices=[], ip=bip)
    t = memref_d.LoadOp(memref=tail_ptr, indices=[], ip=bip)
    ne = arith_d.CmpIOp(1, lhs=h.result, rhs=t.result, ip=bip)          # head != tail
    hidx = index_d.CastUOp(output=idx, input=h, ip=bip)
    tsh = memref_d.LoadOp(memref=ts_ptr, indices=[hidx], ip=bip)
    tsle = arith_d.CmpIOp(3, lhs=tsh.result, rhs=t_val.result, ip=bip)  # ts[head] <= T
    has_data = arith_d.AndIOp(lhs=ne.result, rhs=tsle.result, ip=bip)
    pc = memref_d.LoadOp(memref=prod_ptr, indices=[], ip=bip)
    past = arith_d.CmpIOp(4, lhs=pc.result, rhs=t_val.result, ip=bip)   # prod > T
    decided = arith_d.OrIOp(lhs=has_data.result, rhs=past.result, ip=bip)
    true1 = arith_d.ConstantOp(i1, 1, ip=bip)
    not_decided = arith_d.XOrIOp(lhs=decided.result, rhs=true1.result, ip=bip)
    scf_d.ConditionOp(condition=not_decided.result, args=[], ip=bip)
    after = Block.create_at_start(parent=while_op.after, arg_types=[])
    aip = InsertionPoint(after)
    _spin_backoff(module, aip)
    scf_d.YieldOp(results_=[], ip=aip)


def _emit_write_barrier(free_ts_ptr, tail_next_idx, cons_ptr, t_val, module, ip):
    """Write-side time-barrier for try_put: spin until DECIDED = (the target slot was
    freed by time T: free_ts_ring[tail_next] <= T) OR (consumer passed T). Does not
    wait while space is available, so it can't deadlock against a consumer blocked on
    its own downstream. Deterministic (clocks/timestamps only)."""
    i1 = IntegerType.get_signless(1, module.context)
    while_op = scf_d.WhileOp(results_=[], inits=[], ip=ip)
    before = Block.create_at_start(parent=while_op.before, arg_types=[])
    bip = InsertionPoint(before)
    openmp_d.FlushOp([], ip=bip)
    ft = memref_d.LoadOp(memref=free_ts_ptr, indices=[tail_next_idx], ip=bip)
    free = arith_d.CmpIOp(3, lhs=ft.result, rhs=t_val.result, ip=bip)   # free_ts <= T
    cc = memref_d.LoadOp(memref=cons_ptr, indices=[], ip=bip)
    past = arith_d.CmpIOp(4, lhs=cc.result, rhs=t_val.result, ip=bip)   # cons > T
    decided = arith_d.OrIOp(lhs=free.result, rhs=past.result, ip=bip)
    true1 = arith_d.ConstantOp(i1, 1, ip=bip)
    not_decided = arith_d.XOrIOp(lhs=decided.result, rhs=true1.result, ip=bip)
    scf_d.ConditionOp(condition=not_decided.result, args=[], ip=bip)
    after = Block.create_at_start(parent=while_op.after, arg_types=[])
    aip = InsertionPoint(after)
    _spin_backoff(module, aip)
    scf_d.YieldOp(results_=[], ip=aip)


def _lower_nb_stream_op(
    stream_access_op, head_ptr, tail_ptr, fifo_ptr,
    stream_type, const_one, const_fifo_depth, module, replace_ip,
    ts_ptr, clock_arg, prod_ptr, cons_ptr, free_ts_ptr,
):
    """Lower a non-blocking / status stream op (empty/full/try_put/try_get)
    to its ring-buffer implementation. Returns True if it handled the op,
    False for a blocking put/get (left to the caller). Shared by the
    cross-call and local lowering paths (Phase 0 refactor)."""
    if isinstance(stream_access_op, allo_d.StreamEmptyOp):
        openmp_d.FlushOp([], ip=replace_ip)
        head_val = memref_d.LoadOp(memref=head_ptr, indices=[], ip=replace_ip)
        tail_val = memref_d.LoadOp(memref=tail_ptr, indices=[], ip=replace_ip)
        cmp_op = arith_d.CmpIOp(0, lhs=head_val, rhs=tail_val, ip=replace_ip)
        stream_access_op.results[0].replace_all_uses_with(cmp_op.result)
        stream_access_op.operation.erase()
        return True
    if isinstance(stream_access_op, allo_d.StreamFullOp):
        openmp_d.FlushOp([], ip=replace_ip)
        tail_val = memref_d.LoadOp(memref=tail_ptr, indices=[], ip=replace_ip)
        tail_inc = arith_d.AddIOp(
            lhs=tail_val.result, rhs=const_one.result, ip=replace_ip
        )
        tail_next = arith_d.RemUIOp(
            lhs=tail_inc.result, rhs=const_fifo_depth.result, ip=replace_ip
        )
        head_val = memref_d.LoadOp(memref=head_ptr, indices=[], ip=replace_ip)
        cmp_op = arith_d.CmpIOp(
            0, lhs=tail_next.result, rhs=head_val.result, ip=replace_ip
        )
        stream_access_op.results[0].replace_all_uses_with(cmp_op.result)
        stream_access_op.operation.erase()
        return True
    if isinstance(stream_access_op, allo_d.StreamTryPutOp):
        openmp_d.FlushOp([], ip=replace_ip)
        tail_val_op = memref_d.LoadOp(memref=tail_ptr, indices=[], ip=replace_ip)
        tail_inc_op = arith_d.AddIOp(
            lhs=tail_val_op.result, rhs=const_one.result, ip=replace_ip
        )
        tail_next_op = arith_d.RemUIOp(
            lhs=tail_inc_op.result, rhs=const_fifo_depth.result, ip=replace_ip
        )
        if clock_arg is not None:
            # Write barrier: wait until the target slot is free by our time T, or the
            # consumer has passed T; then put_cond = free_ts_ring[tail_next] <= T.
            t_val = memref_d.LoadOp(memref=clock_arg, indices=[], ip=replace_ip)
            tail_next_idx = index_d.CastUOp(
                output=IndexType.get(module.context), input=tail_next_op, ip=replace_ip
            )
            _emit_write_barrier(
                free_ts_ptr, tail_next_idx, cons_ptr, t_val, module, replace_ip
            )
            openmp_d.FlushOp([], ip=replace_ip)
            ft = memref_d.LoadOp(memref=free_ts_ptr, indices=[tail_next_idx], ip=replace_ip)
            put_cond = arith_d.CmpIOp(  # free_ts_ring[tail_next] <= T  (not full at T)
                3, lhs=ft.result, rhs=t_val.result, ip=replace_ip
            )
        else:
            head_val_op = memref_d.LoadOp(memref=head_ptr, indices=[], ip=replace_ip)
            put_cond = arith_d.CmpIOp(  # head != tail_next  (physical not-full)
                1, lhs=head_val_op.result, rhs=tail_next_op.result, ip=replace_ip
            )
        if_op = scf_d.IfOp(
            put_cond.result,
            [IntegerType.get_signless(1, module.context)],
            has_else=True,
            ip=replace_ip,
        )
        # Then block (Not Full)
        then_ip = InsertionPoint(if_op.then_block)
        data = stream_access_op.data
        tail_index_op = index_d.CastUOp(
            output=IndexType.get(module.context), input=tail_val_op, ip=then_ip
        )
        if isinstance(data.type, MemRefType):
            element_type = data.type.element_type
            rank = data.type.rank
            for_ip = then_ip
            for_induction_vars = []
            for_ips = []
            for i in range(rank):
                dim_size = data.type.get_dim_size(i)
                for_loop_op = affine_d.AffineForOp(0, dim_size, ip=for_ip)
                for_induction_vars.append(for_loop_op.induction_variable)
                for_ip = InsertionPoint(for_loop_op.body)
                for_ips.append(for_ip)
            element_dim_map = AffineMap.get(
                dim_count=rank,
                symbol_count=0,
                exprs=[AffineExpr.get_dim(i) for i in range(rank)],
                context=module.context,
            )
            element_load_op = affine_d.AffineLoadOp(
                result=element_type,
                memref=data,
                indices=for_induction_vars,
                map=AffineMapAttr.get(element_dim_map),
                ip=for_ip,
            )
            memref_d.StoreOp(
                value=element_load_op,
                memref=fifo_ptr,
                indices=[tail_index_op] + for_induction_vars,
                ip=for_ip,
            )
            for ip in for_ips:
                affine_d.AffineYieldOp([], ip=ip)
        else:
            fifo_element_type = stream_type.element_type
            store_value = data
            if data.type != fifo_element_type:
                if (
                    isinstance(data.type, (IntegerType, IndexType))
                    and isinstance(fifo_element_type, (IntegerType, IndexType))
                ):
                    if isinstance(data.type, IndexType):
                        store_value = index_d.CastSOp(
                            fifo_element_type, data, ip=then_ip
                        )
                    elif isinstance(fifo_element_type, IndexType):
                        store_value = index_d.CastSOp(
                            IndexType.get(module.context), data, ip=then_ip
                        )
                    elif data.type.width > fifo_element_type.width:
                        store_value = arith_d.TruncIOp(
                            fifo_element_type, data, ip=then_ip
                        )
                    elif data.type.width < fifo_element_type.width:
                        if data.type.is_signed:
                            store_value = arith_d.ExtSIOp(
                                fifo_element_type, data, ip=then_ip
                            )
                        else:
                            store_value = arith_d.ExtUIOp(
                                fifo_element_type, data, ip=then_ip
                            )
            memref_d.StoreOp(
                value=store_value,
                memref=fifo_ptr,
                indices=[tail_index_op],
                ip=then_ip,
            )
        _stamp_put_ts(ts_ptr, tail_index_op, clock_arg, then_ip)
        _mark_occupied(free_ts_ptr, tail_index_op, clock_arg, module, then_ip)
        critical_op = openmp_d.CriticalOp(ip=then_ip)
        critical_ip = InsertionPoint(Block.create_at_start(critical_op.region))
        memref_d.StoreOp(tail_next_op, tail_ptr, [], ip=critical_ip)
        openmp_d.TerminatorOp(ip=critical_ip)
        openmp_d.FlushOp([], ip=then_ip)
        true_val = arith_d.ConstantOp(
            IntegerType.get_signless(1, module.context), 1, ip=then_ip
        )
        scf_d.YieldOp(results_=[true_val.result], ip=then_ip)
        # Else block (Full)
        else_ip = InsertionPoint(if_op.else_block)
        false_val = arith_d.ConstantOp(
            IntegerType.get_signless(1, module.context), 0, ip=else_ip
        )
        # Failed poll consumes a cycle so a spin-wait producer advances its clock
        # (this is what un-freezes the producer that deadlocked before).
        _tick_clock(clock_arg, module, else_ip)
        scf_d.YieldOp(results_=[false_val.result], ip=else_ip)
        stream_access_op.results[0].replace_all_uses_with(if_op.results[0])
        stream_access_op.operation.erase()
        return True
    if isinstance(stream_access_op, allo_d.StreamTryGetOp):
        openmp_d.FlushOp([], ip=replace_ip)
        # Phase 3 read barrier: spin only until the result is DECIDED (data present
        # AND produced by T, OR producer past T). Does not wait while data is
        # available, so it can't deadlock against a producer blocked on a full FIFO.
        if clock_arg is not None:
            t_val = memref_d.LoadOp(memref=clock_arg, indices=[], ip=replace_ip)
            _emit_read_barrier(
                head_ptr, tail_ptr, ts_ptr, prod_ptr, t_val, module, replace_ip
            )
            openmp_d.FlushOp([], ip=replace_ip)
        head_val_op = memref_d.LoadOp(memref=head_ptr, indices=[], ip=replace_ip)
        tail_val_op = memref_d.LoadOp(memref=tail_ptr, indices=[], ip=replace_ip)
        is_empty = arith_d.CmpIOp(
            0, lhs=head_val_op.result, rhs=tail_val_op.result, ip=replace_ip
        )
        is_not_empty = arith_d.CmpIOp(
            1, lhs=head_val_op.result, rhs=tail_val_op.result, ip=replace_ip
        )
        if clock_arg is not None:
            # available = not_empty AND ts_ring[head] <= T
            head_idx = index_d.CastUOp(
                output=IndexType.get(module.context), input=head_val_op, ip=replace_ip
            )
            ts_head = memref_d.LoadOp(memref=ts_ptr, indices=[head_idx], ip=replace_ip)
            ts_le = arith_d.CmpIOp(
                3, lhs=ts_head.result, rhs=t_val.result, ip=replace_ip
            )
            get_cond = arith_d.AndIOp(
                lhs=is_not_empty.result, rhs=ts_le.result, ip=replace_ip
            )
        else:
            get_cond = is_not_empty
        orig_got_val = stream_access_op.results[0]
        expected_type = orig_got_val.type
        if_op = scf_d.IfOp(
            get_cond.result,
            [expected_type, IntegerType.get_signless(1, module.context)],
            has_else=True,
            ip=replace_ip,
        )
        # Then block (Not Empty)
        then_ip = InsertionPoint(if_op.then_block)
        head_index_op = index_d.CastUOp(
            output=IndexType.get(module.context), input=head_val_op, ip=then_ip
        )
        head_inc_op = arith_d.AddIOp(
            lhs=head_val_op.result, rhs=const_one.result, ip=then_ip
        )
        head_next_op = arith_d.RemUIOp(
            lhs=head_inc_op.result, rhs=const_fifo_depth.result, ip=then_ip
        )
        if isinstance(expected_type, MemRefType):
            element_alloc_op = memref_d.AllocOp(
                memref=expected_type,
                dynamicSizes=[],
                symbolOperands=[],
                ip=then_ip,
            )
            rank = expected_type.rank
            for_ip = then_ip
            for_induction_vars = []
            for_ips = []
            for i in range(rank):
                for_loop_op = affine_d.AffineForOp(
                    0, expected_type.get_dim_size(i), ip=for_ip
                )
                for_induction_vars.append(for_loop_op.induction_variable)
                for_ip = InsertionPoint(for_loop_op.body)
                for_ips.append(for_ip)
            element_dim_map = AffineMap.get(
                dim_count=rank,
                symbol_count=0,
                exprs=[AffineExpr.get_dim(i) for i in range(rank)],
                context=module.context,
            )
            element_load_op = memref_d.LoadOp(
                memref=fifo_ptr,
                indices=[head_index_op] + for_induction_vars,
                ip=for_ip,
            )
            affine_d.AffineStoreOp(
                value=element_load_op,
                memref=element_alloc_op,
                indices=for_induction_vars,
                map=AffineMapAttr.get(element_dim_map),
                ip=for_ip,
            )
            for ip in for_ips:
                affine_d.AffineYieldOp([], ip=ip)
            data_val = element_alloc_op.result
        else:
            new_get_op = memref_d.LoadOp(
                memref=fifo_ptr, indices=[head_index_op], ip=then_ip
            )
            loaded_value = new_get_op.result
            if loaded_value.type != expected_type:
                if isinstance(loaded_value.type, IntegerType) and isinstance(
                    expected_type, IntegerType
                ):
                    if loaded_value.type.width < expected_type.width:
                        if loaded_value.type.is_signed:
                            loaded_value = arith_d.ExtSIOp(
                                expected_type, loaded_value, ip=then_ip
                            )
                        else:
                            loaded_value = arith_d.ExtUIOp(
                                expected_type, loaded_value, ip=then_ip
                            )
                    elif loaded_value.type.width > expected_type.width:
                        loaded_value = arith_d.TruncIOp(
                            expected_type, loaded_value, ip=then_ip
                        )
            data_val = loaded_value
        _advance_get_ts(ts_ptr, head_index_op, clock_arg, then_ip)
        _stamp_free_ts(free_ts_ptr, head_index_op, clock_arg, then_ip)
        critical_op = openmp_d.CriticalOp(ip=then_ip)
        critical_ip = InsertionPoint(Block.create_at_start(critical_op.region))
        memref_d.StoreOp(head_next_op, head_ptr, [], ip=critical_ip)
        openmp_d.TerminatorOp(ip=critical_ip)
        openmp_d.FlushOp([], ip=then_ip)
        true_val = arith_d.ConstantOp(
            IntegerType.get_signless(1, module.context), 1, ip=then_ip
        )
        scf_d.YieldOp(results_=[data_val, true_val.result], ip=then_ip)
        # Else block (Empty)
        else_ip = InsertionPoint(if_op.else_block)
        if isinstance(expected_type, MemRefType):
            dummy_data = memref_d.AllocOp(
                memref=expected_type,
                dynamicSizes=[],
                symbolOperands=[],
                ip=else_ip,
            )
            dummy_data_val = dummy_data.result
        elif isinstance(expected_type, IntegerType):
            dummy_data_val = arith_d.ConstantOp(expected_type, 0, ip=else_ip).result
        elif isinstance(expected_type, FloatType):
            dummy_data_val = arith_d.ConstantOp(
                expected_type, 0.0, ip=else_ip
            ).result
        else:
            raise NotImplementedError(
                f"Unsupported stream type for dummy data: {expected_type}"
            )
        false_val = arith_d.ConstantOp(
            IntegerType.get_signless(1, module.context), 0, ip=else_ip
        )
        # Failed poll consumes a cycle so a spin-wait consumer advances its clock.
        _tick_clock(clock_arg, module, else_ip)
        scf_d.YieldOp(results_=[dummy_data_val, false_val.result], ip=else_ip)
        stream_access_op.results[0].replace_all_uses_with(if_op.results[0])
        stream_access_op.results[1].replace_all_uses_with(if_op.results[1])
        stream_access_op.operation.erase()
        return True
    return False


def _process_function_streams(
    module: Module,
    func: func_d.FuncOp,
    processed_funcs: set,
    all_pe_calls_by_func: dict,
):
    """
    Process streams and PE calls within a single function.
    Returns (stream_struct_table, stream_type_table, pe_call_define_ops, stream_construct_ops)
    for use by the caller.
    """
    func_name = str(func.sym_name).strip('"')
    if func_name in processed_funcs:
        return {}, {}, {}, {}
    processed_funcs.add(func_name)

    if not isinstance(func.body, Region) or len(func.body.blocks) == 0:
        return {}, {}, {}, {}

    func_ops = func.body.blocks[0].operations
    pe_call_define_ops: dict[func_d.CallOp, func_d.FuncOp] = {}
    stream_construct_ops: dict[str, allo_d.StreamConstructOp] = {}

    # Collect PE calls and stream construct ops in this function.
    # The top-level scan handles direct (non-nested) calls and local stream
    # constructs, which is sufficient to identify top-level parallel PE calls.
    for op in func_ops:
        if isinstance(op, memref_d.AllocOp):
            continue
        if isinstance(op, func_d.CallOp):
            callee_name = str(op.callee)[1:]
            if not callee_name.startswith(("load_buf", "store_res")):
                for mod_op in module.body.operations:
                    if isinstance(mod_op, func_d.FuncOp):
                        if callee_name == str(mod_op.sym_name).strip('"'):
                            pe_call_define_ops[op] = mod_op
                            # Recursively process the callee function first
                            _process_function_streams(
                                module, mod_op, processed_funcs, all_pe_calls_by_func
                            )
                            break
        elif isinstance(op, allo_d.StreamConstructOp):
            stream_name = str(op.attributes["name"]).strip('"')
            stream_construct_ops[stream_name] = op

    # Deep scan: also reach func.call ops nested inside affine.for / scf.if /
    # other control-flow regions. Without this, a sub-region call like
    # ``inner(buf)`` placed inside ``for _ in range(N): inner(buf)`` is not
    # discovered by the top-level scan above, and the callee's own
    # ``allo.stream_put`` / ``allo.stream_get`` ops survive into LLVM
    # lowering -- ``convert-func-to-llvm`` then fails with
    # "cannot be converted to LLVM IR: missing LLVMTranslationDialectInterface
    # registration for dialect for op: func.func".
    #
    # We do not add nested calls to ``pe_call_define_ops`` here: nested
    # calls do not pass parent-region streams as call args (those would
    # have been visible at the top level), so there is no parent-side
    # arg-mapping to perform. We only need to ensure the callee gets
    # processed so its internal streams are lowered.
    nested_calls: list = []
    recursive_collect_ops(func, func_d.CallOp, nested_calls)
    for call_op in nested_calls:
        if call_op in pe_call_define_ops:
            continue
        callee_name = str(call_op.callee)[1:]
        if callee_name.startswith(("load_buf", "store_res", "usleep")):
            continue
        for mod_op in module.body.operations:
            if isinstance(mod_op, func_d.FuncOp):
                if callee_name == str(mod_op.sym_name).strip('"'):
                    _process_function_streams(
                        module, mod_op, processed_funcs, all_pe_calls_by_func
                    )
                    break

    # If no streams, nothing to do for this function
    if not stream_construct_ops:
        return {}, {}, pe_call_define_ops, {}

    # Construct Memref variables for pipes
    stream_struct_table: dict[str, OpResult] = {}  # stream name: stream struct
    stream_type_table: dict[str, MemRefType] = {}
    int_type = IntegerType.get_signless(32, module.context)
    memref_scalar_int_type = MemRefType.get([], int_type)
    empty_map = AffineMapAttr.get(AffineMap.get(0, 0, []))
    const_0_defined = False
    const_zero = None

    for stream_access_op in stream_construct_ops.values():
        stream_name = stream_access_op.attributes["name"]
        stream_type = allo_d.StreamType(stream_access_op.result.type)
        stream_item_type = stream_type.base_type
        stream_depth = stream_type.depth
        assert isinstance(stream_item_type, (MemRefType, IntegerType, FloatType))
        assert isinstance(stream_depth, int)
        ip = InsertionPoint(beforeOperation=stream_access_op)
        if isinstance(stream_item_type, MemRefType):
            item_element_type = stream_item_type.element_type
            if not isinstance(item_element_type, (IntegerType, FloatType)):
                raise NotImplementedError()
            memref_stream_type = MemRefType.get(
                shape=[stream_depth + 1] + stream_item_type.shape,
                element_type=item_element_type,
            )
        else:
            memref_stream_type = MemRefType.get(
                shape=[stream_depth + 1], element_type=stream_item_type
            )
        stream_memref_op = memref_d.AllocOp(memref_stream_type, [], [], ip=ip)
        stream_head_op = memref_d.AllocOp(memref_scalar_int_type, [], [], ip=ip)
        stream_tail_op = memref_d.AllocOp(memref_scalar_int_type, [], [], ip=ip)
        # Phase 2: parallel timestamp ring -- ts_ring[i] holds the producer's
        # simulated-time clock at the moment element i was put. Same length as
        # the data ring; i64 to match the per-PE clock.
        i64_type = IntegerType.get_signless(64, func.context)
        ts_ring_type = MemRefType.get([stream_depth + 1], i64_type)
        ts_ring_op = memref_d.AllocOp(ts_ring_type, [], [], ip=ip)
        # Phase 3 (D1): free-timestamp ring -- free_ts_ring[s] = the consumer's clock
        # when it freed slot s. Init to 0 so never-occupied slots read as "free".
        free_ts_ring_op = memref_d.AllocOp(ts_ring_type, [], [], ip=ip)
        _zero_i64 = arith_d.ConstantOp(i64_type, 0, ip=ip)
        _init_for = affine_d.AffineForOp(0, stream_depth + 1, ip=ip)
        _init_ip = InsertionPoint(_init_for.body)
        memref_d.StoreOp(
            _zero_i64, free_ts_ring_op, [_init_for.induction_variable], ip=_init_ip
        )
        affine_d.AffineYieldOp([], ip=_init_ip)
        # Phase 3 (A2): struct members 4/5 point at the producer's and consumer's
        # live PE clock cells, so the time-barrier (chunk C) can read the peer's
        # clock. Identify by which PE puts/gets this stream; fall back to a fresh
        # (inert) cell when the role can't be identified.
        i64_scalar_type = MemRefType.get([], i64_type)
        _prod_clk, _cons_clk = _stream_producer_consumer_clocks(
            stream_access_op, pe_call_define_ops
        )
        def _fallback_maxed_cell():
            # A cell pinned to INT64_MAX so a time-barrier on it never waits
            # (used when the producer/consumer role couldn't be identified).
            cell = memref_d.AllocOp(i64_scalar_type, [], [], ip=ip)
            maxc = arith_d.ConstantOp(i64_type, (1 << 63) - 1, ip=ip)
            memref_d.StoreOp(maxc, cell, [], ip=ip)
            return cell.result

        prod_clock_val = _prod_clk if _prod_clk is not None else _fallback_maxed_cell()
        cons_clock_val = _cons_clk if _cons_clk is not None else _fallback_maxed_cell()
        if not const_0_defined:
            const_zero = arith_d.ConstantOp(int_type, 0, ip=ip)
            const_0_defined = True
        memref_d.StoreOp(value=const_zero, memref=stream_head_op, indices=[], ip=ip)
        memref_d.StoreOp(value=const_zero, memref=stream_tail_op, indices=[], ip=ip)
        fifo_struct_type = allo_d.StructType.get(
            members=[
                memref_stream_type,
                memref_scalar_int_type,
                memref_scalar_int_type,
                ts_ring_type,
                i64_scalar_type,
                i64_scalar_type,
                ts_ring_type,
            ],
            context=func.context,
        )
        fifo_struct_op = allo_d.StructConstructOp(
            output=fifo_struct_type,
            input=[
                stream_memref_op, stream_head_op, stream_tail_op, ts_ring_op,
                prod_clock_val, cons_clock_val, free_ts_ring_op,
            ],
            ip=ip,
        )
        fifo_struct_memref_type = MemRefType.get([], fifo_struct_type)
        stream_memref_op = memref_d.AllocOp(fifo_struct_memref_type, [], [], ip=ip)
        stream_memref_op.attributes["name"] = stream_name
        affine_d.AffineStoreOp(
            value=fifo_struct_op,
            memref=stream_memref_op,
            indices=[],
            map=empty_map,
            ip=ip,
        )
        stream_name_str = str(stream_name).strip('"')
        stream_head_op.attributes["name"] = StringAttr.get(f"{stream_name_str}_head")
        stream_tail_op.attributes["name"] = StringAttr.get(f"{stream_name_str}_tail")
        stream_memref_op.attributes["name"] = stream_name
        stream_struct_table[stream_name_str] = stream_memref_op.result
        stream_type_table[stream_name_str] = memref_stream_type

    # Transform the stream operations in function calls
    for call_op, func_def_op in pe_call_define_ops.items():
        # Get the correspondence between arguments and passed pipes
        arg_stream_table: dict[BlockArgument, str] = {}  # arg: stream name
        assert isinstance(call_op.operands_, OpOperandList)
        assert isinstance(func_def_op.arguments, BlockArgumentList)
        assert len(call_op.operands_) == len(func_def_op.arguments)
        # 1. Update this call site and callee signature for all stream arguments
        for i in range(len(call_op.operands_)):
            arg_instance = call_op.operands_[i]
            for stream_name, stream_construct_op in stream_construct_ops.items():
                if Value(stream_construct_op.result) == arg_instance:
                    arg_def = func_def_op.arguments[i]
                    stream_memref = stream_struct_table[stream_name]
                    arg_stream_table[arg_def] = stream_name
                    # Change argument definitions
                    arg_def.set_type(stream_memref.type)
                    old_func_type = func_def_op.type
                    new_inputs = list(old_func_type.inputs)
                    new_inputs[arg_def.arg_number] = stream_memref.type
                    new_func_type = FunctionType.get(
                        inputs=new_inputs,
                        results=old_func_type.results,
                        context=old_func_type.context,
                    )
                    func_def_op.attributes["function_type"] = TypeAttr.get(
                        new_func_type, module.context
                    )
                    call_op.operands_[arg_def.arg_number] = stream_memref
        # Collect and replace `stream_get`s and `stream_put`s
        func_stream_ops = []
        recursive_collect_ops(
            func_def_op,
            (
                allo_d.StreamGetOp,
                allo_d.StreamPutOp,
                allo_d.StreamTryGetOp,
                allo_d.StreamTryPutOp,
                allo_d.StreamEmptyOp,
                allo_d.StreamFullOp,
            ),
            func_stream_ops,
        )
        for stream_access_op in func_stream_ops:
            assert isinstance(
                stream_access_op,
                (
                    allo_d.StreamGetOp,
                    allo_d.StreamPutOp,
                    allo_d.StreamTryGetOp,
                    allo_d.StreamTryPutOp,
                    allo_d.StreamEmptyOp,
                    allo_d.StreamFullOp,
                ),
            )
            replace_ip = InsertionPoint(beforeOperation=stream_access_op)
            # Have to leverage weak typing here
            stream = stream_access_op.stream
            # Check if this stream is a block argument (passed from caller)
            # If not (e.g., local stream_construct), skip it
            try:
                stream_arg = BlockArgument(stream)
            except ValueError:
                # Not a block argument, skip - will be handled elsewhere
                continue
            # Check if this stream is in our arg_stream_table
            if stream_arg not in arg_stream_table:
                continue
            stream_name = arg_stream_table[stream_arg]
            stream_type = stream_type_table[stream_name]
            stream_memref = stream_struct_table[stream_name]
            # FIFO access
            # Spin and wait for the FIFO to be not full
            assert isinstance(stream_memref.type, MemRefType)
            stream_struct = affine_d.AffineLoadOp(
                result=stream_memref.type.element_type,
                memref=stream_arg,
                indices=[],
                map=empty_map,
                ip=replace_ip,
            )
            head_ptr = allo_d.StructGetOp(
                output=memref_scalar_int_type,
                input=stream_struct,
                index=1,
                ip=replace_ip,
            )
            tail_ptr = allo_d.StructGetOp(
                output=memref_scalar_int_type,
                input=stream_struct,
                index=2,
                ip=replace_ip,
            )
            fifo_ptr = allo_d.StructGetOp(
                output=stream_type, input=stream_struct, index=0, ip=replace_ip
            )
            ts_ring_type = MemRefType.get(
                [stream_type.get_dim_size(0)],
                IntegerType.get_signless(64, module.context),
            )
            ts_ptr = allo_d.StructGetOp(
                output=ts_ring_type, input=stream_struct, index=3, ip=replace_ip
            )
            i64_scalar = MemRefType.get(
                [], IntegerType.get_signless(64, module.context)
            )
            prod_ptr = allo_d.StructGetOp(
                output=i64_scalar, input=stream_struct, index=4, ip=replace_ip
            )
            cons_ptr = allo_d.StructGetOp(
                output=i64_scalar, input=stream_struct, index=5, ip=replace_ip
            )
            free_ts_ptr = allo_d.StructGetOp(
                output=ts_ring_type, input=stream_struct, index=6, ip=replace_ip
            )
            clock_arg = _clock_of(func_def_op)
            const_one = arith_d.ConstantOp(int_type, 1, ip=replace_ip)
            const_fifo_depth = arith_d.ConstantOp(
                int_type, stream_type.get_dim_size(0), ip=replace_ip
            )
            if _lower_nb_stream_op(
                stream_access_op, head_ptr, tail_ptr, fifo_ptr,
                stream_type, const_one, const_fifo_depth, module, replace_ip,
                ts_ptr, clock_arg, prod_ptr, cons_ptr, free_ts_ptr,
            ):
                continue
            if isinstance(stream_access_op, allo_d.StreamPutOp):
                openmp_d.FlushOp([], ip=replace_ip)
                tail_val_op = memref_d.LoadOp(
                    memref=tail_ptr, indices=[], ip=replace_ip
                )

                tail_inc_op = arith_d.AddIOp(
                    lhs=tail_val_op.result, rhs=const_one.result, ip=replace_ip
                )
                tail_next_op = arith_d.RemUIOp(
                    lhs=tail_inc_op.result,
                    rhs=const_fifo_depth.result,
                    ip=replace_ip,
                )
            else:
                assert isinstance(stream_access_op, allo_d.StreamGetOp)
                head_val_op = memref_d.LoadOp(
                    memref=head_ptr, indices=[], ip=replace_ip
                )
                head_inc_op = arith_d.AddIOp(
                    lhs=head_val_op.result, rhs=const_one.result, ip=replace_ip
                )
                head_next_op = arith_d.RemUIOp(
                    lhs=head_inc_op.result,
                    rhs=const_fifo_depth.result,
                    ip=replace_ip,
                )
            spin_while_op = scf_d.WhileOp(results_=[], inits=[], ip=replace_ip)
            assert isinstance(spin_while_op.before, Region)
            assert isinstance(spin_while_op.after, Region)
            before_block = Block.create_at_start(
                parent=spin_while_op.before, arg_types=[]
            )
            before_ip = InsertionPoint(before_block)
            openmp_d.FlushOp([], ip=before_ip)
            after_block = Block.create_at_start(
                parent=spin_while_op.after, arg_types=[]
            )
            after_ip = InsertionPoint(after_block)
            openmp_d.TaskyieldOp(ip=after_ip)
            # Inject usleep(1) to prevent CPU starvation
            c1 = arith_d.ConstantOp(
                IntegerType.get_signless(32, module.context), 1, ip=after_ip
            )
            func_d.CallOp([], FlatSymbolRefAttr.get("usleep"), [c1], ip=after_ip)
            scf_d.YieldOp(results_=[], ip=after_ip)
            if isinstance(stream_access_op, allo_d.StreamPutOp):
                head_val_op = memref_d.LoadOp(memref=head_ptr, indices=[], ip=before_ip)
                cmp_op = arith_d.CmpIOp(
                    predicate=0, lhs=head_val_op, rhs=tail_next_op, ip=before_ip
                )
                # Blocked because the FIFO is full -> only the consumer can free it.
                spin_cond = _emit_deadlock_guard(
                    cmp_op.result, cons_ptr.result, _DL_ROLE_PUT, stream_name,
                    module, before_ip, after_block,
                    head_ptr, tail_ptr, const_fifo_depth.result,
                    spin_while_op, replace_ip,
                )
                scf_d.ConditionOp(condition=spin_cond, args=[], ip=before_ip)
                data = stream_access_op.data
                assert isinstance(data, Value)  # Vector or scalar
                tail_index_op = index_d.CastUOp(
                    output=IndexType.get(module.context),
                    input=tail_val_op,
                    ip=replace_ip,
                )
                if isinstance(data.type, MemRefType):  # Vector
                    # Data is an `alloc` pointer and should be loaded first
                    element_type = data.type.element_type
                    if not isinstance(element_type, (IntegerType, FloatType)):
                        # May get StructType involved in the future
                        raise NotImplementedError()
                    rank = data.type.rank
                    for_ip = replace_ip
                    for_induction_vars = []
                    for_ips: list[InsertionPoint] = (
                        []
                    )  # Reserved to insert affine.yield ops later
                    for i in range(rank):
                        dim_size = data.type.get_dim_size(i)
                        for_loop_op = affine_d.AffineForOp(0, dim_size, ip=for_ip)
                        for_induction_vars.append(for_loop_op.induction_variable)
                        for_ip = InsertionPoint(for_loop_op.body)
                        for_ips.append(for_ip)
                    element_dim_map = AffineMap.get(
                        dim_count=rank,
                        symbol_count=0,
                        exprs=[AffineExpr.get_dim(i) for i in range(rank)],
                        context=module.context,
                    )
                    element_load_op = affine_d.AffineLoadOp(
                        result=element_type,
                        memref=data,
                        indices=for_induction_vars,
                        map=AffineMapAttr.get(element_dim_map),
                        ip=for_ip,
                    )  # Fetch the element
                    memref_d.StoreOp(
                        value=element_load_op,
                        memref=fifo_ptr,
                        indices=[tail_index_op] + for_induction_vars,
                        ip=for_ip,
                    )  # Put the element to the stream
                    for ip in for_ips:
                        affine_d.AffineYieldOp([], ip=ip)
                else:  # Scalar
                    # Ensure data type matches the memref element type
                    fifo_element_type = stream_type.element_type
                    store_value = data
                    if data.type != fifo_element_type:
                        # Cast the data to match the expected element type
                        if isinstance(data.type, IntegerType) and isinstance(
                            fifo_element_type, IntegerType
                        ):
                            if data.type.width > fifo_element_type.width:
                                store_value = arith_d.TruncIOp(
                                    fifo_element_type, data, ip=replace_ip
                                )
                            elif data.type.width < fifo_element_type.width:
                                if data.type.is_signed:
                                    store_value = arith_d.ExtSIOp(
                                        fifo_element_type, data, ip=replace_ip
                                    )
                                else:
                                    store_value = arith_d.ExtUIOp(
                                        fifo_element_type, data, ip=replace_ip
                                    )
                    memref_d.StoreOp(
                        value=store_value,
                        memref=fifo_ptr,
                        indices=[tail_index_op],
                        ip=replace_ip,
                    )
                # Atomic update of tail
                _stamp_put_ts(ts_ptr, tail_index_op, clock_arg, replace_ip)
                _mark_occupied(free_ts_ptr, tail_index_op, clock_arg, module, replace_ip)
                critical_op = openmp_d.CriticalOp(ip=replace_ip)
                critical_ip = InsertionPoint(Block.create_at_start(critical_op.region))
                memref_d.StoreOp(tail_next_op, tail_ptr, [], ip=critical_ip)
                openmp_d.TerminatorOp(ip=critical_ip)
                openmp_d.FlushOp([], ip=replace_ip)
            else:
                assert isinstance(stream_access_op, allo_d.StreamGetOp)
                tail_val_op = memref_d.LoadOp(memref=tail_ptr, indices=[], ip=before_ip)
                cmp_op = arith_d.CmpIOp(
                    0, lhs=head_val_op, rhs=tail_val_op, ip=before_ip
                )
                # Blocked because the FIFO is empty -> only the producer can fill it.
                spin_cond = _emit_deadlock_guard(
                    cmp_op.result, prod_ptr.result, _DL_ROLE_GET, stream_name,
                    module, before_ip, after_block,
                    head_ptr, tail_ptr, const_fifo_depth.result,
                    spin_while_op, replace_ip,
                )
                scf_d.ConditionOp(condition=spin_cond, args=[], ip=before_ip)
                orig_got_val = stream_access_op.res
                assert isinstance(orig_got_val, OpResult)
                head_index_op = index_d.CastUOp(
                    output=IndexType.get(module.context),
                    input=head_val_op,
                    ip=replace_ip,
                )
                if isinstance(orig_got_val.type, MemRefType):
                    element_type = orig_got_val.type.element_type
                    if not isinstance(element_type, (IntegerType, FloatType)):
                        raise NotImplementedError()
                    rank = orig_got_val.type.rank
                    assert rank > 0
                    # Create a memref for the loaded element
                    element_alloc_op = memref_d.AllocOp(
                        memref=orig_got_val.type,
                        dynamicSizes=[],
                        symbolOperands=[],
                        ip=replace_ip,
                    )
                    orig_got_val.replace_all_uses_with(element_alloc_op.result)
                    # Create the element load/store loop
                    for_ip = replace_ip
                    for_induction_vars = []
                    for_ips: list[InsertionPoint] = []
                    for i in range(rank):
                        for_loop_op = affine_d.AffineForOp(
                            0,
                            orig_got_val.type.get_dim_size(i),
                            ip=for_ip,
                        )
                        for_induction_vars.append(for_loop_op.induction_variable)
                        for_ip = InsertionPoint(for_loop_op.body)
                        for_ips.append(for_ip)
                    element_dim_map = AffineMap.get(
                        dim_count=rank,
                        symbol_count=0,
                        exprs=[AffineExpr.get_dim(i) for i in range(rank)],
                        context=module.context,
                    )
                    element_load_op = memref_d.LoadOp(
                        memref=fifo_ptr,
                        indices=[head_index_op] + for_induction_vars,
                        ip=for_ip,  # The innermost Loop body
                    )
                    affine_d.AffineStoreOp(
                        value=element_load_op,
                        memref=element_alloc_op,
                        indices=for_induction_vars,
                        map=AffineMapAttr.get(element_dim_map),
                        ip=for_ip,
                    )
                    for ip in for_ips:
                        affine_d.AffineYieldOp([], ip=ip)
                else:  # Scalar
                    new_get_op = memref_d.LoadOp(
                        memref=fifo_ptr, indices=[head_index_op], ip=replace_ip
                    )
                    # Ensure loaded type matches the expected result type
                    loaded_value = new_get_op.result
                    expected_type = orig_got_val.type
                    if loaded_value.type != expected_type:
                        # Cast the loaded value to match the expected type
                        if isinstance(loaded_value.type, IntegerType) and isinstance(
                            expected_type, IntegerType
                        ):
                            if loaded_value.type.width < expected_type.width:
                                if loaded_value.type.is_signed:
                                    loaded_value = arith_d.ExtSIOp(
                                        expected_type, loaded_value, ip=replace_ip
                                    )
                                else:
                                    loaded_value = arith_d.ExtUIOp(
                                        expected_type, loaded_value, ip=replace_ip
                                    )
                            elif loaded_value.type.width > expected_type.width:
                                loaded_value = arith_d.TruncIOp(
                                    expected_type, loaded_value, ip=replace_ip
                                )
                    orig_got_val.replace_all_uses_with(loaded_value)
                _advance_get_ts(ts_ptr, head_index_op, clock_arg, replace_ip)
                _stamp_free_ts(free_ts_ptr, head_index_op, clock_arg, replace_ip)
                critical_op = openmp_d.CriticalOp(ip=replace_ip)
                critical_ip = InsertionPoint(Block.create_at_start(critical_op.region))
                memref_d.StoreOp(head_next_op, head_ptr, [], ip=critical_ip)
                openmp_d.TerminatorOp(ip=critical_ip)
            stream_access_op.operation.erase()

    # Also handle local stream operations within this function directly
    # (streams defined locally and used locally via stream_get/put, not passed to callees)
    local_stream_ops = []
    recursive_collect_ops(
        func,
        (
            allo_d.StreamGetOp,
            allo_d.StreamPutOp,
            allo_d.StreamTryGetOp,
            allo_d.StreamTryPutOp,
            allo_d.StreamEmptyOp,
            allo_d.StreamFullOp,
        ),
        local_stream_ops,
    )

    for stream_access_op in local_stream_ops:
        # Get the stream this op uses
        stream = stream_access_op.stream
        # Check if this stream is one of our locally-defined streams
        stream_name = None
        for sname, sop in stream_construct_ops.items():
            if Value(sop.result) == stream:
                stream_name = sname
                break
        if stream_name is None:
            continue  # Not a local stream we're processing
        if stream_name not in stream_struct_table:
            continue  # Stream wasn't processed (shouldn't happen)

        stream_type = stream_type_table[stream_name]
        stream_memref = stream_struct_table[stream_name]
        replace_ip = InsertionPoint(beforeOperation=stream_access_op)

        # FIFO access - transform the local stream operation
        assert isinstance(stream_memref.type, MemRefType)
        stream_struct = affine_d.AffineLoadOp(
            result=stream_memref.type.element_type,
            memref=stream_memref,
            indices=[],
            map=empty_map,
            ip=replace_ip,
        )
        head_ptr = allo_d.StructGetOp(
            output=memref_scalar_int_type,
            input=stream_struct,
            index=1,
            ip=replace_ip,
        )
        tail_ptr = allo_d.StructGetOp(
            output=memref_scalar_int_type,
            input=stream_struct,
            index=2,
            ip=replace_ip,
        )
        fifo_ptr = allo_d.StructGetOp(
            output=stream_type, input=stream_struct, index=0, ip=replace_ip
        )
        ts_ring_type = MemRefType.get(
            [stream_type.get_dim_size(0)],
            IntegerType.get_signless(64, module.context),
        )
        ts_ptr = allo_d.StructGetOp(
            output=ts_ring_type, input=stream_struct, index=3, ip=replace_ip
        )
        i64_scalar = MemRefType.get(
            [], IntegerType.get_signless(64, module.context)
        )
        prod_ptr = allo_d.StructGetOp(
            output=i64_scalar, input=stream_struct, index=4, ip=replace_ip
        )
        cons_ptr = allo_d.StructGetOp(
            output=i64_scalar, input=stream_struct, index=5, ip=replace_ip
        )
        free_ts_ptr = allo_d.StructGetOp(
            output=ts_ring_type, input=stream_struct, index=6, ip=replace_ip
        )
        clock_arg = _clock_of(func)
        const_one = arith_d.ConstantOp(int_type, 1, ip=replace_ip)
        const_fifo_depth = arith_d.ConstantOp(
            int_type, stream_type.get_dim_size(0), ip=replace_ip
        )
        if _lower_nb_stream_op(
            stream_access_op, head_ptr, tail_ptr, fifo_ptr,
            stream_type, const_one, const_fifo_depth, module, replace_ip,
            ts_ptr, clock_arg, prod_ptr, cons_ptr, free_ts_ptr,
        ):
            continue
        if isinstance(stream_access_op, allo_d.StreamPutOp):
            openmp_d.FlushOp([], ip=replace_ip)
            tail_val_op = memref_d.LoadOp(memref=tail_ptr, indices=[], ip=replace_ip)
            tail_inc_op = arith_d.AddIOp(
                lhs=tail_val_op.result, rhs=const_one.result, ip=replace_ip
            )
            tail_next_op = arith_d.RemUIOp(
                lhs=tail_inc_op.result,
                rhs=const_fifo_depth.result,
                ip=replace_ip,
            )
            spin_while_op = scf_d.WhileOp(results_=[], inits=[], ip=replace_ip)
            assert isinstance(spin_while_op.before, Region)
            assert isinstance(spin_while_op.after, Region)
            before_block = Block.create_at_start(
                parent=spin_while_op.before, arg_types=[]
            )
            before_ip = InsertionPoint(before_block)
            openmp_d.FlushOp([], ip=before_ip)
            after_block = Block.create_at_start(
                parent=spin_while_op.after, arg_types=[]
            )
            after_ip = InsertionPoint(after_block)
            openmp_d.TaskyieldOp(ip=after_ip)
            # Inject usleep(1) to prevent CPU starvation
            c1 = arith_d.ConstantOp(
                IntegerType.get_signless(32, module.context), 1, ip=after_ip
            )
            func_d.CallOp([], FlatSymbolRefAttr.get("usleep"), [c1], ip=after_ip)
            scf_d.YieldOp(results_=[], ip=after_ip)
            head_val_op = memref_d.LoadOp(memref=head_ptr, indices=[], ip=before_ip)
            cmp_op = arith_d.CmpIOp(
                predicate=0, lhs=head_val_op, rhs=tail_next_op, ip=before_ip
            )
            # Blocked because the FIFO is full -> only the consumer can free it.
            spin_cond = _emit_deadlock_guard(
                cmp_op.result, cons_ptr.result, _DL_ROLE_PUT, stream_name,
                module, before_ip, after_block,
                head_ptr, tail_ptr, const_fifo_depth.result,
                spin_while_op, replace_ip,
            )
            scf_d.ConditionOp(condition=spin_cond, args=[], ip=before_ip)
            data = stream_access_op.data
            assert isinstance(data, Value)
            tail_index_op = index_d.CastUOp(
                output=IndexType.get(module.context),
                input=tail_val_op,
                ip=replace_ip,
            )
            if isinstance(data.type, MemRefType):
                element_type = data.type.element_type
                if not isinstance(element_type, (IntegerType, FloatType)):
                    raise NotImplementedError()
                rank = data.type.rank
                for_ip = replace_ip
                for_induction_vars = []
                for_ips: list[InsertionPoint] = []
                for i in range(rank):
                    dim_size = data.type.get_dim_size(i)
                    for_loop_op = affine_d.AffineForOp(0, dim_size, ip=for_ip)
                    for_induction_vars.append(for_loop_op.induction_variable)
                    for_ip = InsertionPoint(for_loop_op.body)
                    for_ips.append(for_ip)
                element_dim_map = AffineMap.get(
                    dim_count=rank,
                    symbol_count=0,
                    exprs=[AffineExpr.get_dim(i) for i in range(rank)],
                    context=module.context,
                )
                element_load_op = affine_d.AffineLoadOp(
                    result=element_type,
                    memref=data,
                    indices=for_induction_vars,
                    map=AffineMapAttr.get(element_dim_map),
                    ip=for_ip,
                )
                memref_d.StoreOp(
                    value=element_load_op,
                    memref=fifo_ptr,
                    indices=[tail_index_op] + for_induction_vars,
                    ip=for_ip,
                )
                for ip in for_ips:
                    affine_d.AffineYieldOp([], ip=ip)
            else:
                fifo_element_type = stream_type.element_type
                store_value = data
                if data.type != fifo_element_type:
                    if isinstance(data.type, IntegerType) and isinstance(
                        fifo_element_type, IntegerType
                    ):
                        if data.type.width > fifo_element_type.width:
                            store_value = arith_d.TruncIOp(
                                fifo_element_type, data, ip=replace_ip
                            )
                        elif data.type.width < fifo_element_type.width:
                            if data.type.is_signed:
                                store_value = arith_d.ExtSIOp(
                                    fifo_element_type, data, ip=replace_ip
                                )
                            else:
                                store_value = arith_d.ExtUIOp(
                                    fifo_element_type, data, ip=replace_ip
                                )
                memref_d.StoreOp(
                    value=store_value,
                    memref=fifo_ptr,
                    indices=[tail_index_op],
                    ip=replace_ip,
                )
            _stamp_put_ts(ts_ptr, tail_index_op, clock_arg, replace_ip)
            _mark_occupied(free_ts_ptr, tail_index_op, clock_arg, module, replace_ip)
            critical_op = openmp_d.CriticalOp(ip=replace_ip)
            critical_ip = InsertionPoint(Block.create_at_start(critical_op.region))
            memref_d.StoreOp(tail_next_op, tail_ptr, [], ip=critical_ip)
            openmp_d.TerminatorOp(ip=critical_ip)
            openmp_d.FlushOp([], ip=replace_ip)
        else:
            assert isinstance(stream_access_op, allo_d.StreamGetOp)
            head_val_op = memref_d.LoadOp(memref=head_ptr, indices=[], ip=replace_ip)
            head_inc_op = arith_d.AddIOp(
                lhs=head_val_op.result, rhs=const_one.result, ip=replace_ip
            )
            head_next_op = arith_d.RemUIOp(
                lhs=head_inc_op.result,
                rhs=const_fifo_depth.result,
                ip=replace_ip,
            )
            spin_while_op = scf_d.WhileOp(results_=[], inits=[], ip=replace_ip)
            assert isinstance(spin_while_op.before, Region)
            assert isinstance(spin_while_op.after, Region)
            before_block = Block.create_at_start(
                parent=spin_while_op.before, arg_types=[]
            )
            before_ip = InsertionPoint(before_block)
            openmp_d.FlushOp([], ip=before_ip)
            after_block = Block.create_at_start(
                parent=spin_while_op.after, arg_types=[]
            )
            after_ip = InsertionPoint(after_block)
            openmp_d.TaskyieldOp(ip=after_ip)
            # Inject usleep(1) to prevent CPU starvation
            c1 = arith_d.ConstantOp(
                IntegerType.get_signless(32, module.context), 1, ip=after_ip
            )
            func_d.CallOp([], FlatSymbolRefAttr.get("usleep"), [c1], ip=after_ip)
            scf_d.YieldOp(results_=[], ip=after_ip)
            tail_val_op = memref_d.LoadOp(memref=tail_ptr, indices=[], ip=before_ip)
            cmp_op = arith_d.CmpIOp(0, lhs=head_val_op, rhs=tail_val_op, ip=before_ip)
            # Blocked because the FIFO is empty -> only the producer can fill it.
            spin_cond = _emit_deadlock_guard(
                cmp_op.result, prod_ptr.result, _DL_ROLE_GET, stream_name,
                module, before_ip, after_block,
                head_ptr, tail_ptr, const_fifo_depth.result,
                spin_while_op, replace_ip,
            )
            scf_d.ConditionOp(condition=spin_cond, args=[], ip=before_ip)
            orig_got_val = stream_access_op.res
            assert isinstance(orig_got_val, OpResult)
            head_index_op = index_d.CastUOp(
                output=IndexType.get(module.context),
                input=head_val_op,
                ip=replace_ip,
            )
            if isinstance(orig_got_val.type, MemRefType):
                element_type = orig_got_val.type.element_type
                if not isinstance(element_type, (IntegerType, FloatType)):
                    raise NotImplementedError()
                rank = orig_got_val.type.rank
                assert rank > 0
                element_alloc_op = memref_d.AllocOp(
                    memref=orig_got_val.type,
                    dynamicSizes=[],
                    symbolOperands=[],
                    ip=replace_ip,
                )
                orig_got_val.replace_all_uses_with(element_alloc_op.result)
                for_ip = replace_ip
                for_induction_vars = []
                for_ips: list[InsertionPoint] = []
                for i in range(rank):
                    for_loop_op = affine_d.AffineForOp(
                        0, orig_got_val.type.get_dim_size(i), ip=for_ip
                    )
                    for_induction_vars.append(for_loop_op.induction_variable)
                    for_ip = InsertionPoint(for_loop_op.body)
                    for_ips.append(for_ip)
                element_dim_map = AffineMap.get(
                    dim_count=rank,
                    symbol_count=0,
                    exprs=[AffineExpr.get_dim(i) for i in range(rank)],
                    context=module.context,
                )
                element_load_op = memref_d.LoadOp(
                    memref=fifo_ptr,
                    indices=[head_index_op] + for_induction_vars,
                    ip=for_ip,
                )
                affine_d.AffineStoreOp(
                    value=element_load_op,
                    memref=element_alloc_op,
                    indices=for_induction_vars,
                    map=AffineMapAttr.get(element_dim_map),
                    ip=for_ip,
                )
                for ip in for_ips:
                    affine_d.AffineYieldOp([], ip=ip)
            else:
                new_get_op = memref_d.LoadOp(
                    memref=fifo_ptr, indices=[head_index_op], ip=replace_ip
                )
                loaded_value = new_get_op.result
                expected_type = orig_got_val.type
                if loaded_value.type != expected_type:
                    if isinstance(loaded_value.type, IntegerType) and isinstance(
                        expected_type, IntegerType
                    ):
                        if loaded_value.type.width < expected_type.width:
                            if loaded_value.type.is_signed:
                                loaded_value = arith_d.ExtSIOp(
                                    expected_type, loaded_value, ip=replace_ip
                                )
                            else:
                                loaded_value = arith_d.ExtUIOp(
                                    expected_type, loaded_value, ip=replace_ip
                                )
                        elif loaded_value.type.width > expected_type.width:
                            loaded_value = arith_d.TruncIOp(
                                expected_type, loaded_value, ip=replace_ip
                            )
                orig_got_val.replace_all_uses_with(loaded_value)
            _advance_get_ts(ts_ptr, head_index_op, clock_arg, replace_ip)
            _stamp_free_ts(free_ts_ptr, head_index_op, clock_arg, replace_ip)
            critical_op = openmp_d.CriticalOp(ip=replace_ip)
            critical_ip = InsertionPoint(Block.create_at_start(critical_op.region))
            memref_d.StoreOp(head_next_op, head_ptr, [], ip=critical_ip)
            openmp_d.TerminatorOp(ip=critical_ip)
        stream_access_op.operation.erase()

    # Erase stream construct ops for this function
    for op in stream_construct_ops.values():
        op.operation.erase()

    # Accumulate PE calls keyed by function for recursive OMP injection
    if pe_call_define_ops:
        all_pe_calls_by_func[func_name] = pe_call_define_ops

    return (
        stream_struct_table,
        stream_type_table,
        pe_call_define_ops,
        stream_construct_ops,
    )


def _inject_omp_parallel_sections(pe_call_define_ops):
    """Wrap a set of func.call ops in omp.parallel > omp.sections > omp.section blocks."""
    assert len(pe_call_define_ops) > 0
    omp_ip = InsertionPoint(beforeOperation=list(pe_call_define_ops.keys())[0])
    omp_parallel_op = openmp_d.ParallelOp([], [], [], [], ip=omp_ip)
    assert isinstance(omp_parallel_op.region, Region)
    omp_parallel_block = Block.create_at_start(omp_parallel_op.region, [])

    # Add `omp.sections`
    ip_omp_parallel = InsertionPoint(omp_parallel_block)
    omp_sections_op = openmp_d.SectionsOp([], [], [], [], ip=ip_omp_parallel)
    omp_sections_block = Block.create_at_start(omp_sections_op.region, [])
    openmp_d.TerminatorOp(ip=ip_omp_parallel)

    # Add `omp.section`s for PE calls
    ip_omp_sections = InsertionPoint(omp_sections_block)
    for call_op in pe_call_define_ops:
        assert isinstance(call_op, OpView)
        omp_section_op = openmp_d.SectionOp(ip=ip_omp_sections)
        omp_section_block = Block.create_at_start(omp_section_op.region, [])
        ip_omp_section = InsertionPoint(omp_section_block)
        omp_term_op = openmp_d.TerminatorOp(ip=ip_omp_section)
        call_op.operation.move_before(omp_term_op.operation)
    openmp_d.TerminatorOp(ip=ip_omp_sections)


def build_dataflow_simulator(module: Module, top_func_name: str):
    # Enable nested OpenMP parallelism so that peer kernels calling
    # sub-regions (which have their own omp.parallel/sections) don't
    # deadlock.  This is safe because the simulator already controls
    # thread counts via omp.sections.
    if os.environ.get("OMP_MAX_ACTIVE_LEVELS") is None:
        os.environ["OMP_MAX_ACTIVE_LEVELS"] = "4"
    with module.context, Location.unknown():
        # Declare usleep for spinloop yielding
        found_usleep = False
        for op in module.body.operations:
            if (
                isinstance(op, func_d.FuncOp)
                and op.attributes["sym_name"].value == "usleep"
            ):
                found_usleep = True
                break
        if not found_usleep:
            usleep_type = FunctionType.get(
                [IntegerType.get_signless(32, module.context)],
                [],
            )
            # pylint: disable=unexpected-keyword-arg
            usleep_op = func_d.FuncOp(
                name="usleep",
                type=usleep_type,
                ip=InsertionPoint(module.body),
            )
            usleep_op.attributes["sym_visibility"] = StringAttr.get("private")

        # Process all functions with streams recursively, starting from top
        processed_funcs: set = set()
        all_pe_calls_by_func: dict = {}
        func = find_func_in_module(module, top_func_name)
        assert isinstance(func.body, Region)

        # Phase 1/2 reorder: thread a per-PE clock arg through the calls BEFORE
        # stream lowering, so put/get lowering can stamp/advance the clock.
        clock_cells = _add_pe_clock_args(module, top_func_name)

        # Declare the read-out global now: stream lowering below emits
        # `memref.get_global` against it when it generates the deadlock checks.
        _SIM_CTX["n_pes"] = len(clock_cells)
        _SIM_CTX["stream_ids"] = {}
        if clock_cells:
            _declare_sim_global(module, len(clock_cells))

        # Charge the clock BEFORE stream lowering, while the IR still holds only
        # user-level ops.  After lowering, a PE body is ~90% simulator plumbing
        # (allo.struct_get, ring-buffer head/tail loads, omp.flush/critical), which
        # has no hardware cost -- charging it made the cycle count mostly overhead.
        # Blocks that lowering creates later (the try_* scf.if arms, and the spin-wait
        # scf.while bodies) get no increment, which is exactly what the old
        # _collect_blocks scf.while skip hand-coded.
        _insert_pe_clock_increments(module)

        # Recursively process the top function and all its callees
        _, _, pe_call_define_ops, _ = _process_function_streams(
            module, func, processed_funcs, all_pe_calls_by_func
        )

        # If no PE calls were found in top function, collect them again from the processed functions
        if not pe_call_define_ops:
            top_func_ops = func.body.blocks[0].operations
            for op in top_func_ops:
                if isinstance(op, func_d.CallOp):
                    callee_name = str(op.callee)[1:]
                    if not callee_name.startswith(("load_buf", "store_res")):
                        for mod_op in module.body.operations:
                            if isinstance(mod_op, func_d.FuncOp):
                                if callee_name == str(mod_op.sym_name).strip('"'):
                                    pe_call_define_ops[op] = mod_op
                                    break
            all_pe_calls_by_func[top_func_name] = pe_call_define_ops

        # Inject omp.parallel/sections into every function that has PE calls
        for func_pe_calls in all_pe_calls_by_func.values():
            if func_pe_calls:
                _inject_omp_parallel_sections(func_pe_calls)

        # Cycle read-out: must come AFTER the omp injection -- the harvest loads each
        # PE's final clock just before the caller returns, and omp.parallel is the
        # join, so before it the clocks are still being written by live threads.
        return _emit_cycle_harvest(module, clock_cells)


# This pass is only meant to run on fully lowered MLIR code
# Note: OpenMP operations in lowered IR are not the original operation types anymore
def convert_critical_write_to_atomic_write(module: Module):
    with module.context, Location.unknown():
        omp_critical_ops = []
        for op in module.body:
            if not isinstance(op, llvm_d.LLVMFuncOp):
                continue
            recursive_collect_ops_by_name(op, "omp.critical", omp_critical_ops)
        for critical_op in omp_critical_ops:
            # Transform a critical area with only the store op and omp.terminator
            assert isinstance(critical_op.regions, RegionSequence)
            if len(critical_op.regions) != 1:
                continue
            region = critical_op.regions[0]
            if len(region.blocks) != 1:
                continue
            block = region.blocks[0]
            if len(block.operations) != 2:
                continue
            if (
                not isinstance(block.operations[0], llvm_d.StoreOp)
                or block.operations[1].name != "omp.terminator"
            ):
                continue
            store_op = block.operations[0]
            assert isinstance(store_op, llvm_d.StoreOp)
            store_ip = InsertionPoint(critical_op)
            openmp_d.AtomicWriteOp(x=store_op.addr, expr=store_op.value, ip=store_ip)
            critical_op.operation.erase()


class SimCycles:
    """Per-PE simulated cycle counts plus the makespan (max over PEs)."""

    def __init__(self, per_pe):
        self.per_pe = per_pe
        self.makespan = max(per_pe.values()) if per_pe else 0

    def __repr__(self):
        return f"SimCycles(makespan={self.makespan}, pes={len(self.per_pe)})"


class LLVMOMPModule(LLVMModule):
    def __init__(self, mod: Module, top_func_name: str, ext_libs=None):
        with Context() as ctx:
            allo_d.register_dialect(ctx)
            self.module = Module.parse(str(mod), ctx)
            self.top_func_name = top_func_name
            func = find_func_in_module(self.module, top_func_name)
            ext_libs = [] if ext_libs is None else ext_libs
            # Get input/output types
            self.in_types, self.out_types = get_func_inputs_outputs(func)
            self.module = decompose_library_function(self.module)

            self.sim_pe_names = build_dataflow_simulator(
                self.module, self.top_func_name
            )
            # Snapshot this build's id->stream-name map while _SIM_CTX still holds it,
            # so a deadlock report can name the stream rather than print a bare index.
            self.sim_stream_names = {
                i: nm for nm, i in _SIM_CTX["stream_ids"].items()
            }
            # Attach necessary attributes
            func = find_func_in_module(self.module, top_func_name)
            if func is None:
                raise RuntimeError(
                    "No top-level function found in the built MLIR module"
                )
            func.attributes["llvm.emit_c_interface"] = UnitAttr.get()
            func.attributes["top"] = UnitAttr.get()

            # Start lowering
            # Lower linalg for AIE
            pm = PassManager.parse(
                "builtin.module("
                "one-shot-bufferize,"
                "expand-strided-metadata,"
                "func.func(convert-linalg-to-affine-loops)"
                ")"
            )
            pm.run(self.module.operation)
            # Lower StructType
            allo_d.lower_composite_type(self.module)
            # Lower bit ops
            allo_d.lower_bit_ops(self.module)
            # Reference: https://discourse.llvm.org/t/help-lowering-affine-loop-to-openmp/72441/9
            pm = PassManager.parse(
                "builtin.module("
                "lower-affine,"
                "convert-scf-to-cf,"
                "finalize-memref-to-llvm,"
                "convert-func-to-llvm,"
                "convert-index-to-llvm,"
                "convert-arith-to-llvm,"
                "convert-cf-to-llvm,"
                "convert-openmp-to-llvm,"
                "canonicalize"
                ")"
            )
            pm.run(self.module.operation)
            convert_critical_write_to_atomic_write(self.module)

            assert os.getenv("LLVM_BUILD_DIR") is not None, "LLVM_BUILD_DIR is not set"
            shared_libs = [
                os.path.join(
                    os.getenv("LLVM_BUILD_DIR"), "lib", "libmlir_runner_utils.so"
                ),
                os.path.join(
                    os.getenv("LLVM_BUILD_DIR"), "lib", "libmlir_c_runner_utils.so"
                ),
                os.path.join(os.getenv("LLVM_BUILD_DIR"), "lib", "libomp.so"),
            ]
            shared_libs += [lib.compile_shared_lib() for lib in ext_libs]
            self.execution_engine = ExecutionEngine(
                self.module, opt_level=2, shared_libs=shared_libs
            )

    def __call__(self, *args):
        """Run, then surface a deadlock as an exception instead of returning the
        garbage the unwound spin loops leave behind."""
        result = super().__call__(*args)
        self._raise_if_deadlocked()
        return result

    def _raise_if_deadlocked(self):
        """Raise if the run ended because a PE was blocked on a stream whose peer had
        already finished.  Detection happens in the spin loops (_emit_deadlock_guard);
        this only reports it.  Called after every run -- the flag reads 0 otherwise."""
        names, buf = self._read_sim_buf()
        if buf is None or int(buf[len(names)]) == 0:
            return
        n = len(names)
        sid, role, cause = int(buf[n + 1]), int(buf[n + 2]), int(buf[n + 5])
        stream = getattr(self, "sim_stream_names", {}).get(sid, f"#{sid}")
        verb = "put to (FIFO full)" if role == _DL_ROLE_PUT else "get from (FIFO empty)"
        if cause == _DL_CAUSE_CIRCULAR:
            msg = (
                f"deadlock (circular wait): every PE was blocked or finished for "
                f"{_DL_POLLS} consecutive polls with no progress anywhere.  The "
                f"reporting PE was blocked trying to {verb} stream '{stream}' -- it "
                f"is one participant, not necessarily the root cause."
            )
        else:
            msg = (
                f"deadlock: a PE blocked trying to {verb} stream '{stream}', but the "
                f"peer PE on that stream had already finished, so it can never unblock"
            )
        err = DeadlockError(msg)
        err.detail = {
            "stream": stream, "stream_id": sid, "role": role,
            "cause": "circular" if cause == _DL_CAUSE_CIRCULAR else "peer_done",
        }
        raise err

    def _read_sim_buf(self):
        """Copy the read-out global out once: `n` cycle slots then [flag, stream, role].
        Returns (pe_names, buffer), or (names, None) if there is nothing to read."""
        names = getattr(self, "sim_pe_names", None) or []
        if len(names) == 0:
            return names, None
        buf = np.zeros(len(names) + _DL_SLOTS, dtype=np.int64)
        self.execution_engine.invoke(
            SIM_CYCLES_READER,
            ctypes.pointer(ctypes.pointer(get_ranked_memref_descriptor(buf))),
        )
        return names, buf

    def get_cycles(self):
        """Simulated cycle counts from the most recent run, under the per-PE clock's
        cost model (see _op_latency).  Returns a SimCycles: `.per_pe` maps each PE to
        its final clock, `.makespan` is the max over PEs -- the number to score a
        design with.  These are *abstract* cycles: correct in ordering, only
        approximate in magnitude.  Returns an empty result if the design has no
        clocked PEs, or if called before the first run."""
        names, buf = self._read_sim_buf()
        if buf is None:
            return SimCycles({})
        # Disambiguate repeated kernel names (mapping=[...] gives one func, many calls).
        per_pe = {}
        seen = {}
        for name, cycles in zip(names, buf.tolist()):
            k = seen.get(name, 0)
            seen[name] = k + 1
            per_pe[name if k == 0 else f"{name}#{k}"] = cycles
        return SimCycles(per_pe)
