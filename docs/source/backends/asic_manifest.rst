..  Copyright Allo authors. All Rights Reserved.
    SPDX-License-Identifier: Apache-2.0

###################################
The ASIC manifest
###################################

``allo/backend/asic`` (see its ``PROVENANCE.md``) contains the maintained
hierarchical Allo ASIC flow. Its ``allo-*`` compilation node consumes
Allo-level architecture, rather than a hand-written hardware manifest: it
loads an Allo design and *calls*
``build(project, target, mode, configs)`` with
``configs["asic_manifest"] = {...}``: Allo is asked to emit, as part of
compilation, the architecture it just compiled. Downstream planners then place
macros on a grid from PID row/column deltas, weight the wires between them by
**pre-HLS stream bit width**, and decide which instances may share a macro from
the realized MLIR and emitted-HLS implementation contract rather than generated
names. None of the compiler identity survives in emitted Verilog, so nothing
later in the flow can recover it or check it.

The older flat RTL nodes are archived under ``nodes/legacy-flat-flow/`` for
historical TinyTPU constructors. They consume emitted RTL and a file list and
do not participate in the manifest contract documented here.

Until 2026-09-24 this fork did not emit that manifest, which is why the TinyTPU
runs were routed around the compilation node. :mod:`allo.backend.asic_manifest`
is the emitter.

.. important::

   Emitting the manifest is not by itself physical signoff. The fixed 4-by-4
   Vitis pipe cleaner has now exercised the manifest through licensed HLS,
   macro hardening, full-chip implementation, functional gate simulation,
   back-annotated functional simulation, DRC, and LVS. That run validates the
   integration contract, while its separately reported antenna, SDF-coverage,
   and BAGL runtime timing-check issues remain open signoff limitations.


What is emitted, and when
=========================

Two files, matching what ``nodes/allo-asic-compilation/validate_build.py``
checks for, each with a ``.tcl`` sibling because the flow's nodes stage both:
the Tcl sibling assigns the complete same schema to ``allo_asic_manifest``
using only built-in ``dict`` and ``list`` commands.

``asic-manifest.json`` — ``stage: "pre_hls"``
    The architecture. Written inside ``allo.dataflow.build()``, from the
    schedule that is about to be handed to the backend, **before any HLS tool
    runs** — so it exists whether or not a licence does.

``asic-manifest-final.json`` — ``stage: "post_hls_enriched"``
    The same records joined through backend-specific emitted source and
    synthesis evidence to the RTL hierarchy the backend produced. Written from
    :class:`~allo.backend.hls.HLSModule` once ``csyn`` has succeeded, because
    only then do the generated process, module, and interface names exist.

``asic-debug/`` holds flat TSV views plus the exact canonical MLIR, emitted-HLS,
and RTL strings consumed by the recorded hashes.

Nothing happens unless ``configs["asic_manifest"]["enabled"]`` is set, so
ordinary non-ASIC builds are unchanged.

.. code-block:: python

   import allo.dataflow as df

   df.build(
       design,
       target="vitis_hls",
       mode="csyn",
       project="top.prj",
       configs={
           "frequency": 300.0,
           "device": "u280",
           "asic_manifest": {
               "enabled": True,
               "path": "asic-manifest.json",
               "debug_artifacts": True,
               "debug_dir": "asic-debug",
           },
       },
   )

That ``configs`` dict is not an invention of this page: it is verbatim what
``nodes/allo-asic-compilation/backend.py`` builds and passes in.


The schema, as the consumers define it
======================================

Schema version 2 is emitted by :mod:`allo.backend.asic_manifest`; its validator
and downstream readers are the normative contract. The fields below summarize
the fields those consumers require rather than defining a second independent
schema.

Envelope
--------

``stage``
    ``"pre_hls"`` / ``"post_hls_enriched"``. ``validate_build`` rejects
    anything else.
``top``
    The RTL top module name. ``plan_assembly`` requires it to be both the
    node's ``top_module`` parameter and a module present in ``design.v``.
``backend``
    ``vitis`` / ``catapult`` / ``systemc``; ``plan_macros`` and
    ``plan_assembly`` refuse a mismatch with their own parameter.
``summary``
    ``unmatched_or_ambiguous`` and ``unjoined_post_hls_records`` **must both
    be 0**, checked twice (``validate_build``, ``plan_macros``).
``top_arguments[]``
    Order is load-bearing: it must equal the frozen workload's
    ``call_signature``. Each entry carries ``ordinal``, ``name``, ``shape``,
    ``type``, ``direction``.

``pe_instances[]``
------------------

``semantic_id``
    ``<top>/<kernel>/pid=<i>[,<j>]``. The format is fixed by
    ``plan_physical_intent``: it takes the first two ``/``-segments as the
    kernel key and regex-matches ``pid=([0-9,-]+)``. Must be unique --
    several consumers key a dict on it with a bare ``[]`` lookup.
``kernel``
    The bare kernel name (*not* ``top/kernel``).
``pid``
    List of ints. ``plan_macros`` turns a +-1 delta on the last axis into
    ``E``/``W`` and on the second-to-last into ``S``/``N``; the row index
    increases **southward**.
``ports[]``
    The stream interface, in a dense 0-based ``ordinal`` order, each with
    ``channel_id``, ``stream``, ``direction`` (``in``/``out``) and ``type``.
    The flow zips this positionally against the RTL's handshake bundles, so an
    ``m_axi`` memory argument is **not** one of them.
``post_hls_records[]``
    Post-HLS only: the emitted-HLS function/interface hash, backend-specific
    join evidence, synthesis contract, ``rtl_root_module``, dependency closure,
    realized hierarchy paths, and ``rtl_equivalence_hash``. Vitis records carry
    process/legalization evidence; SystemC records carry the joined emitted
    ``SC_MODULE`` and realized Catapult interface evidence.

``channels[]``
--------------

``channel_id`` and ``stream``
    Provenance; ``ports[].channel_id`` joins to it, and a ``null`` there means
    a SystemC-internal channel.
``type``
    The MLIR stream type. ``plan_physical_intent`` takes the **first**
    ``i<N>``/``u<N>`` in it as the placement edge weight, so
    ``!allo.stream<i32, 16>`` weighs 32 and the depth is ignored.
``endpoints[]``
    ``pe`` (a ``semantic_id``), ``role`` (``producer``/``consumer`` -- read by
    physical intent), ``direction`` (``in``/``out`` -- read by macro planning),
    and ``accesses[].port_ordinal``. Both vocabularies are emitted, because the
    two nodes read different ones.

``macro_groups[]`` (post-HLS)
-----------------------------

``macro_class_id``, ``representative`` (must be in ``pe_instances``),
``member_count``, ``members[]`` (``semantic_id``, ``rtl_module``,
``orientation``), and ``proof.status`` — which must be exactly ``"proven"`` or
the class is silently dropped. ``proof.method`` is
``specialized_mlir_emitted_hls_contract``. ``rtl_audit.authority`` is false:
canonical RTL disagreement is reported, while the selected representative
implementation remains authoritative.


What the emitter derives it from
================================

Everything comes out of the **realized dataflow IR**, never from a table
someone typed. A hand-written manifest would be a second editing surface for
the architecture, which is precisely what having one Allo specification exists
to remove.

After ``allo.dataflow.customize``, the module is one ``func.func`` per kernel
instance plus a top function whose body constructs every stream and then calls
each instance with the streams it is wired to. **That body is the netlist.**

* **PE instances** — every ``func.func`` carrying ``df.kernel``. Its name is
  ``<kernel>_<i>_<j>``; the region's own ``mappings`` dict gives the split
  exactly (a kernel whose name ends in ``_<digits>`` is otherwise
  indistinguishable from one grid axis).
* **Ports** — the ``stypes`` attribute, stamped per argument by
  ``move_stream_to_interface``: ``i`` read, ``o`` written, ``_`` not a link.
* **Channels and their endpoints** — the SSA value each ``allo.stream_construct``
  defines, matched against the operands of each ``func.call``.
* **Top arguments** — ``allo.dataflow._build_manifest_top_arguments`` pairs the
  *realized* argument order with ``analyze_arg_load_store``'s directions.
  A region's arguments are not necessarily emitted in declaration order
  (:doc:`/developer/pitfalls`), and direction is a fact about the body.
* **Memories** — ``postprocess_hls_code`` pragmas argument *i* onto
  ``bundle=gmem{i}``, so the manifest names the ``<top>_gmem<i>_m_axi`` adapter
  Vitis will generate for it.

The emitter refuses what the consumers assume and never state: a duplicate
``semantic_id``, a non-dense port ordinal, an endpoint naming an unknown PE or
an ordinal that PE does not have, and a channel with two writers or two
readers. Each of those is otherwise a ``KeyError``, a ``StopIteration`` or a
silently misplaced macro several nodes downstream.

Post-HLS, the Vitis join parses emitted C++ functions and the Vitis log. Direct
and legalized clone names are resolved from reported process names; functions
inlined into a parent are recovered through compiler-generated loop labels.
The SystemC join parses emitted ``SC_MODULE`` kernels and Catapult's preferred
self-contained ``concat_rtl.v``, then joins semantic top arguments to the exact
synchronous-memory pin families realized in RTL. Every Verilog module
definition and instantiation is parsed once into a common dependency graph,
from which hardening roots, dependency closures, and realized instance paths
are derived. Ambiguous HLS processes, SystemC modules, or RTL modules fail
closed instead of being guessed.

Macro equivalence is proven by a versioned contract containing the specialized
MLIR contract hash, canonical emitted-HLS behavior and interface, hierarchy
mode, and synthesis context. A structural RTL fingerprint retains widths,
constants, operators, connectivity, and child-module types while discarding
generated spelling; it is diagnostic evidence, not the equivalence authority.


Validated against TinyTPU
=========================

At the shipped configuration (``TPU_T=4``, ``TPU_MAXDIM=64``) the emitter
produces, entirely from the design:

* 8 kernels and **38 PE instances** — ``pe`` and ``wld`` at 4×4, the other six
  singletons — matching ``TPU.architecture``'s ``instances=``;
* **85 channels**, one per element of the 16 declared ``Channel``\ s including
  the array ones, of which 12 (``3 * T``) are the terminal elements of the
  three chains and carry no endpoints — the last column forwards nothing east
  and the bottom row sends ``cw`` rather than a partial sum south;
* 4 memories on ``gmem0``–``gmem3``.

The post-HLS join, run against the committed 145-file RTL export, reproduces
the module names in the shipped ``T4_MAXDIM64_shipped`` hierarchy report
exactly: ``accu_0``, ``spm_0``, ``vru_0``, ``dma_ld_0_1``, ``sequencer_0_1``
and ``gmem0..3_m_axi``, with 0 unmatched PEs and 0 unjoined records.

The sharpest check is that the flow's *own* planner reads the array back out.
``plan_macros.graph_pin_sides`` is given PIDs and channel endpoints and nothing
about systolic arrays; on ``pe(1,1)`` it returns ``a_fwd`` in from the **W**,
out to the **E**, and ``p_fwd`` in from the **N**, out to the **S**. 90 of the
146 stream pins resolve as ``same_kernel_neighbor``.

``tests/dataflow/test_asic_manifest.py`` runs all of this, computing its
expectations from ``TPU.architecture`` rather than hard-coding them, and loads
``plan_macros`` and ``plan_physical_intent`` from
``allo/backend/asic/nodes/`` so that "the consumer accepts it" is executed
rather than asserted.


Validated in the Vitis pipe cleaner
===================================

The October 6, 2026 fixed 4-by-4 pipe cleaner is the first retained real Vitis
artifact to traverse the complete integrated flow. Its final manifest reports:

* schema version 2 and ``stage: "post_hls_enriched"``;
* 19 PE instances and 19 matched post-HLS records;
* zero unmatched/ambiguous and zero unjoined post-HLS records;
* seven proven implementation classes; and
* 119 parsed RTL module definitions with roots, dependencies, and realized
  instance paths.

The manifest was consumed by workload/testbench generation, RTL normalization,
macro planning and hardening, macro publication, full-chip assembly,
synthesis, and PNR. FFGL passed. BAGL reached ``ALLO_TEST_PASS`` with no DUT
unknowns and complete top plus 14/14 macro SDF annotation, but its strict
status remains failed because VCS reported 45 runtime setup/hold violations.
Innovus reported zero connectivity/DRC violations and clean setup/hold
signoff; Calibre reported zero non-antenna DRC results and correct LVS. The
remaining antenna, SDF-warning, and BAGL timing-simulation issues are not
waived by this integration result.


Validated in the SystemC/Catapult example
==========================================

The October 7, 2026 SystemC/Catapult run exercised the supported
``target="systemc"`` path through Catapult synthesis and the same hierarchical
ASIC consumers. The final manifest joined emitted ``SC_MODULE`` kernels to the
self-contained ``concat_rtl.v``, recorded synchronous-memory top-interface
families, parsed dependency closures, and drove macro planning, hardening,
full-chip assembly, FFGL, BAGL, DRC, LVS, activity extraction, and power. Both
gate-level simulations passed, all 15 macro instances received SDF in BAGL,
and LVS was correct.

The direct ``target="catapult"`` backend is not part of this supported path.
Catapult is the synthesis engine for SystemC/Connections input, and its
artifact parsing remains isolated behind the common manifest API.


What is *not* emitted
=====================

**Unsupported direct-Catapult evidence.** The supported Catapult-family path is
SystemC/Connections emission followed by Catapult synthesis. The separate
direct ``target="catapult"`` compatibility backend is not qualified for this
ASIC flow and has no parity requirement. Older direct-backend subfunction
discovery and ``FIFO_DEPTH`` constraint generation are not part of the
SystemC manifest contract.

**Arithmetic.** A composed region declares what a unit is wired to and never
what it computes, so the manifest carries no compute ports. That is the Action
layer's to add (:doc:`/developer/actions`).

Current validation boundary
===========================

The real Vitis and SystemC/Catapult runs close the former “consumer accepts a
fixture but no physical run exists” gap. Remaining work should be described
precisely:

* optionally compare the integrated manifest field by field with a
  representative result from the standalone ``allo-asic`` extraction and
  record hierarchy-parser runtime; this is a nonblocking parity/performance
  audit, not a missing integrated consumer;
* investigate the deferred antenna results, SDF annotation-coverage warnings,
  and BAGL runtime timing checks before calling the physical result signoff
  clean;
* retain fail-closed joins for future Vitis and SystemC naming patterns: a new
  ambiguous or unmatched process/module must increment the summary counters
  and stop the compilation node rather than be guessed; and
* keep direct ``target="catapult"`` outside the supported ASIC contract unless
  it independently receives backend enrichment and end-to-end qualification.

The manifest proves planning identity and representative substitution. It does
not waive downstream synthesis-boundary checks, physical verification, or
timing-aware simulation criteria.
