
//===------------------------------------------------------------*- C++ -*-===//
// Automatically generated file for SystemC (Catapult HLS / MatchLib Connections).
//===----------------------------------------------------------------------===//
#include <systemc.h>
#include <mc_connections.h>   // MatchLib Connections (LI valid/ready channels)
#include <mc_scverify.h>      // SCVerify testbench macros (CCS_MAIN / CCS_DESIGN)
#include <ac_int.h>
#include <ac_fixed.h>
#include <ac_channel.h>     // local self-FIFO streams (one-kernel put+get+status)
#include <ac_std_float.h>   // IEEE floats: ac_ieee_float<binaryNN>
#include <cstring>          // std::memcpy for bit-reinterpret (bitcast)
#include <stdint.h>
// f16: Catapult has no native `half`; alias it to ac_ieee_float<binary16>.
// (f32 -> ac_ieee_float<binary32> is emitted directly by getCatapultTypeName.)
typedef ac_ieee_float<binary16> half;
#include <iostream>
#include <fstream>
#include <iomanip>          // std::setprecision for lossless float tb output
#include <algorithm>
// --- float support helpers (half / ac_ieee_float<binary32> / double) ---
// Floats have no implicit int/stream conversions (and half is non-trivial), so
// memory ports (which transport a raw bit pattern in an ac_int req word) and the
// testbench (text I/O + waveform trace) need these shims.
// float -> raw bits (memcpy handles half=2B, ac_ieee_float<binary32>=4B, double=8B).
template <class T> inline unsigned long long _fbits(const T &v) {
  unsigned long long b = 0; std::memcpy(&b, &v, sizeof(T)); return b;
}
// Reconstruct a memory element from the DATAW raw bits: value-cast for integers,
// bit-reinterpret for floats (a value-cast would corrupt the float).
template <typename T> inline T _mem_decode(unsigned long long r) { return (T)(long long)r; }
template <> inline half _mem_decode<half>(unsigned long long r) {
  uint16_t u = (uint16_t)r; half h; std::memcpy(&h, &u, sizeof(u)); return h;
}
template <>
inline ac_ieee_float<binary32> _mem_decode<ac_ieee_float<binary32> >(unsigned long long r) {
  uint32_t u = (uint32_t)r; ac_ieee_float<binary32> f; std::memcpy(&f, &u, sizeof(u)); return f;
}
template <> inline double _mem_decode<double>(unsigned long long r) {
  double d; std::memcpy(&d, &r, sizeof(d)); return d;
}
// tb: data files hold float text -> read a float and convert.
inline std::istream &operator>>(std::istream &is, half &h) { float f; is >> f; h = half(f); return is; }
inline std::istream &operator>>(std::istream &is, ac_ieee_float<binary32> &h) {
  float f; is >> f; h = ac_ieee_float<binary32>(f); return is;
}
// tb/Connections waveform trace of a float (trace its raw bit pattern). Needed
// because Connections/sc_signal ports templated on these types call sc_trace.
inline void sc_trace(sc_core::sc_trace_file *tf, const half &h, const std::string &n) {
  sc_trace(tf, (unsigned short)_fbits(h), n);
}
inline void sc_trace(sc_core::sc_trace_file *tf, const ac_ieee_float<binary32> &h,
                     const std::string &n) {
  sc_trace(tf, (unsigned)_fbits(h), n);
}
// Single-shot completion counter (csim/testbench ONLY). Each kernel bumps this
// once, right after its body finishes its single pass; the sc_main testbench for
// memory-mapped-output designs advances the clock until all kernels are done
// before reading the memories out. Declared unconditionally so the tb compiles
// under __SYNTHESIS__ too, but the kernel's increment is guarded out of synthesis
// (see emitKernelModule) so the synthesized logic stays side-effect free.
static long __allo_done = 0;
// The reused body emits bare max()/min() for the Allo max/min intrinsics (Vitis
// resolves them via hls::); bind them to std:: so the same body compiles here.
using std::max;
using std::min;
// The reused Vivado-emitter body prints Vitis ap_(u)int types; alias them to
// Catapult's ac_int so the same body compiles. (TODO: emit ac_int/ac_fixed
// natively via a type-name override, like getCatapultTypeName in the Catapult
// emitter, and drop this shim.)
// ap_(u)int is a thin ac_int subclass adding the two Vitis affordances the reused
// body relies on and ac_int lacks:
//  (1) the x(hi,lo) BIT-RANGE operator (packed streams unpack via v(31,16) etc) —
//      a proxy that extracts on read and inserts on write, for const/runtime hi,lo;
//  (2) for W>64 only, an implicit narrowing conversion (`int32_t x = wide;`, e.g. a
//      GEMM accumulator past 64 bits) which ac_int omits above 64 bits.
// Adding operator() doesn't disturb arithmetic (it's the call operator, not a
// conversion); the W>64 narrowing stays gated so ac_int's own operators keep
// winning overload resolution (no builtin-conversion ambiguity).
//
// CSYNTH NOTE: the subclass-of-ac_int form below is CSIM-ONLY. Catapult's front-
// end treats ac_int as a builtin, so a struct deriving from it trips
// "struct assignment from non-struct type" (CIN-15) on every `v = <ac_int expr>;`
// — breaking synthesis branch-wide. Under __SYNTHESIS__ we therefore fall back to
// a PLAIN ac_int alias, which loses the two csim affordances (the x(hi,lo) bit-
// range and the >64-bit implicit narrowing). Consequence: every non-bit-slicing
// design synthesizes; a design that actually bit-slices (packed streams) fails at
// its `(hi,lo)` site instead — a clear, local error, and those need native ac_int
// .slc emission anyway. csim keeps the full-featured shim so behavior is unchanged.
#ifdef __SYNTHESIS__
template <int W> using ap_int = ac_int<W, true>;
template <int W> using ap_uint = ac_int<W, false>;
#else
template <class AC> struct ap_rng {
  AC &r;
  int hi, lo;
  ap_rng(AC &x, int h, int l) : r(x), hi(h), lo(l) {}
  operator long long() const { // read bits [hi:lo]
    AC m = (AC(1) << (hi - lo + 1)) - 1;
    return ((r >> lo) & m).to_int64();
  }
  template <class V> ap_rng &operator=(V v) { // write bits [hi:lo] = v
    AC m = (AC(1) << (hi - lo + 1)) - 1;
    r = (r & ~(m << lo)) | ((AC(v) & m) << lo);
    return *this;
  }
};
template <int W, bool Big = (W > 64)> struct ap_sel {
  struct s : ac_int<W, true> {
    using ac_int<W, true>::ac_int;
    ap_rng<ac_int<W, true>> operator()(int hi, int lo) { return {*this, hi, lo}; }
  };
  struct u : ac_int<W, false> {
    using ac_int<W, false>::ac_int;
    ap_rng<ac_int<W, false>> operator()(int hi, int lo) { return {*this, hi, lo}; }
  };
};
template <int W> struct ap_sel<W, true> {
  struct s : ac_int<W, true> {
    using ac_int<W, true>::ac_int;
    ap_rng<ac_int<W, true>> operator()(int hi, int lo) { return {*this, hi, lo}; }
    operator long long() const { return this->to_int64(); }
  };
  struct u : ac_int<W, false> {
    using ac_int<W, false>::ac_int;
    ap_rng<ac_int<W, false>> operator()(int hi, int lo) { return {*this, hi, lo}; }
    operator unsigned long long() const { return this->to_uint64(); }
  };
};
template <int W> using ap_int = typename ap_sel<W>::s;
template <int W> using ap_uint = typename ap_sel<W>::u;
#endif

// Random-access memory port for a non-sequential boundary array (SystemC/
// Connections flow — internal memory, the only kind SystemC supports; useref
// 14.9.1). Request packed into one ac_int: bit0 = opcode (0=LOAD,1=STORE),
// [ADDRW] addr, [DATAW] wdata. Response = the read value T. One txn/cycle.
// The kernel is the CLIENT (Out<req>/In<rsp>); this module holds the storage.
template <typename T, int SIZE, int ADDRW, int DATAW>
SC_MODULE(AlloMem) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::In< ac_int<1 + ADDRW + DATAW, false> > req;
  Connections::Out<T> rsp;
  T mem[SIZE];
  SC_HAS_PROCESS(AlloMem);
  AlloMem(sc_module_name n) : sc_module(n), req("req"), rsp("rsp") {
    SC_THREAD(run);
    sensitive << clk.pos();
    async_reset_signal_is(rst, false);
  }
  void run() {
    req.Reset();
    rsp.Reset();
    wait();
    while (1) {
      ac_int<1 + ADDRW + DATAW, false> r = req.Pop();
      ac_int<ADDRW, false> a = r.template slc<ADDRW>(1);
      if (r[0])
        mem[a] = _mem_decode<T>(r.template slc<DATAW>(1 + ADDRW).to_int64());
      else
        rsp.Push(mem[a]);
      wait();
    }
  }
};

// Write-only random-access memory port for a non-sequential OUTPUT array. Same
// packed request as AlloMem but STORE-only, so there is NO response port (a
// store is fire-and-forget; nothing to bind an rsp Out to). The testbench reads
// mem[] out after the run.  ⚠ the int64 cast makes float wdata lossy (defer).
template <typename T, int SIZE, int ADDRW, int DATAW>
SC_MODULE(AlloMemW) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::In< ac_int<1 + ADDRW + DATAW, false> > req;
  T mem[SIZE];
  SC_HAS_PROCESS(AlloMemW);
  AlloMemW(sc_module_name n) : sc_module(n), req("req") {
    SC_THREAD(run);
    sensitive << clk.pos();
    async_reset_signal_is(rst, false);
  }
  void run() {
    req.Reset();
    for (int z = 0; z < SIZE; z++) // 0-init so unwritten elements sum-merge as 0
      mem[z] = _mem_decode<T>(0);
    wait();
    while (1) {
      ac_int<1 + ADDRW + DATAW, false> r = req.Pop();
      ac_int<ADDRW, false> a = r.template slc<ADDRW>(1);
      mem[a] = _mem_decode<T>(r.template slc<DATAW>(1 + ADDRW).to_int64());
      wait();
    }
  }
};

// Depth-N buffered stream channel (Stream[T, N>=1]) — a SHIFT-REGISTER FIFO that
// Catapult can schedule. The read is the STATIC index buf[0] and the only
// runtime-indexed access is the append write buf[count], so there is NO
// same-cycle runtime read+write to buf (a ring buffer's buf[head]-read +
// buf[tail]-write couldn't be scheduled: the tool can't prove head != tail).
// Non-blocking, throughput 1: a dequeue frees a slot an enqueue can fill the
// same cycle. (Connections::Fifo is the official channel but trips a Catapult
// 2024.2 front-end assertion, sif_ci_expr:2080, on its named constructor.)
template <typename T, int N>
SC_MODULE(AlloFifo) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::In<T> in;
  Connections::Out<T> out;
  SC_HAS_PROCESS(AlloFifo);
  AlloFifo(sc_module_name nm) : sc_module(nm), in("in"), out("out") {
    SC_THREAD(run);
    sensitive << clk.pos();
    async_reset_signal_is(rst, false);
  }
  void run() {
    T buf[N];
    int count = 0;
    in.Reset();
    out.Reset();
    wait();
    while (1) {
      // enqueue and dequeue decisions are INDEPENDENT (both from the
      // start-of-cycle count) so the in.rdy / out.vld handshakes don't chain,
      // which is what let Catapult schedule the fixed-timing Connections I/O.
      bool deq = (count > 0) && out.PushNB(buf[0]);
      bool enq = false;
      T v;
      if (count < N)
        enq = in.PopNB(v);
      if (deq) { // shift the queue down by one (static indices)
        for (int k = 0; k < N - 1; k++)
          buf[k] = buf[k + 1];
        count--;
      }
      if (enq) { // append the new element (only runtime-indexed access)
        buf[count] = v;
        count++;
      }
      wait();
    }
  }
};

SC_MODULE(node_0_0) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::Out< ac_int<34, false> > v0_req;
  Connections::In< int32_t > v0_rsp;
  Connections::Out< ac_int<26, true> > v1;
  Connections::Out< ac_int<26, true> > v2;
  Connections::Out< ac_int<26, true> > v3;
  Connections::Out< ac_int<26, true> > v4;
  Connections::Out< ac_int<17, true> > v5;
  Connections::Out< ac_int<17, true> > v6;
  Connections::Out< ac_int<17, true> > v7;
  Connections::Out< ac_int<17, true> > v8;
  Connections::Out< int32_t > v9;
  Connections::Out< int32_t > v10;
  Connections::Out< int32_t > v11;
  Connections::Out< int32_t > v12;
  Connections::Out< int32_t > v13;
  Connections::Out< int32_t > v14;
  Connections::Out< int32_t > v15;
  Connections::Out< int32_t > v16;
  Connections::In< ac_int<26, true> > v17;
  Connections::In< ac_int<26, true> > v18;
  Connections::In< ac_int<26, true> > v19;
  Connections::In< ac_int<26, true> > v20;
  Connections::In< int32_t > v21;
  Connections::In< int32_t > v22;
  Connections::In< int32_t > v23;
  Connections::In< int32_t > v24;
  Connections::In< int32_t > v25;
  Connections::In< int32_t > v26;
  Connections::In< int32_t > v27;
  Connections::In< int32_t > v28;
  Connections::In< ac_int<17, true> > v29;
  Connections::In< ac_int<17, true> > v30;
  Connections::In< ac_int<17, true> > v31;
  Connections::In< ac_int<17, true> > v32;
  SC_HAS_PROCESS(node_0_0);
  node_0_0(sc_module_name n) : sc_module(n), v0_req("v0_req"), v0_rsp("v0_rsp"), v1("v1"), v2("v2"), v3("v3"), v4("v4"), v5("v5"), v6("v6"), v7("v7"), v8("v8"), v9("v9"), v10("v10"), v11("v11"), v12("v12"), v13("v13"), v14("v14"), v15("v15"), v16("v16"), v17("v17"), v18("v18"), v19("v19"), v20("v20"), v21("v21"), v22("v22"), v23("v23"), v24("v24"), v25("v25"), v26("v26"), v27("v27"), v28("v28"), v29("v29"), v30("v30"), v31("v31"), v32("v32") {
    SC_THREAD(run);
    sensitive << clk.pos();
    async_reset_signal_is(rst, false);
  }
  void run() {
    v0_req.Reset();
    v0_rsp.Reset();
    v1.Reset();
    v2.Reset();
    v3.Reset();
    v4.Reset();
    v5.Reset();
    v6.Reset();
    v7.Reset();
    v8.Reset();
    v9.Reset();
    v10.Reset();
    v11.Reset();
    v12.Reset();
    v13.Reset();
    v14.Reset();
    v15.Reset();
    v16.Reset();
    v17.Reset();
    v18.Reset();
    v19.Reset();
    v20.Reset();
    v21.Reset();
    v22.Reset();
    v23.Reset();
    v24.Reset();
    v25.Reset();
    v26.Reset();
    v27.Reset();
    v28.Reset();
    v29.Reset();
    v30.Reset();
    v31.Reset();
    v32.Reset();
    wait();
    int32_t irf[8];	// L39
    for (int v34 = 0; v34 < 8; v34++) {	// L40
      irf[v34] = 0;	// L40
    }
    half drf[8];	// L41
    for (int v36 = 0; v36 < 8; v36++) {	// L42
      drf[v36] = half(0.000000f);	// L42
    }
    int32_t drf_full[8];	// L43
    for (int v38 = 0; v38 < 8; v38++) {	// L44
      drf_full[v38] = 0;	// L44
    }
    int32_t dsmask;	// L45
    dsmask = 0;	// L46
    int32_t crv_vld;	// L47
    crv_vld = 0;	// L48
    half crv_data;	// L49
    crv_data = half(0.000000f);	// L50
    int32_t crv_addr;	// L51
    crv_addr = 0;	// L52
    int32_t crv_mode;	// L53
    crv_mode = 0;	// L54
    int32_t crv_raw;	// L55
    crv_raw = 0;	// L56
    int32_t csd_vld;	// L57
    csd_vld = 0;	// L58
    ac_int<26, false> csd_pkt;	// L59
    csd_pkt = 0;	// L60
    int32_t csd_dir;	// L61
    csd_dir = 0;	// L62
    int32_t row_id;	// L63
    row_id = 0;	// L64
    int32_t col_id;	// L65
    col_id = 0;	// L66
    ac_int<26, false> oe_r;	// L67
    oe_r = 0;	// L68
    ac_int<26, false> ow_r;	// L69
    ow_r = 0;	// L70
    ac_int<26, false> on_r;	// L71
    on_r = 0;	// L72
    ac_int<26, false> os_r;	// L73
    os_r = 0;	// L74
    ac_int<17, false> txn_r;	// L75
    txn_r = 0;	// L76
    ac_int<17, false> txs_r;	// L77
    txs_r = 0;	// L78
    ac_int<17, false> txw_r;	// L79
    txw_r = 0;	// L80
    ac_int<17, false> txe_r;	// L81
    txe_r = 0;	// L82
    half hold_v[4][2];	// L83
    for (int v59 = 0; v59 < 4; v59++) {	// L84
      for (int v60 = 0; v60 < 2; v60++) {	// L84
        hold_v[v59][v60] = half(0.000000f);	// L84
      }
    }
    uint8_t hold_cnt[4];	// L85
    for (int v62 = 0; v62 < 4; v62++) {	// L86
      hold_cnt[v62] = 0;	// L86
    }
    ac_int<26, false> rbuf[4][2];	// L87
    for (int v64 = 0; v64 < 4; v64++) {	// L88
      for (int v65 = 0; v65 < 2; v65++) {	// L88
        rbuf[v64][v65] = 0;	// L88
      }
    }
    uint8_t rbcnt[4];	// L89
    for (int v67 = 0; v67 < 4; v67++) {	// L90
      rbcnt[v67] = 0;	// L90
    }
    uint8_t rcred[4];	// L91
    for (int v69 = 0; v69 < 4; v69++) {	// L92
      rcred[v69] = 0;	// L92
    }
    uint8_t cre_r;	// L93
    cre_r = 2;	// L94
    uint8_t crw_r;	// L95
    crw_r = 2;	// L96
    uint8_t crs_r;	// L97
    crs_r = 2;	// L98
    uint8_t crn_r;	// L99
    crn_r = 2;	// L100
    int32_t scred[4];	// L101
    for (int v75 = 0; v75 < 4; v75++) {	// L102
      scred[v75] = 0;	// L102
    }
    int32_t txp_v[4];	// L103
    for (int v77 = 0; v77 < 4; v77++) {	// L104
      txp_v[v77] = 0;	// L104
    }
    half txp_d[4];	// L105
    for (int v79 = 0; v79 < 4; v79++) {	// L106
      txp_d[v79] = half(0.000000f);	// L106
    }
    int32_t txp_r[4];	// L107
    for (int v81 = 0; v81 < 4; v81++) {	// L108
      txp_r[v81] = 0;	// L108
    }
    int32_t sc_r[4];	// L109
    for (int v83 = 0; v83 < 4; v83++) {	// L110
      sc_r[v83] = 2;	// L110
    }
    int32_t cfg_isz;	// L111
    cfg_isz = 0;	// L112
    int32_t cfg_itsz;	// L113
    cfg_itsz = 0;	// L114
    uint8_t fetch_en;	// L115
    fetch_en = 0;	// L116
    uint8_t instr_cnt;	// L117
    instr_cnt = 0;	// L118
    uint8_t iter_cnt;	// L119
    iter_cnt = 0;	// L120
    uint8_t condition_reg;	// L121
    condition_reg = 0;	// L122
    uint8_t sb_v[5];	// L123
    for (int v91 = 0; v91 < 5; v91++) {	// L124
      sb_v[v91] = 0;	// L124
    }
    uint8_t sb_dst[5];	// L125
    for (int v93 = 0; v93 < 5; v93++) {	// L126
      sb_dst[v93] = 0;	// L126
    }
    uint8_t sb_cmp[5];	// L127
    for (int v95 = 0; v95 < 5; v95++) {	// L128
      sb_cmp[v95] = 0;	// L128
    }
    uint8_t sb_rtr[5];	// L129
    for (int v97 = 0; v97 < 5; v97++) {	// L130
      sb_rtr[v97] = 0;	// L130
    }
    uint8_t sb_inj[5];	// L131
    for (int v99 = 0; v99 < 5; v99++) {	// L132
      sb_inj[v99] = 0;	// L132
    }
    uint8_t sb_dir[5];	// L133
    for (int v101 = 0; v101 < 5; v101++) {	// L134
      sb_dir[v101] = 0;	// L134
    }
    uint8_t sb_id[5];	// L135
    for (int v103 = 0; v103 < 5; v103++) {	// L136
      sb_id[v103] = 0;	// L136
    }
    uint8_t sb_rvld[5];	// L137
    for (int v105 = 0; v105 < 5; v105++) {	// L138
      sb_rvld[v105] = 0;	// L138
    }
    uint8_t sb_ix[5];	// L139
    for (int v107 = 0; v107 < 5; v107++) {	// L140
      sb_ix[v107] = 0;	// L140
    }
    uint8_t sb_long[5];	// L141
    for (int v109 = 0; v109 < 5; v109++) {	// L142
      sb_long[v109] = 0;	// L142
    }
    half resq[8];	// L143
    for (int v111 = 0; v111 < 8; v111++) {	// L144
      resq[v111] = half(0.000000f);	// L144
    }
    uint8_t cmpq[8];	// L145
    for (int v113 = 0; v113 < 8; v113++) {	// L146
      cmpq[v113] = 0;	// L146
    }
    uint8_t resq_wr;	// L147
    resq_wr = 0;	// L148
    ac_int<26, false> zpkt;	// L149
    zpkt = 0;	// L150
    ac_int<17, false> zsys;	// L151
    zsys = 0;	// L152
    int32_t zcr;	// L153
    zcr = 0;	// L154
    int32_t v118;
    v0_req.Push( (ac_int<34, false>)(((0) + (0))) << 1 );
    v118 = v0_rsp.Pop();	// L155
    ac_int<33, true> v119 = v118;	// L156
    ac_int<33, true> v120 = v119 - 1;	// L157
    int v121 = v120;	// L158
    for (int v122 = 0; v122 < v121; v122 += 1) {	// L159
      ac_int<26, true> v123 = zpkt;	// L160
      v1.Push(v123);	// L161
      ac_int<26, true> v124 = zpkt;	// L162
      v2.Push(v124);	// L163
      ac_int<26, true> v125 = zpkt;	// L164
      v3.Push(v125);	// L165
      ac_int<26, true> v126 = zpkt;	// L166
      v4.Push(v126);	// L167
      ac_int<17, true> v127 = zsys;	// L168
      v5.Push(v127);	// L169
      ac_int<17, true> v128 = zsys;	// L170
      v6.Push(v128);	// L171
      ac_int<17, true> v129 = zsys;	// L172
      v7.Push(v129);	// L173
      ac_int<17, true> v130 = zsys;	// L174
      v8.Push(v130);	// L175
      int32_t v131 = zcr;	// L176
      v9.Push(v131);	// L177
      int32_t v132 = zcr;	// L178
      v10.Push(v132);	// L179
      int32_t v133 = zcr;	// L180
      v11.Push(v133);	// L181
      int32_t v134 = zcr;	// L182
      v12.Push(v134);	// L183
      int32_t v135 = zcr;	// L184
      v13.Push(v135);	// L185
      int32_t v136 = zcr;	// L186
      v14.Push(v136);	// L187
      int32_t v137 = zcr;	// L188
      v15.Push(v137);	// L189
      int32_t v138 = zcr;	// L190
      v16.Push(v138);	// L191
    }
    ac_int<26, true> v139 = oe_r;	// L193
    v1.Push(v139);	// L194
    ac_int<26, true> v140 = ow_r;	// L195
    v2.Push(v140);	// L196
    ac_int<26, true> v141 = os_r;	// L197
    v3.Push(v141);	// L198
    ac_int<26, true> v142 = on_r;	// L199
    v4.Push(v142);	// L200
    ac_int<17, true> v143 = txe_r;	// L201
    v5.Push(v143);	// L202
    ac_int<17, true> v144 = txw_r;	// L203
    v6.Push(v144);	// L204
    ac_int<17, true> v145 = txs_r;	// L205
    v7.Push(v145);	// L206
    ac_int<17, true> v146 = txn_r;	// L207
    v8.Push(v146);	// L208
    int8_t v147 = cre_r;	// L209
    v9.Push(v147);	// L210
    int8_t v148 = crw_r;	// L211
    v10.Push(v148);	// L212
    int8_t v149 = crs_r;	// L213
    v11.Push(v149);	// L214
    int8_t v150 = crn_r;	// L215
    v12.Push(v150);	// L216
    int32_t v151 = sc_r[0];	// L217
    v15.Push(v151);	// L218
    int32_t v152 = sc_r[1];	// L219
    v16.Push(v152);	// L220
    int32_t v153 = sc_r[2];	// L221
    v13.Push(v153);	// L222
    int32_t v154 = sc_r[3];	// L223
    v14.Push(v154);	// L224
    l_S_t_1_t: for (int t = 0; t < 215; t++) {	// L225
      ac_int<26, false> v156 = v17.Pop();	// L226
      ac_int<26, false> p_w;	// L227
      p_w = v156;	// L228
      ac_int<26, false> v158 = v18.Pop();	// L229
      ac_int<26, false> p_e;	// L230
      p_e = v158;	// L231
      ac_int<26, false> v160 = v19.Pop();	// L232
      ac_int<26, false> p_n;	// L233
      p_n = v160;	// L234
      ac_int<26, false> v162 = v20.Pop();	// L235
      ac_int<26, false> p_s;	// L236
      p_s = v162;	// L237
      int32_t v164 = v21.Pop();	// L238
      uint8_t v165 = rcred[0];	// L239
      ac_int<33, true> v166 = v165;	// L240
      ac_int<33, true> v167 = v164;	// L241
      ac_int<33, true> v168 = v166 + v167;	// L242
      uint8_t v169 = v168;	// L243
      rcred[0] = v169;	// L244
      int32_t v170 = v22.Pop();	// L245
      uint8_t v171 = rcred[1];	// L246
      ac_int<33, true> v172 = v171;	// L247
      ac_int<33, true> v173 = v170;	// L248
      ac_int<33, true> v174 = v172 + v173;	// L249
      uint8_t v175 = v174;	// L250
      rcred[1] = v175;	// L251
      int32_t v176 = v23.Pop();	// L252
      uint8_t v177 = rcred[2];	// L253
      ac_int<33, true> v178 = v177;	// L254
      ac_int<33, true> v179 = v176;	// L255
      ac_int<33, true> v180 = v178 + v179;	// L256
      uint8_t v181 = v180;	// L257
      rcred[2] = v181;	// L258
      int32_t v182 = v24.Pop();	// L259
      uint8_t v183 = rcred[3];	// L260
      ac_int<33, true> v184 = v183;	// L261
      ac_int<33, true> v185 = v182;	// L262
      ac_int<33, true> v186 = v184 + v185;	// L263
      uint8_t v187 = v186;	// L264
      rcred[3] = v187;	// L265
      int32_t v188 = v25.Pop();	// L266
      int32_t v189 = scred[0];	// L267
      ac_int<33, true> v190 = v189;	// L268
      ac_int<33, true> v191 = v188;	// L269
      ac_int<33, true> v192 = v190 + v191;	// L270
      int32_t v193 = v192;	// L271
      scred[0] = v193;	// L272
      int32_t v194 = v26.Pop();	// L273
      int32_t v195 = scred[1];	// L274
      ac_int<33, true> v196 = v195;	// L275
      ac_int<33, true> v197 = v194;	// L276
      ac_int<33, true> v198 = v196 + v197;	// L277
      int32_t v199 = v198;	// L278
      scred[1] = v199;	// L279
      int32_t v200 = v27.Pop();	// L280
      int32_t v201 = scred[2];	// L281
      ac_int<33, true> v202 = v201;	// L282
      ac_int<33, true> v203 = v200;	// L283
      ac_int<33, true> v204 = v202 + v203;	// L284
      int32_t v205 = v204;	// L285
      scred[2] = v205;	// L286
      int32_t v206 = v28.Pop();	// L287
      int32_t v207 = scred[3];	// L288
      ac_int<33, true> v208 = v207;	// L289
      ac_int<33, true> v209 = v206;	// L290
      ac_int<33, true> v210 = v208 + v209;	// L291
      int32_t v211 = v210;	// L292
      scred[3] = v211;	// L293
      ac_int<26, false> fin[4];	// L294
      for (int v213 = 0; v213 < 4; v213++) {	// L295
        fin[v213] = 0;	// L295
      }
      ac_int<26, true> v214 = p_w;	// L296
      fin[0] = v214;	// L297
      ac_int<26, true> v215 = p_e;	// L298
      fin[1] = v215;	// L299
      ac_int<26, true> v216 = p_n;	// L300
      fin[2] = v216;	// L301
      ac_int<26, true> v217 = p_s;	// L302
      fin[3] = v217;	// L303
      l_S_d_1_d: for (int d = 0; d < 4; d++) {	// L304
        ac_int<26, false> v219 = fin[d];	// L305
        bool v220;
        ac_int<26, true> _bs_v220 = v219;
        v220 = _bs_v220[25];	// L306
        int32_t v221 = v220;	// L307
        bool v222 = v221 == 1;	// L308
        uint8_t v223 = rbcnt[d];	// L309
        int32_t v224 = v223;	// L310
        bool v225 = v224 < 2;	// L311
        bool v226 = v222 & v225;	// L312
        if (v226) {	// L313
          ac_int<26, false> v227 = fin[d];	// L314
          uint8_t v228 = rbcnt[d];	// L315
          int v229 = v228;	// L316
          rbuf[d][v229] = v227;	// L317
          uint8_t v230 = rbcnt[d];	// L318
          ac_int<33, true> v231 = v230;	// L319
          ac_int<33, true> v232 = v231 + 1;	// L320
          uint8_t v233 = v232;	// L321
          rbcnt[d] = v233;	// L322
        }
      }
      ac_int<26, false> hd[4];	// L325
      for (int v235 = 0; v235 < 4; v235++) {	// L326
        hd[v235] = 0;	// L326
      }
      int32_t hvld[4];	// L327
      for (int v237 = 0; v237 < 4; v237++) {	// L328
        hvld[v237] = 0;	// L328
      }
      int32_t hit[4];	// L329
      for (int v239 = 0; v239 < 4; v239++) {	// L330
        hit[v239] = 0;	// L330
      }
      int32_t axis[4];	// L331
      for (int v241 = 0; v241 < 4; v241++) {	// L332
        axis[v241] = 0;	// L332
      }
      int32_t v242 = col_id;	// L333
      axis[0] = v242;	// L334
      int32_t v243 = col_id;	// L335
      axis[1] = v243;	// L336
      int32_t v244 = row_id;	// L337
      axis[2] = v244;	// L338
      int32_t v245 = row_id;	// L339
      axis[3] = v245;	// L340
      l_S_d_2_d1: for (int d1 = 0; d1 < 4; d1++) {	// L341
        uint8_t v247 = rbcnt[d1];	// L342
        int32_t v248 = v247;	// L343
        bool v249 = v248 > 0;	// L344
        if (v249) {	// L345
          ac_int<26, false> v250 = rbuf[d1][0];	// L346
          hd[d1] = v250;	// L347
          hvld[d1] = 1;	// L348
          ac_int<26, false> v251 = hd[d1];	// L349
          ac_int<4, true> v252;
          ac_int<26, true> _bs_v252 = v251;
          v252 = _bs_v252.slc<4>(21);	// L350
          int32_t v253 = axis[d1];	// L351
          int32_t v254 = v252;	// L352
          bool v255 = v254 == v253;	// L353
          if (v255) {	// L354
            hit[d1] = 1;	// L355
          }
        }
      }
      ac_int<26, false> o_crv;	// L359
      o_crv = 0;	// L360
      int32_t crv_in;	// L361
      crv_in = -1;	// L362
      int32_t v258 = hit[3];	// L363
      bool v259 = v258 == 1;	// L364
      if (v259) {	// L365
        ac_int<26, false> v260 = hd[3];	// L366
        o_crv = v260;	// L367
        crv_in = 3;	// L368
      } else {
        int32_t v261 = hit[2];	// L370
        bool v262 = v261 == 1;	// L371
        if (v262) {	// L372
          ac_int<26, false> v263 = hd[2];	// L373
          o_crv = v263;	// L374
          crv_in = 2;	// L375
        } else {
          int32_t v264 = hit[1];	// L377
          bool v265 = v264 == 1;	// L378
          if (v265) {	// L379
            ac_int<26, false> v266 = hd[1];	// L380
            o_crv = v266;	// L381
            crv_in = 1;	// L382
          } else {
            int32_t v267 = hit[0];	// L384
            bool v268 = v267 == 1;	// L385
            if (v268) {	// L386
              ac_int<26, false> v269 = hd[0];	// L387
              o_crv = v269;	// L388
              crv_in = 0;	// L389
            }
          }
        }
      }
      ac_int<26, false> o_out[4];	// L394
      for (int v271 = 0; v271 < 4; v271++) {	// L395
        o_out[v271] = 0;	// L395
      }
      int32_t pop[4];	// L396
      for (int v273 = 0; v273 < 4; v273++) {	// L397
        pop[v273] = 0;	// L397
      }
      int32_t inj_done;	// L398
      inj_done = 0;	// L399
      int32_t idir;	// L400
      idir = -1;	// L401
      ac_int<26, true> v276 = csd_pkt;	// L402
      bool v277;
      ac_int<26, true> _bs_v277 = v276;
      v277 = _bs_v277[25];	// L403
      int32_t v278 = v277;	// L404
      bool v279 = v278 == 1;	// L405
      if (v279) {	// L406
        int32_t v280 = csd_dir;	// L407
        ac_int<33, true> v281 = v280;	// L408
        ac_int<33, true> v282 = 3 - v281;	// L409
        int32_t v283 = v282;	// L410
        idir = v283;	// L411
      }
      l_S_o_3_o: for (int o = 0; o < 4; o++) {	// L413
        uint8_t v285 = rcred[o];	// L414
        int32_t v286 = v285;	// L415
        bool v287 = v286 > 0;	// L416
        if (v287) {	// L417
          int32_t v288 = idir;	// L418
          ac_int<33, true> v289 = v288;	// L419
          ac_int<33, true> v290 = o;	// L420
          bool v291 = v289 == v290;	// L421
          if (v291) {	// L422
            ac_int<26, true> v292 = csd_pkt;	// L423
            o_out[o] = v292;	// L424
            uint8_t v293 = rcred[o];	// L425
            ac_int<33, true> v294 = v293;	// L426
            ac_int<33, true> v295 = v294 - 1;	// L427
            uint8_t v296 = v295;	// L428
            rcred[o] = v296;	// L429
            inj_done = 1;	// L430
          } else {
            int32_t v297 = hvld[o];	// L432
            bool v298 = v297 == 1;	// L433
            int32_t v299 = hit[o];	// L434
            bool v300 = v299 == 0;	// L435
            bool v301 = v298 & v300;	// L436
            if (v301) {	// L437
              ac_int<26, false> v302 = hd[o];	// L438
              o_out[o] = v302;	// L439
              uint8_t v303 = rcred[o];	// L440
              ac_int<33, true> v304 = v303;	// L441
              ac_int<33, true> v305 = v304 - 1;	// L442
              uint8_t v306 = v305;	// L443
              rcred[o] = v306;	// L444
              pop[o] = 1;	// L445
            }
          }
        }
      }
      int32_t v307 = crv_in;	// L450
      bool v308 = v307 >= 0;	// L451
      if (v308) {	// L452
        int32_t v309 = crv_in;	// L453
        int v310 = v309;	// L454
        pop[v310] = 1;	// L455
      }
      int32_t ret[4];	// L457
      for (int v312 = 0; v312 < 4; v312++) {	// L458
        ret[v312] = 0;	// L458
      }
      l_S_d_4_d2: for (int d2 = 0; d2 < 4; d2++) {	// L459
        int32_t v314 = pop[d2];	// L460
        bool v315 = v314 == 1;	// L461
        if (v315) {	// L462
          l_S_sft_4_sft: for (int sft = 0; sft < 1; sft++) {	// L463
            ac_int<26, false> v317 = rbuf[d2][(sft + 1)];	// L464
            rbuf[d2][sft] = v317;	// L465
          }
          uint8_t v318 = rbcnt[d2];	// L467
          ac_int<33, true> v319 = v318;	// L468
          ac_int<33, true> v320 = v319 - 1;	// L469
          uint8_t v321 = v320;	// L470
          rbcnt[d2] = v321;	// L471
          ret[d2] = 1;	// L472
        }
      }
      int32_t v322 = ret[0];	// L475
      uint8_t v323 = v322;	// L476
      cre_r = v323;	// L477
      int32_t v324 = ret[1];	// L478
      uint8_t v325 = v324;	// L479
      crw_r = v325;	// L480
      int32_t v326 = ret[2];	// L481
      uint8_t v327 = v326;	// L482
      crs_r = v327;	// L483
      int32_t v328 = ret[3];	// L484
      uint8_t v329 = v328;	// L485
      crn_r = v329;	// L486
      ac_int<26, false> v330 = o_out[0];	// L487
      oe_r = v330;	// L488
      ac_int<26, false> v331 = o_out[1];	// L489
      ow_r = v331;	// L490
      ac_int<26, false> v332 = o_out[2];	// L491
      os_r = v332;	// L492
      ac_int<26, false> v333 = o_out[3];	// L493
      on_r = v333;	// L494
      int32_t v334 = inj_done;	// L495
      bool v335 = v334 == 1;	// L496
      if (v335) {	// L497
        csd_pkt = 0;	// L498
      }
      ac_int<26, true> v336 = o_crv;	// L500
      bool v337;
      ac_int<26, true> _bs_v337 = v336;
      v337 = _bs_v337[25];	// L501
      int32_t v338 = v337;	// L502
      crv_vld = v338;	// L503
      ac_int<26, true> v339 = o_crv;	// L504
      int16_t v340;
      ac_int<26, true> _bs_v340 = v339;
      v340 = _bs_v340.slc<16>(0);	// L505
      half v341;
      uint16_t _bc_v341 = v340;
      std::memcpy(&v341, &_bc_v341, sizeof(v341));	// L506
      crv_data = v341;	// L507
      ac_int<26, true> v342 = o_crv;	// L508
      ac_int<4, true> v343;
      ac_int<26, true> _bs_v343 = v342;
      v343 = _bs_v343.slc<4>(16);	// L509
      int32_t v344 = v343;	// L510
      crv_addr = v344;	// L511
      ac_int<26, true> v345 = o_crv;	// L512
      bool v346;
      ac_int<26, true> _bs_v346 = v345;
      v346 = _bs_v346[20];	// L513
      int32_t v347 = v346;	// L514
      crv_mode = v347;	// L515
      ac_int<26, true> v348 = o_crv;	// L516
      int16_t v349;
      ac_int<26, true> _bs_v349 = v348;
      v349 = _bs_v349.slc<16>(0);	// L517
      int32_t v350 = v349;	// L518
      crv_raw = v350;	// L519
      ac_int<17, false> v351 = v29.Pop();	// L520
      ac_int<17, false> rx_w;	// L521
      rx_w = v351;	// L522
      ac_int<17, false> v353 = v30.Pop();	// L523
      ac_int<17, false> rx_e;	// L524
      rx_e = v353;	// L525
      ac_int<17, false> v355 = v31.Pop();	// L526
      ac_int<17, false> rx_n;	// L527
      rx_n = v355;	// L528
      ac_int<17, false> v357 = v32.Pop();	// L529
      ac_int<17, false> rx_s;	// L530
      rx_s = v357;	// L531
      half rxv[4];	// L532
      for (int v360 = 0; v360 < 4; v360++) {	// L533
        rxv[v360] = half(0.000000f);	// L533
      }
      int32_t rxvld[4];	// L534
      for (int v362 = 0; v362 < 4; v362++) {	// L535
        rxvld[v362] = 0;	// L535
      }
      ac_int<17, true> v363 = rx_n;	// L536
      int16_t v364;
      ac_int<17, true> _bs_v364 = v363;
      v364 = _bs_v364.slc<16>(1);	// L537
      half v365;
      uint16_t _bc_v365 = v364;
      std::memcpy(&v365, &_bc_v365, sizeof(v365));	// L538
      rxv[0] = v365;	// L539
      ac_int<17, true> v366 = rx_n;	// L540
      bool v367;
      ac_int<17, true> _bs_v367 = v366;
      v367 = _bs_v367[0];	// L541
      int32_t v368 = v367;	// L542
      rxvld[0] = v368;	// L543
      ac_int<17, true> v369 = rx_s;	// L544
      int16_t v370;
      ac_int<17, true> _bs_v370 = v369;
      v370 = _bs_v370.slc<16>(1);	// L545
      half v371;
      uint16_t _bc_v371 = v370;
      std::memcpy(&v371, &_bc_v371, sizeof(v371));	// L546
      rxv[1] = v371;	// L547
      ac_int<17, true> v372 = rx_s;	// L548
      bool v373;
      ac_int<17, true> _bs_v373 = v372;
      v373 = _bs_v373[0];	// L549
      int32_t v374 = v373;	// L550
      rxvld[1] = v374;	// L551
      ac_int<17, true> v375 = rx_w;	// L552
      int16_t v376;
      ac_int<17, true> _bs_v376 = v375;
      v376 = _bs_v376.slc<16>(1);	// L553
      half v377;
      uint16_t _bc_v377 = v376;
      std::memcpy(&v377, &_bc_v377, sizeof(v377));	// L554
      rxv[2] = v377;	// L555
      ac_int<17, true> v378 = rx_w;	// L556
      bool v379;
      ac_int<17, true> _bs_v379 = v378;
      v379 = _bs_v379[0];	// L557
      int32_t v380 = v379;	// L558
      rxvld[2] = v380;	// L559
      ac_int<17, true> v381 = rx_e;	// L560
      int16_t v382;
      ac_int<17, true> _bs_v382 = v381;
      v382 = _bs_v382.slc<16>(1);	// L561
      half v383;
      uint16_t _bc_v383 = v382;
      std::memcpy(&v383, &_bc_v383, sizeof(v383));	// L562
      rxv[3] = v383;	// L563
      ac_int<17, true> v384 = rx_e;	// L564
      bool v385;
      ac_int<17, true> _bs_v385 = v384;
      v385 = _bs_v385[0];	// L565
      int32_t v386 = v385;	// L566
      rxvld[3] = v386;	// L567
      l_S_d_6_d3: for (int d3 = 0; d3 < 4; d3++) {	// L568
        int32_t v388 = rxvld[d3];	// L569
        bool v389 = v388 == 1;	// L570
        uint8_t v390 = hold_cnt[d3];	// L571
        int32_t v391 = v390;	// L572
        bool v392 = v391 < 2;	// L573
        bool v393 = v389 & v392;	// L574
        if (v393) {	// L575
          half v394 = rxv[d3];	// L576
          uint8_t v395 = hold_cnt[d3];	// L577
          int v396 = v395;	// L578
          hold_v[d3][v396] = v394;	// L579
          uint8_t v397 = hold_cnt[d3];	// L580
          ac_int<33, true> v398 = v397;	// L581
          ac_int<33, true> v399 = v398 + 1;	// L582
          uint8_t v400 = v399;	// L583
          hold_cnt[d3] = v400;	// L584
        }
      }
      int32_t retire_ok;	// L587
      retire_ok = 1;	// L588
      uint8_t v402 = sb_v[0];	// L589
      int32_t v403 = v402;	// L590
      bool v404 = v403 == 1;	// L591
      uint8_t v405 = sb_rtr[0];	// L592
      int32_t v406 = v405;	// L593
      bool v407 = v406 == 0;	// L594
      uint8_t v408 = sb_dst[0];	// L595
      int32_t v409 = v408;	// L596
      bool v410 = v409 >= 12;	// L597
      bool v411 = v404 & v407;	// L598
      bool v412 = v411 & v410;	// L599
      if (v412) {	// L600
        uint8_t v413 = sb_rvld[0];	// L601
        int32_t v414 = v413;	// L602
        bool v415 = v414 == 1;	// L603
        uint8_t v416 = sb_dst[0];	// L604
        int32_t v417 = v416;	// L605
        int32_t v418 = v417 & 3;	// L606
        int v419 = v418;	// L607
        int32_t v420 = txp_v[v419];	// L608
        bool v421 = v420 == 1;	// L609
        bool v422 = v415 & v421;	// L610
        if (v422) {	// L611
          retire_ok = 0;	// L612
        }
      }
      uint8_t v423 = sb_v[0];	// L615
      int32_t v424 = v423;	// L616
      bool v425 = v424 == 1;	// L617
      int32_t v426 = retire_ok;	// L618
      bool v427 = v426 == 1;	// L619
      bool v428 = v425 & v427;	// L620
      if (v428) {	// L621
        uint8_t v429 = sb_ix[0];	// L622
        int v430 = v429;	// L623
        half v431 = resq[v430];	// L624
        half wb;	// L625
        wb = v431;	// L626
        uint8_t v433 = sb_cmp[0];	// L627
        int32_t v434 = v433;	// L628
        bool v435 = v434 == 1;	// L629
        if (v435) {	// L630
          uint8_t v436 = sb_ix[0];	// L631
          int v437 = v436;	// L632
          uint8_t v438 = cmpq[v437];	// L633
          condition_reg = v438;	// L634
        }
        uint8_t v439 = sb_rtr[0];	// L636
        int32_t v440 = v439;	// L637
        bool v441 = v440 == 1;	// L638
        if (v441) {	// L639
          uint8_t v442 = sb_inj[0];	// L640
          int32_t v443 = v442;	// L641
          bool v444 = v443 == 1;	// L642
          ac_int<26, true> v445 = csd_pkt;	// L643
          bool v446;
          ac_int<26, true> _bs_v446 = v445;
          v446 = _bs_v446[25];	// L644
          int32_t v447 = v446;	// L645
          bool v448 = v447 == 0;	// L646
          bool v449 = v444 & v448;	// L647
          if (v449) {	// L648
            half v450 = wb;	// L649
            uint16_t v451;
            half _bc_v451 = v450;
            std::memcpy(&v451, &_bc_v451, sizeof(v451));	// L650
            ac_int<26, true> v452 = csd_pkt;	// L651
            ac_int<26, true> v453;
            ac_int<26, true> _bs_v453 = v452;
            _bs_v453.set_slc(0, ac_int<16, false>(v451));
            v453 = _bs_v453;	// L652
            csd_pkt = v453;	// L653
            uint8_t v454 = sb_dst[0];	// L654
            ac_int<4, false> v455 = v454;	// L655
            ac_int<26, true> v456 = csd_pkt;	// L656
            ac_int<26, true> v457;
            ac_int<26, true> _bs_v457 = v456;
            _bs_v457.set_slc(16, ac_int<4, false>(v455));
            v457 = _bs_v457;	// L657
            csd_pkt = v457;	// L658
            uint8_t v458 = sb_id[0];	// L659
            ac_int<4, false> v459 = v458;	// L660
            ac_int<26, true> v460 = csd_pkt;	// L661
            ac_int<26, true> v461;
            ac_int<26, true> _bs_v461 = v460;
            _bs_v461.set_slc(21, ac_int<4, false>(v459));
            v461 = _bs_v461;	// L662
            csd_pkt = v461;	// L663
            uint8_t v462 = sb_rvld[0];	// L664
            bool v463 = v462;	// L665
            ac_int<26, true> v464 = csd_pkt;	// L666
            ac_int<26, true> v465;
            ac_int<26, true> _bs_v465 = v464;
            _bs_v465[25] = v463;
            v465 = _bs_v465;	// L667
            csd_pkt = v465;	// L668
            uint8_t v466 = sb_dir[0];	// L669
            int32_t v467 = v466;	// L670
            csd_dir = v467;	// L671
          }
        } else {
          uint8_t v468 = sb_dst[0];	// L674
          int32_t v469 = v468;	// L675
          bool v470 = v469 >= 12;	// L676
          if (v470) {	// L677
            uint8_t v471 = sb_rvld[0];	// L678
            int32_t v472 = v471;	// L679
            bool v473 = v472 == 1;	// L680
            if (v473) {	// L681
              uint8_t v474 = sb_dst[0];	// L682
              int32_t v475 = v474;	// L683
              int32_t v476 = v475 & 3;	// L684
              int v477 = v476;	// L685
              txp_v[v477] = 1;	// L686
              half v478 = wb;	// L687
              uint8_t v479 = sb_dst[0];	// L688
              int32_t v480 = v479;	// L689
              int32_t v481 = v480 & 3;	// L690
              int v482 = v481;	// L691
              txp_d[v482] = v478;	// L692
              uint8_t v483 = sb_dst[0];	// L693
              int32_t v484 = v483;	// L694
              int32_t v485 = v484 & 3;	// L695
              int v486 = v485;	// L696
              txp_r[v486] = 1;	// L697
            }
          } else {
            uint8_t v487 = sb_rvld[0];	// L700
            int32_t v488 = v487;	// L701
            bool v489 = v488 == 1;	// L702
            if (v489) {	// L703
              uint8_t v490 = sb_dst[0];	// L704
              int32_t v491 = v490;	// L705
              bool v492 = v491 < 8;	// L706
              int32_t v493 = dsmask;	// L707
              int32_t v494 = v493 >> v491;	// L710
              int32_t v495 = v494 & 1;	// L711
              bool v496 = v495 == 1;	// L712
              bool v497 = v492 & v496;	// L713
              if (v497) {	// L714
                uint8_t v498 = sb_dst[0];	// L715
                int v499 = v498;	// L716
                int32_t v500 = drf_full[v499];	// L717
                bool v501 = v500 == 0;	// L718
                if (v501) {	// L719
                  half v502 = wb;	// L720
                  uint8_t v503 = sb_dst[0];	// L721
                  int v504 = v503;	// L722
                  drf[v504] = v502;	// L723
                  uint8_t v505 = sb_dst[0];	// L724
                  int v506 = v505;	// L725
                  drf_full[v506] = 1;	// L726
                }
              } else {
                half v507 = wb;	// L729
                uint8_t v508 = sb_dst[0];	// L730
                int32_t v509 = v508;	// L731
                int32_t v510 = v509 & 7;	// L732
                int v511 = v510;	// L733
                drf[v511] = v507;	// L734
              }
            }
          }
        }
      }
      int32_t pc;	// L740
      pc = -1;	// L741
      int8_t v513 = fetch_en;	// L742
      int32_t v514 = v513;	// L743
      bool v515 = v514 == 1;	// L744
      if (v515) {	// L745
        int8_t v516 = instr_cnt;	// L746
        int32_t v517 = v516;	// L747
        pc = v517;	// L748
      }
      int32_t instr;	// L750
      instr = 0;	// L751
      int32_t v519 = pc;	// L752
      bool v520 = v519 >= 0;	// L753
      if (v520) {	// L754
        int32_t v521 = pc;	// L755
        int v522 = v521;	// L756
        int32_t v523 = irf[v522];	// L757
        instr = v523;	// L758
      }
      int32_t v524 = instr;	// L760
      int32_t v525 = v524 & 15;	// L761
      int32_t op;	// L762
      op = v525;	// L763
      int32_t v527 = instr;	// L764
      int32_t v528 = v527 >> 4;	// L765
      int32_t v529 = v528 & 15;	// L766
      int32_t dst;	// L767
      dst = v529;	// L768
      int32_t v531 = instr;	// L769
      int32_t v532 = v531 >> 8;	// L770
      int32_t v533 = v532 & 15;	// L771
      int32_t s1;	// L772
      s1 = v533;	// L773
      int32_t v535 = instr;	// L774
      int32_t v536 = v535 >> 12;	// L775
      int32_t v537 = v536 & 15;	// L776
      int32_t s2;	// L777
      s2 = v537;	// L778
      half a;	// L779
      a = half(0.000000f);	// L780
      half b;	// L781
      b = half(0.000000f);	// L782
      int32_t v541 = s1;	// L783
      bool v542 = v541 >= 12;	// L784
      if (v542) {	// L785
        int32_t v543 = s1;	// L786
        int32_t v544 = v543 & 3;	// L787
        int v545 = v544;	// L788
        half v546 = hold_v[v545][0];	// L789
        a = v546;	// L790
      } else {
        int32_t v547 = s1;	// L792
        int v548 = v547;	// L793
        half v549 = drf[v548];	// L794
        a = v549;	// L795
      }
      int32_t v550 = s2;	// L797
      bool v551 = v550 >= 12;	// L798
      if (v551) {	// L799
        int32_t v552 = s2;	// L800
        int32_t v553 = v552 & 3;	// L801
        int v554 = v553;	// L802
        half v555 = hold_v[v554][0];	// L803
        b = v555;	// L804
      } else {
        int32_t v556 = s2;	// L806
        int v557 = v556;	// L807
        half v558 = drf[v557];	// L808
        b = v558;	// L809
      }
      int32_t a_vld;	// L811
      a_vld = 1;	// L812
      int32_t b_vld;	// L813
      b_vld = 1;	// L814
      int32_t v561 = s1;	// L815
      bool v562 = v561 >= 12;	// L816
      if (v562) {	// L817
        a_vld = 0;	// L818
        int32_t v563 = s1;	// L819
        int32_t v564 = v563 & 3;	// L820
        int v565 = v564;	// L821
        uint8_t v566 = hold_cnt[v565];	// L822
        int32_t v567 = v566;	// L823
        bool v568 = v567 > 0;	// L824
        if (v568) {	// L825
          a_vld = 1;	// L826
        }
      }
      int32_t v569 = s2;	// L829
      bool v570 = v569 >= 12;	// L830
      if (v570) {	// L831
        b_vld = 0;	// L832
        int32_t v571 = s2;	// L833
        int32_t v572 = v571 & 3;	// L834
        int v573 = v572;	// L835
        uint8_t v574 = hold_cnt[v573];	// L836
        int32_t v575 = v574;	// L837
        bool v576 = v575 > 0;	// L838
        if (v576) {	// L839
          b_vld = 1;	// L840
        }
      }
      int32_t v577 = s1;	// L843
      bool v578 = v577 < 8;	// L844
      int32_t v579 = dsmask;	// L845
      int32_t v580 = v579 >> v577;	// L847
      int32_t v581 = v580 & 1;	// L848
      bool v582 = v581 == 1;	// L849
      bool v583 = v578 & v582;	// L850
      if (v583) {	// L851
        int32_t v584 = s1;	// L852
        int v585 = v584;	// L853
        int32_t v586 = drf_full[v585];	// L854
        bool v587 = v586 == 0;	// L855
        if (v587) {	// L856
          a_vld = 0;	// L857
        }
      }
      int32_t v588 = s2;	// L860
      bool v589 = v588 < 8;	// L861
      int32_t v590 = dsmask;	// L862
      int32_t v591 = v590 >> v588;	// L864
      int32_t v592 = v591 & 1;	// L865
      bool v593 = v592 == 1;	// L866
      bool v594 = v589 & v593;	// L867
      if (v594) {	// L868
        int32_t v595 = s2;	// L869
        int v596 = v595;	// L870
        int32_t v597 = drf_full[v596];	// L871
        bool v598 = v597 == 0;	// L872
        if (v598) {	// L873
          b_vld = 0;	// L874
        }
      }
      int32_t binop;	// L877
      binop = 0;	// L878
      int32_t v600 = op;	// L879
      bool v601 = v600 == 0;	// L880
      bool v602 = v600 == 1;	// L882
      bool v603 = v600 == 2;	// L884
      bool v604 = v600 == 8;	// L886
      bool v605 = v600 == 9;	// L888
      bool v606 = v601 | v602;	// L889
      bool v607 = v606 | v603;	// L890
      bool v608 = v607 | v604;	// L891
      bool v609 = v608 | v605;	// L892
      if (v609) {	// L893
        binop = 1;	// L894
      }
      int32_t raw;	// L896
      raw = 0;	// L897
      int32_t cmp_busy;	// L898
      cmp_busy = 0;	// L899
      int32_t fwd_a;	// L900
      fwd_a = 0;	// L901
      int32_t fwd_a_ix;	// L902
      fwd_a_ix = 0;	// L903
      int32_t raw_a;	// L904
      raw_a = 0;	// L905
      int32_t fwd_b;	// L906
      fwd_b = 0;	// L907
      int32_t fwd_b_ix;	// L908
      fwd_b_ix = 0;	// L909
      int32_t raw_b;	// L910
      raw_b = 0;	// L911
      l_S_k_7_k: for (int k = 0; k < 4; k++) {	// L912
        ac_int<34, true> v619 = k;	// L913
        ac_int<34, true> v620 = v619 + 1;	// L914
        int32_t v621 = v620;	// L915
        int32_t kk;	// L916
        kk = v621;	// L917
        int32_t v623 = kk;	// L918
        ac_int<34, true> v624 = v623;	// L919
        ac_int<34, true> v625 = 4 - v624;	// L920
        int32_t v626 = v625;	// L921
        int32_t inflight;	// L922
        inflight = v626;	// L923
        int32_t need;	// L924
        need = 0;	// L925
        int32_t v629 = kk;	// L926
        int v630 = v629;	// L927
        uint8_t v631 = sb_long[v630];	// L928
        int32_t v632 = v631;	// L929
        bool v633 = v632 == 1;	// L930
        if (v633) {	// L931
          need = 1;	// L932
        }
        int32_t rdy;	// L934
        rdy = 0;	// L935
        int32_t v635 = inflight;	// L936
        int32_t v636 = need;	// L937
        bool v637 = v635 >= v636;	// L938
        if (v637) {	// L939
          rdy = 1;	// L940
        }
        int32_t v638 = kk;	// L942
        int v639 = v638;	// L943
        uint8_t v640 = sb_v[v639];	// L944
        int32_t v641 = v640;	// L945
        bool v642 = v641 == 1;	// L946
        uint8_t v643 = sb_rtr[v639];	// L949
        int32_t v644 = v643;	// L950
        bool v645 = v644 == 0;	// L951
        uint8_t v646 = sb_dst[v639];	// L954
        int32_t v647 = v646;	// L955
        bool v648 = v647 < 12;	// L956
        bool v649 = v642 & v645;	// L957
        bool v650 = v649 & v648;	// L958
        if (v650) {	// L959
          int32_t v651 = s1;	// L960
          bool v652 = v651 < 12;	// L961
          int32_t v653 = kk;	// L962
          int v654 = v653;	// L963
          uint8_t v655 = sb_dst[v654];	// L964
          int32_t v656 = v655;	// L965
          int32_t v657 = v656 & 7;	// L966
          int32_t v658 = v651 & 7;	// L968
          bool v659 = v657 == v658;	// L969
          bool v660 = v652 & v659;	// L970
          if (v660) {	// L971
            int32_t v661 = rdy;	// L972
            bool v662 = v661 == 1;	// L973
            if (v662) {	// L974
              fwd_a = 1;	// L975
              int32_t v663 = kk;	// L976
              int v664 = v663;	// L977
              uint8_t v665 = sb_ix[v664];	// L978
              int32_t v666 = v665;	// L979
              fwd_a_ix = v666;	// L980
              raw_a = 0;	// L981
            } else {
              fwd_a = 0;	// L983
              raw_a = 1;	// L984
            }
          }
          int32_t v667 = binop;	// L987
          bool v668 = v667 == 1;	// L988
          int32_t v669 = s2;	// L989
          bool v670 = v669 < 12;	// L990
          int32_t v671 = kk;	// L991
          int v672 = v671;	// L992
          uint8_t v673 = sb_dst[v672];	// L993
          int32_t v674 = v673;	// L994
          int32_t v675 = v674 & 7;	// L995
          int32_t v676 = v669 & 7;	// L997
          bool v677 = v675 == v676;	// L998
          bool v678 = v668 & v670;	// L999
          bool v679 = v678 & v677;	// L1000
          if (v679) {	// L1001
            int32_t v680 = rdy;	// L1002
            bool v681 = v680 == 1;	// L1003
            if (v681) {	// L1004
              fwd_b = 1;	// L1005
              int32_t v682 = kk;	// L1006
              int v683 = v682;	// L1007
              uint8_t v684 = sb_ix[v683];	// L1008
              int32_t v685 = v684;	// L1009
              fwd_b_ix = v685;	// L1010
              raw_b = 0;	// L1011
            } else {
              fwd_b = 0;	// L1013
              raw_b = 1;	// L1014
            }
          }
        }
        int32_t v686 = kk;	// L1018
        int v687 = v686;	// L1019
        uint8_t v688 = sb_v[v687];	// L1020
        int32_t v689 = v688;	// L1021
        bool v690 = v689 == 1;	// L1022
        uint8_t v691 = sb_cmp[v687];	// L1025
        int32_t v692 = v691;	// L1026
        bool v693 = v692 == 1;	// L1027
        bool v694 = v690 & v693;	// L1028
        if (v694) {	// L1029
          cmp_busy = 1;	// L1030
        }
      }
      int32_t v695 = raw_a;	// L1033
      raw = v695;	// L1034
      int32_t v696 = binop;	// L1035
      bool v697 = v696 == 1;	// L1036
      int32_t v698 = raw_b;	// L1037
      bool v699 = v698 == 1;	// L1038
      bool v700 = v697 & v699;	// L1039
      if (v700) {	// L1040
        raw = 1;	// L1041
      }
      int32_t v701 = fwd_a;	// L1043
      bool v702 = v701 == 1;	// L1044
      if (v702) {	// L1045
        int32_t v703 = fwd_a_ix;	// L1046
        int v704 = v703;	// L1047
        half v705 = resq[v704];	// L1048
        a = v705;	// L1049
        a_vld = 1;	// L1050
      }
      int32_t v706 = fwd_b;	// L1052
      bool v707 = v706 == 1;	// L1053
      if (v707) {	// L1054
        int32_t v708 = fwd_b_ix;	// L1055
        int v709 = v708;	// L1056
        half v710 = resq[v709];	// L1057
        b = v710;	// L1058
        b_vld = 1;	// L1059
      }
      int32_t is_cond;	// L1061
      is_cond = 0;	// L1062
      int32_t v712 = op;	// L1063
      bool v713 = v712 >= 12;	// L1064
      ac_int<33, true> v714 = v712;	// L1066
      bool v715 = v714 <= 15;	// L1067
      bool v716 = v713 & v715;	// L1068
      if (v716) {	// L1069
        is_cond = 1;	// L1070
      }
      int32_t grant;	// L1072
      grant = 0;	// L1073
      int32_t v718 = pc;	// L1074
      bool v719 = v718 >= 0;	// L1075
      if (v719) {	// L1076
        grant = 1;	// L1077
      }
      int32_t v720 = pc;	// L1079
      bool v721 = v720 >= 0;	// L1080
      int32_t v722 = a_vld;	// L1081
      bool v723 = v722 == 0;	// L1082
      int32_t v724 = binop;	// L1083
      bool v725 = v724 == 1;	// L1084
      int32_t v726 = b_vld;	// L1085
      bool v727 = v726 == 0;	// L1086
      bool v728 = v725 & v727;	// L1087
      bool v729 = v723 | v728;	// L1088
      bool v730 = v721 & v729;	// L1089
      if (v730) {	// L1090
        grant = 0;	// L1091
      }
      int32_t v731 = pc;	// L1093
      bool v732 = v731 >= 0;	// L1094
      int32_t v733 = raw;	// L1095
      bool v734 = v733 == 1;	// L1096
      int32_t v735 = is_cond;	// L1097
      bool v736 = v735 == 1;	// L1098
      int32_t v737 = cmp_busy;	// L1099
      bool v738 = v737 == 1;	// L1100
      bool v739 = v736 & v738;	// L1101
      bool v740 = v734 | v739;	// L1102
      bool v741 = v732 & v740;	// L1103
      if (v741) {	// L1104
        grant = 0;	// L1105
      }
      int32_t v742 = retire_ok;	// L1107
      bool v743 = v742 == 0;	// L1108
      if (v743) {	// L1109
        grant = 0;	// L1110
      }
      int32_t v744 = grant;	// L1112
      bool v745 = v744 == 1;	// L1113
      if (v745) {	// L1114
        int8_t v746 = instr_cnt;	// L1115
        int32_t v747 = cfg_isz;	// L1116
        int32_t v748 = v746;	// L1117
        bool v749 = v748 == v747;	// L1118
        if (v749) {	// L1119
          instr_cnt = 0;	// L1120
          int8_t v750 = iter_cnt;	// L1121
          int32_t v751 = cfg_itsz;	// L1122
          ac_int<33, true> v752 = v751;	// L1123
          ac_int<33, true> v753 = v752 - 1;	// L1124
          ac_int<33, true> v754 = v750;	// L1125
          bool v755 = v754 == v753;	// L1126
          if (v755) {	// L1127
            fetch_en = 0;	// L1128
          } else {
            int8_t v756 = iter_cnt;	// L1130
            ac_int<33, true> v757 = v756;	// L1131
            ac_int<33, true> v758 = v757 + 1;	// L1132
            uint8_t v759 = v758;	// L1133
            iter_cnt = v759;	// L1134
          }
        } else {
          int8_t v760 = instr_cnt;	// L1137
          ac_int<33, true> v761 = v760;	// L1138
          ac_int<33, true> v762 = v761 + 1;	// L1139
          uint8_t v763 = v762;	// L1140
          instr_cnt = v763;	// L1141
        }
      }
      int32_t c1;	// L1144
      c1 = -1;	// L1145
      int32_t c2;	// L1146
      c2 = -1;	// L1147
      int32_t v766 = grant;	// L1148
      bool v767 = v766 == 1;	// L1149
      int32_t v768 = s1;	// L1150
      bool v769 = v768 >= 12;	// L1151
      bool v770 = v767 & v769;	// L1152
      if (v770) {	// L1153
        int32_t v771 = s1;	// L1154
        int32_t v772 = v771 & 3;	// L1155
        c1 = v772;	// L1156
      }
      int32_t v773 = grant;	// L1158
      bool v774 = v773 == 1;	// L1159
      int32_t v775 = s2;	// L1160
      bool v776 = v775 >= 12;	// L1161
      bool v777 = v774 & v776;	// L1162
      if (v777) {	// L1163
        int32_t v778 = s2;	// L1164
        int32_t v779 = v778 & 3;	// L1165
        c2 = v779;	// L1166
      }
      int32_t v780 = c1;	// L1168
      bool v781 = v780 >= 0;	// L1169
      if (v781) {	// L1170
        int32_t v782 = c1;	// L1171
        int v783 = v782;	// L1172
        half v784 = hold_v[v783][1];	// L1173
        hold_v[v783][0] = v784;	// L1176
        int32_t v785 = c1;	// L1177
        int v786 = v785;	// L1178
        uint8_t v787 = hold_cnt[v786];	// L1179
        ac_int<33, true> v788 = v787;	// L1180
        ac_int<33, true> v789 = v788 - 1;	// L1181
        uint8_t v790 = v789;	// L1182
        hold_cnt[v786] = v790;	// L1185
      }
      int32_t v791 = c2;	// L1187
      bool v792 = v791 >= 0;	// L1188
      int32_t v793 = c1;	// L1190
      bool v794 = v791 != v793;	// L1191
      bool v795 = v792 & v794;	// L1192
      if (v795) {	// L1193
        int32_t v796 = c2;	// L1194
        int v797 = v796;	// L1195
        half v798 = hold_v[v797][1];	// L1196
        hold_v[v797][0] = v798;	// L1199
        int32_t v799 = c2;	// L1200
        int v800 = v799;	// L1201
        uint8_t v801 = hold_cnt[v800];	// L1202
        ac_int<33, true> v802 = v801;	// L1203
        ac_int<33, true> v803 = v802 - 1;	// L1204
        uint8_t v804 = v803;	// L1205
        hold_cnt[v800] = v804;	// L1208
      }
      l_S_d_8_d4: for (int d4 = 0; d4 < 4; d4++) {	// L1210
        sc_r[d4] = 0;	// L1211
      }
      int32_t v806 = c1;	// L1213
      bool v807 = v806 >= 0;	// L1214
      if (v807) {	// L1215
        int32_t v808 = c1;	// L1216
        int v809 = v808;	// L1217
        sc_r[v809] = 1;	// L1218
      }
      int32_t v810 = c2;	// L1220
      bool v811 = v810 >= 0;	// L1221
      int32_t v812 = c1;	// L1223
      bool v813 = v810 != v812;	// L1224
      bool v814 = v811 & v813;	// L1225
      if (v814) {	// L1226
        int32_t v815 = c2;	// L1227
        int v816 = v815;	// L1228
        sc_r[v816] = 1;	// L1229
      }
      int32_t v817 = grant;	// L1231
      bool v818 = v817 == 1;	// L1232
      int32_t v819 = s1;	// L1233
      bool v820 = v819 < 8;	// L1234
      int32_t v821 = dsmask;	// L1235
      int32_t v822 = v821 >> v819;	// L1237
      int32_t v823 = v822 & 1;	// L1238
      bool v824 = v823 == 1;	// L1239
      bool v825 = v818 & v820;	// L1240
      bool v826 = v825 & v824;	// L1241
      if (v826) {	// L1242
        int32_t v827 = s1;	// L1243
        int v828 = v827;	// L1244
        drf_full[v828] = 0;	// L1245
      }
      int32_t v829 = grant;	// L1247
      bool v830 = v829 == 1;	// L1248
      int32_t v831 = s2;	// L1249
      bool v832 = v831 < 8;	// L1250
      int32_t v833 = dsmask;	// L1251
      int32_t v834 = v833 >> v831;	// L1253
      int32_t v835 = v834 & 1;	// L1254
      bool v836 = v835 == 1;	// L1255
      bool v837 = v830 & v832;	// L1256
      bool v838 = v837 & v836;	// L1257
      if (v838) {	// L1258
        int32_t v839 = s2;	// L1259
        int v840 = v839;	// L1260
        drf_full[v840] = 0;	// L1261
      }
      half res;	// L1263
      res = half(0.000000f);	// L1264
      int32_t v842 = op;	// L1265
      bool v843 = v842 == 0;	// L1266
      if (v843) {	// L1267
        half v844 = a;	// L1268
        half v845 = b;	// L1269
        half v846 = v844 + v845;	// L1270
        res = v846;	// L1271
      } else {
        int32_t v847 = op;	// L1273
        bool v848 = v847 == 1;	// L1274
        if (v848) {	// L1275
          half v849 = a;	// L1276
          half v850 = b;	// L1277
          half v851 = v849 - v850;	// L1278
          res = v851;	// L1279
        } else {
          int32_t v852 = op;	// L1281
          bool v853 = v852 == 2;	// L1282
          if (v853) {	// L1283
            half v854 = a;	// L1284
            half v855 = b;	// L1285
            half v856 = v854 * v855;	// L1286
            res = v856;	// L1287
          } else {
            int32_t v857 = op;	// L1289
            bool v858 = v857 == 8;	// L1290
            if (v858) {	// L1291
              half v859 = a;	// L1292
              half v860 = b;	// L1293
              bool v861 = v859 >= v860;	// L1294
              if (v861) {	// L1295
                res = half(1.000000f);	// L1296
              } else {
                res = half(-1.000000f);	// L1298
              }
            } else {
              int32_t v862 = op;	// L1301
              bool v863 = v862 == 9;	// L1302
              if (v863) {	// L1303
                half v864 = a;	// L1304
                half v865 = b;	// L1305
                bool v866 = v864 < v865;	// L1306
                if (v866) {	// L1307
                  res = half(1.000000f);	// L1308
                } else {
                  res = half(-1.000000f);	// L1310
                }
              } else {
                half v867 = a;	// L1313
                res = v867;	// L1314
              }
            }
          }
        }
      }
      int32_t v868 = a_vld;	// L1320
      int32_t res_vld;	// L1321
      res_vld = v868;	// L1322
      int32_t v870 = op;	// L1323
      bool v871 = v870 == 0;	// L1324
      bool v872 = v870 == 1;	// L1326
      bool v873 = v870 == 2;	// L1328
      bool v874 = v870 == 8;	// L1330
      bool v875 = v870 == 9;	// L1332
      bool v876 = v871 | v872;	// L1333
      bool v877 = v876 | v873;	// L1334
      bool v878 = v877 | v874;	// L1335
      bool v879 = v878 | v875;	// L1336
      if (v879) {	// L1337
        int32_t v880 = a_vld;	// L1338
        int32_t v881 = b_vld;	// L1339
        int64_t v882 = v880;	// L1340
        int64_t v883 = v881;	// L1341
        int64_t v884 = v882 * v883;	// L1342
        int32_t v885 = v884;	// L1343
        res_vld = v885;	// L1344
      }
      int32_t v886 = grant;	// L1346
      bool v887 = v886 == 0;	// L1347
      if (v887) {	// L1348
        res_vld = 0;	// L1349
      }
      int32_t is_rtr;	// L1351
      is_rtr = 0;	// L1352
      int32_t v889 = op;	// L1353
      bool v890 = v889 >= 4;	// L1354
      ac_int<33, true> v891 = v889;	// L1356
      bool v892 = v891 <= 7;	// L1357
      bool v893 = v890 & v892;	// L1358
      if (v893) {	// L1359
        is_rtr = 1;	// L1360
      }
      int32_t v894 = retire_ok;	// L1362
      bool v895 = v894 == 1;	// L1363
      if (v895) {	// L1364
        l_S_k_9_k1: for (int k1 = 0; k1 < 4; k1++) {	// L1365
          uint8_t v897 = sb_v[(k1 + 1)];	// L1366
          sb_v[k1] = v897;	// L1367
          uint8_t v898 = sb_dst[(k1 + 1)];	// L1368
          sb_dst[k1] = v898;	// L1369
          uint8_t v899 = sb_cmp[(k1 + 1)];	// L1370
          sb_cmp[k1] = v899;	// L1371
          uint8_t v900 = sb_rtr[(k1 + 1)];	// L1372
          sb_rtr[k1] = v900;	// L1373
          uint8_t v901 = sb_inj[(k1 + 1)];	// L1374
          sb_inj[k1] = v901;	// L1375
          uint8_t v902 = sb_dir[(k1 + 1)];	// L1376
          sb_dir[k1] = v902;	// L1377
          uint8_t v903 = sb_id[(k1 + 1)];	// L1378
          sb_id[k1] = v903;	// L1379
          uint8_t v904 = sb_rvld[(k1 + 1)];	// L1380
          sb_rvld[k1] = v904;	// L1381
          uint8_t v905 = sb_ix[(k1 + 1)];	// L1382
          sb_ix[k1] = v905;	// L1383
          uint8_t v906 = sb_long[(k1 + 1)];	// L1384
          sb_long[k1] = v906;	// L1385
        }
        sb_v[4] = 0;	// L1387
      }
      int32_t v907 = grant;	// L1389
      bool v908 = v907 == 1;	// L1390
      if (v908) {	// L1391
        half v909 = res;	// L1392
        int8_t v910 = resq_wr;	// L1393
        int v911 = v910;	// L1394
        resq[v911] = v909;	// L1395
        int32_t cq;	// L1396
        cq = 0;	// L1397
        int32_t v913 = op;	// L1398
        bool v914 = v913 == 8;	// L1399
        if (v914) {	// L1400
          half v915 = a;	// L1401
          half v916 = b;	// L1402
          bool v917 = v915 >= v916;	// L1403
          if (v917) {	// L1404
            cq = 1;	// L1405
          }
        }
        int32_t v918 = op;	// L1408
        bool v919 = v918 == 9;	// L1409
        if (v919) {	// L1410
          half v920 = a;	// L1411
          half v921 = b;	// L1412
          bool v922 = v920 < v921;	// L1413
          if (v922) {	// L1414
            cq = 1;	// L1415
          }
        }
        int32_t v923 = cq;	// L1418
        uint8_t v924 = v923;	// L1419
        int8_t v925 = resq_wr;	// L1420
        int v926 = v925;	// L1421
        cmpq[v926] = v924;	// L1422
        sb_v[4] = 1;	// L1423
        int32_t v927 = dst;	// L1424
        uint8_t v928 = v927;	// L1425
        sb_dst[4] = v928;	// L1426
        int8_t v929 = resq_wr;	// L1427
        sb_ix[4] = v929;	// L1428
        int32_t v930 = binop;	// L1429
        uint8_t v931 = v930;	// L1430
        sb_long[4] = v931;	// L1431
        sb_cmp[4] = 0;	// L1432
        int32_t v932 = op;	// L1433
        bool v933 = v932 == 8;	// L1434
        bool v934 = v932 == 9;	// L1436
        bool v935 = v933 | v934;	// L1437
        if (v935) {	// L1438
          sb_cmp[4] = 1;	// L1439
        }
        int32_t v936 = is_rtr;	// L1441
        int32_t rtrf;	// L1442
        rtrf = v936;	// L1443
        int32_t v938 = is_cond;	// L1444
        bool v939 = v938 == 1;	// L1445
        if (v939) {	// L1446
          rtrf = 1;	// L1447
        }
        int32_t v940 = rtrf;	// L1449
        uint8_t v941 = v940;	// L1450
        sb_rtr[4] = v941;	// L1451
        int32_t v942 = is_rtr;	// L1452
        int32_t inj;	// L1453
        inj = v942;	// L1454
        int32_t v944 = is_cond;	// L1455
        bool v945 = v944 == 1;	// L1456
        int8_t v946 = condition_reg;	// L1457
        int32_t v947 = v946;	// L1458
        bool v948 = v947 == 1;	// L1459
        bool v949 = v945 & v948;	// L1460
        if (v949) {	// L1461
          inj = 1;	// L1462
        }
        int32_t v950 = inj;	// L1464
        uint8_t v951 = v950;	// L1465
        sb_inj[4] = v951;	// L1466
        int32_t v952 = op;	// L1467
        int32_t v953 = v952 & 3;	// L1468
        uint8_t v954 = v953;	// L1469
        sb_dir[4] = v954;	// L1470
        int32_t v955 = s2;	// L1471
        uint8_t v956 = v955;	// L1472
        sb_id[4] = v956;	// L1473
        int32_t v957 = res_vld;	// L1474
        uint8_t v958 = v957;	// L1475
        sb_rvld[4] = v958;	// L1476
        int8_t v959 = resq_wr;	// L1477
        ac_int<33, true> v960 = v959;	// L1478
        ac_int<33, true> v961 = v960 + 1;	// L1479
        ac_int<33, true> v962 = v961 & 7;	// L1480
        uint8_t v963 = v962;	// L1481
        resq_wr = v963;	// L1482
      }
      txn_r = 0;	// L1484
      txs_r = 0;	// L1485
      txw_r = 0;	// L1486
      txe_r = 0;	// L1487
      int32_t v964 = txp_v[0];	// L1488
      bool v965 = v964 == 1;	// L1489
      int32_t v966 = scred[0];	// L1490
      bool v967 = v966 > 0;	// L1491
      bool v968 = v965 & v967;	// L1492
      if (v968) {	// L1493
        ac_int<17, false> twn;	// L1494
        twn = 0;	// L1495
        ac_int<17, true> v970 = twn;	// L1496
        ac_int<17, true> v971;
        ac_int<17, true> _bs_v971 = v970;
        _bs_v971[0] = 1;
        v971 = _bs_v971;	// L1497
        twn = v971;	// L1498
        half v972 = txp_d[0];	// L1499
        uint16_t v973;
        half _bc_v973 = v972;
        std::memcpy(&v973, &_bc_v973, sizeof(v973));	// L1500
        ac_int<17, true> v974 = twn;	// L1501
        ac_int<17, true> v975;
        ac_int<17, true> _bs_v975 = v974;
        _bs_v975.set_slc(1, ac_int<16, false>(v973));
        v975 = _bs_v975;	// L1502
        twn = v975;	// L1503
        ac_int<17, true> v976 = twn;	// L1504
        txn_r = v976;	// L1505
        txp_v[0] = 0;	// L1506
        int32_t v977 = scred[0];	// L1507
        ac_int<33, true> v978 = v977;	// L1508
        ac_int<33, true> v979 = v978 - 1;	// L1509
        int32_t v980 = v979;	// L1510
        scred[0] = v980;	// L1511
      }
      int32_t v981 = txp_v[1];	// L1513
      bool v982 = v981 == 1;	// L1514
      int32_t v983 = scred[1];	// L1515
      bool v984 = v983 > 0;	// L1516
      bool v985 = v982 & v984;	// L1517
      if (v985) {	// L1518
        ac_int<17, false> tws;	// L1519
        tws = 0;	// L1520
        ac_int<17, true> v987 = tws;	// L1521
        ac_int<17, true> v988;
        ac_int<17, true> _bs_v988 = v987;
        _bs_v988[0] = 1;
        v988 = _bs_v988;	// L1522
        tws = v988;	// L1523
        half v989 = txp_d[1];	// L1524
        uint16_t v990;
        half _bc_v990 = v989;
        std::memcpy(&v990, &_bc_v990, sizeof(v990));	// L1525
        ac_int<17, true> v991 = tws;	// L1526
        ac_int<17, true> v992;
        ac_int<17, true> _bs_v992 = v991;
        _bs_v992.set_slc(1, ac_int<16, false>(v990));
        v992 = _bs_v992;	// L1527
        tws = v992;	// L1528
        ac_int<17, true> v993 = tws;	// L1529
        txs_r = v993;	// L1530
        txp_v[1] = 0;	// L1531
        int32_t v994 = scred[1];	// L1532
        ac_int<33, true> v995 = v994;	// L1533
        ac_int<33, true> v996 = v995 - 1;	// L1534
        int32_t v997 = v996;	// L1535
        scred[1] = v997;	// L1536
      }
      int32_t v998 = txp_v[2];	// L1538
      bool v999 = v998 == 1;	// L1539
      int32_t v1000 = scred[2];	// L1540
      bool v1001 = v1000 > 0;	// L1541
      bool v1002 = v999 & v1001;	// L1542
      if (v1002) {	// L1543
        ac_int<17, false> tww;	// L1544
        tww = 0;	// L1545
        ac_int<17, true> v1004 = tww;	// L1546
        ac_int<17, true> v1005;
        ac_int<17, true> _bs_v1005 = v1004;
        _bs_v1005[0] = 1;
        v1005 = _bs_v1005;	// L1547
        tww = v1005;	// L1548
        half v1006 = txp_d[2];	// L1549
        uint16_t v1007;
        half _bc_v1007 = v1006;
        std::memcpy(&v1007, &_bc_v1007, sizeof(v1007));	// L1550
        ac_int<17, true> v1008 = tww;	// L1551
        ac_int<17, true> v1009;
        ac_int<17, true> _bs_v1009 = v1008;
        _bs_v1009.set_slc(1, ac_int<16, false>(v1007));
        v1009 = _bs_v1009;	// L1552
        tww = v1009;	// L1553
        ac_int<17, true> v1010 = tww;	// L1554
        txw_r = v1010;	// L1555
        txp_v[2] = 0;	// L1556
        int32_t v1011 = scred[2];	// L1557
        ac_int<33, true> v1012 = v1011;	// L1558
        ac_int<33, true> v1013 = v1012 - 1;	// L1559
        int32_t v1014 = v1013;	// L1560
        scred[2] = v1014;	// L1561
      }
      int32_t v1015 = txp_v[3];	// L1563
      bool v1016 = v1015 == 1;	// L1564
      int32_t v1017 = scred[3];	// L1565
      bool v1018 = v1017 > 0;	// L1566
      bool v1019 = v1016 & v1018;	// L1567
      if (v1019) {	// L1568
        ac_int<17, false> twe;	// L1569
        twe = 0;	// L1570
        ac_int<17, true> v1021 = twe;	// L1571
        ac_int<17, true> v1022;
        ac_int<17, true> _bs_v1022 = v1021;
        _bs_v1022[0] = 1;
        v1022 = _bs_v1022;	// L1572
        twe = v1022;	// L1573
        half v1023 = txp_d[3];	// L1574
        uint16_t v1024;
        half _bc_v1024 = v1023;
        std::memcpy(&v1024, &_bc_v1024, sizeof(v1024));	// L1575
        ac_int<17, true> v1025 = twe;	// L1576
        ac_int<17, true> v1026;
        ac_int<17, true> _bs_v1026 = v1025;
        _bs_v1026.set_slc(1, ac_int<16, false>(v1024));
        v1026 = _bs_v1026;	// L1577
        twe = v1026;	// L1578
        ac_int<17, true> v1027 = twe;	// L1579
        txe_r = v1027;	// L1580
        txp_v[3] = 0;	// L1581
        int32_t v1028 = scred[3];	// L1582
        ac_int<33, true> v1029 = v1028;	// L1583
        ac_int<33, true> v1030 = v1029 - 1;	// L1584
        int32_t v1031 = v1030;	// L1585
        scred[3] = v1031;	// L1586
      }
      int32_t v1032 = crv_vld;	// L1588
      bool v1033 = v1032 == 1;	// L1589
      if (v1033) {	// L1590
        int32_t v1034 = crv_mode;	// L1591
        bool v1035 = v1034 == 1;	// L1592
        if (v1035) {	// L1593
          int32_t v1036 = crv_addr;	// L1594
          int32_t v1037 = v1036 >> 3;	// L1595
          int32_t v1038 = v1037 & 1;	// L1596
          bool v1039 = v1038 == 1;	// L1597
          if (v1039) {	// L1598
            int32_t v1040 = crv_raw;	// L1599
            int32_t v1041 = crv_addr;	// L1600
            int32_t v1042 = v1041 & 7;	// L1601
            int v1043 = v1042;	// L1602
            irf[v1043] = v1040;	// L1603
          } else {
            int32_t v1044 = crv_addr;	// L1605
            bool v1045 = v1044 == 0;	// L1606
            if (v1045) {	// L1607
              int32_t v1046 = crv_raw;	// L1608
              int32_t v1047 = v1046 & 255;	// L1609
              dsmask = v1047;	// L1610
              int32_t v1048 = crv_raw;	// L1611
              int32_t v1049 = v1048 >> 8;	// L1612
              int32_t v1050 = v1049 & 7;	// L1613
              cfg_isz = v1050;	// L1614
              int32_t v1051 = crv_raw;	// L1615
              int32_t v1052 = v1051 >> 15;	// L1616
              int32_t v1053 = v1052 & 1;	// L1617
              bool v1054 = v1053 == 1;	// L1618
              if (v1054) {	// L1619
                fetch_en = 1;	// L1620
                instr_cnt = 0;	// L1621
                iter_cnt = 0;	// L1622
              }
            } else {
              int32_t v1055 = crv_addr;	// L1625
              bool v1056 = v1055 == 1;	// L1626
              if (v1056) {	// L1627
                int32_t v1057 = crv_raw;	// L1628
                int32_t v1058 = v1057 & 255;	// L1629
                cfg_itsz = v1058;	// L1630
              }
            }
          }
        } else {
          int32_t v1059 = crv_addr;	// L1635
          int32_t v1060 = v1059 >> 2;	// L1636
          int32_t v1061 = v1060 & 3;	// L1637
          bool v1062 = v1061 == 3;	// L1638
          if (v1062) {	// L1639
            int32_t v1063 = crv_addr;	// L1640
            int32_t v1064 = v1063 & 3;	// L1641
            int v1065 = v1064;	// L1642
            txp_v[v1065] = 1;	// L1643
            half v1066 = crv_data;	// L1644
            int32_t v1067 = crv_addr;	// L1645
            int32_t v1068 = v1067 & 3;	// L1646
            int v1069 = v1068;	// L1647
            txp_d[v1069] = v1066;	// L1648
            int32_t v1070 = crv_addr;	// L1649
            int32_t v1071 = v1070 & 3;	// L1650
            int v1072 = v1071;	// L1651
            txp_r[v1072] = 1;	// L1652
          } else {
            int32_t v1073 = crv_addr;	// L1654
            bool v1074 = v1073 < 8;	// L1655
            int32_t v1075 = dsmask;	// L1656
            int32_t v1076 = v1075 >> v1073;	// L1658
            int32_t v1077 = v1076 & 1;	// L1659
            bool v1078 = v1077 == 1;	// L1660
            bool v1079 = v1074 & v1078;	// L1661
            if (v1079) {	// L1662
              int32_t v1080 = crv_addr;	// L1663
              int v1081 = v1080;	// L1664
              int32_t v1082 = drf_full[v1081];	// L1665
              bool v1083 = v1082 == 0;	// L1666
              if (v1083) {	// L1667
                half v1084 = crv_data;	// L1668
                int32_t v1085 = crv_addr;	// L1669
                int v1086 = v1085;	// L1670
                drf[v1086] = v1084;	// L1671
                int32_t v1087 = crv_addr;	// L1672
                int v1088 = v1087;	// L1673
                drf_full[v1088] = 1;	// L1674
              }
            } else {
              half v1089 = crv_data;	// L1677
              int32_t v1090 = crv_addr;	// L1678
              int v1091 = v1090;	// L1679
              drf[v1091] = v1089;	// L1680
            }
          }
        }
      }
      ac_int<26, true> v1092 = oe_r;	// L1685
      v1.Push(v1092);	// L1686
      ac_int<26, true> v1093 = ow_r;	// L1687
      v2.Push(v1093);	// L1688
      ac_int<26, true> v1094 = os_r;	// L1689
      v3.Push(v1094);	// L1690
      ac_int<26, true> v1095 = on_r;	// L1691
      v4.Push(v1095);	// L1692
      ac_int<17, true> v1096 = txe_r;	// L1693
      v5.Push(v1096);	// L1694
      ac_int<17, true> v1097 = txw_r;	// L1695
      v6.Push(v1097);	// L1696
      ac_int<17, true> v1098 = txs_r;	// L1697
      v7.Push(v1098);	// L1698
      ac_int<17, true> v1099 = txn_r;	// L1699
      v8.Push(v1099);	// L1700
      int8_t v1100 = cre_r;	// L1701
      v9.Push(v1100);	// L1702
      int8_t v1101 = crw_r;	// L1703
      v10.Push(v1101);	// L1704
      int8_t v1102 = crs_r;	// L1705
      v11.Push(v1102);	// L1706
      int8_t v1103 = crn_r;	// L1707
      v12.Push(v1103);	// L1708
      int32_t v1104 = sc_r[0];	// L1709
      v15.Push(v1104);	// L1710
      int32_t v1105 = sc_r[1];	// L1711
      v16.Push(v1105);	// L1712
      int32_t v1106 = sc_r[2];	// L1713
      v13.Push(v1106);	// L1714
      int32_t v1107 = sc_r[3];	// L1715
      v14.Push(v1107);	// L1716
    }
#ifndef __SYNTHESIS__
    __allo_done++; // csim: this kernel finished its single pass
#endif
    while (1) { wait(); }
  }
};

SC_MODULE(drv_w_0) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::Out< ac_int<25, false> > v1108_req;
  Connections::In< half > v1108_rsp;
  Connections::Out< ac_int<41, false> > v1109_req;
  Connections::In< int32_t > v1109_rsp;
  Connections::Out< ac_int<34, false> > v1110_req;
  Connections::In< int32_t > v1110_rsp;
  Connections::Out< ac_int<17, true> > v1111;
  Connections::In< int32_t > v1112;
  SC_HAS_PROCESS(drv_w_0);
  drv_w_0(sc_module_name n) : sc_module(n), v1108_req("v1108_req"), v1108_rsp("v1108_rsp"), v1109_req("v1109_req"), v1109_rsp("v1109_rsp"), v1110_req("v1110_req"), v1110_rsp("v1110_rsp"), v1111("v1111"), v1112("v1112") {
    SC_THREAD(run);
    sensitive << clk.pos();
    async_reset_signal_is(rst, false);
  }
  void run() {
    v1108_req.Reset();
    v1108_rsp.Reset();
    v1109_req.Reset();
    v1109_rsp.Reset();
    v1110_req.Reset();
    v1110_rsp.Reset();
    v1111.Reset();
    v1112.Reset();
    wait();
    int32_t dcred[1];	// L1729
    for (int v1114 = 0; v1114 < 1; v1114++) {	// L1730
      dcred[v1114] = 0;	// L1730
    }
    int32_t sp[1];	// L1731
    for (int v1116 = 0; v1116 < 1; v1116++) {	// L1732
      sp[v1116] = 0;	// L1732
    }
    ac_int<17, false> zw;	// L1733
    zw = 0;	// L1734
    int32_t v1118;
    v1110_req.Push( (ac_int<34, false>)(((0) + (0))) << 1 );
    v1118 = v1110_rsp.Pop();	// L1735
    ac_int<33, true> v1119 = v1118;	// L1736
    ac_int<33, true> v1120 = v1119 - 1;	// L1737
    int v1121 = v1120;	// L1738
    for (int v1122 = 0; v1122 < v1121; v1122 += 1) {	// L1739
      ac_int<17, true> v1123 = zw;	// L1740
      v1111.Push(v1123);	// L1741
    }
    l_S_t_1_t1: for (int t1 = 0; t1 < 215; t1++) {	// L1743
      int32_t v1125 = v1112.Pop();	// L1744
      int32_t v1126 = dcred[0];	// L1745
      ac_int<33, true> v1127 = v1126;	// L1746
      ac_int<33, true> v1128 = v1125;	// L1747
      ac_int<33, true> v1129 = v1127 + v1128;	// L1748
      int32_t v1130 = v1129;	// L1749
      dcred[0] = v1130;	// L1750
      ac_int<17, false> w;	// L1751
      w = 0;	// L1752
      int32_t v1132 = sp[0];	// L1753
      bool v1133 = v1132 < 215;	// L1754
      ac_int<33, true> v1134 = t1;	// L1756
      ac_int<33, true> v1135 = v1132;	// L1757
      bool v1136 = v1134 >= v1135;	// L1758
      bool v1137 = v1133 & v1136;	// L1759
      if (v1137) {	// L1760
        int32_t v1138 = sp[0];	// L1761
        int v1139 = v1138;	// L1762
        int32_t v1140;
        v1109_req.Push( (ac_int<41, false>)(((0) * 215 + (v1139))) << 1 );
        v1140 = v1109_rsp.Pop();	// L1763
        bool v1141 = v1140 == 0;	// L1764
        if (v1141) {	// L1765
          int32_t v1142 = sp[0];	// L1766
          ac_int<33, true> v1143 = v1142;	// L1767
          ac_int<33, true> v1144 = v1143 + 1;	// L1768
          int32_t v1145 = v1144;	// L1769
          sp[0] = v1145;	// L1770
        } else {
          int32_t v1146 = dcred[0];	// L1772
          bool v1147 = v1146 > 0;	// L1773
          if (v1147) {	// L1774
            ac_int<17, true> v1148 = w;	// L1775
            ac_int<17, true> v1149;
            ac_int<17, true> _bs_v1149 = v1148;
            _bs_v1149[0] = 1;
            v1149 = _bs_v1149;	// L1776
            w = v1149;	// L1777
            int32_t v1150 = sp[0];	// L1778
            int v1151 = v1150;	// L1779
            half v1152;
            v1108_req.Push( (ac_int<25, false>)(((0) * 215 + (v1151))) << 1 );
            v1152 = v1108_rsp.Pop();	// L1780
            uint16_t v1153;
            half _bc_v1153 = v1152;
            std::memcpy(&v1153, &_bc_v1153, sizeof(v1153));	// L1781
            ac_int<17, true> v1154 = w;	// L1782
            ac_int<17, true> v1155;
            ac_int<17, true> _bs_v1155 = v1154;
            _bs_v1155.set_slc(1, ac_int<16, false>(v1153));
            v1155 = _bs_v1155;	// L1783
            w = v1155;	// L1784
            int32_t v1156 = dcred[0];	// L1785
            ac_int<33, true> v1157 = v1156;	// L1786
            ac_int<33, true> v1158 = v1157 - 1;	// L1787
            int32_t v1159 = v1158;	// L1788
            dcred[0] = v1159;	// L1789
            int32_t v1160 = sp[0];	// L1790
            ac_int<33, true> v1161 = v1160;	// L1791
            ac_int<33, true> v1162 = v1161 + 1;	// L1792
            int32_t v1163 = v1162;	// L1793
            sp[0] = v1163;	// L1794
          }
        }
      }
      ac_int<17, true> v1164 = w;	// L1798
      v1111.Push(v1164);	// L1799
    }
#ifndef __SYNTHESIS__
    __allo_done++; // csim: this kernel finished its single pass
#endif
    while (1) { wait(); }
  }
};

SC_MODULE(drv_e_0) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::Out< ac_int<25, false> > v1165_req;
  Connections::In< half > v1165_rsp;
  Connections::Out< ac_int<41, false> > v1166_req;
  Connections::In< int32_t > v1166_rsp;
  Connections::Out< ac_int<34, false> > v1167_req;
  Connections::In< int32_t > v1167_rsp;
  Connections::Out< ac_int<17, true> > v1168;
  Connections::In< int32_t > v1169;
  SC_HAS_PROCESS(drv_e_0);
  drv_e_0(sc_module_name n) : sc_module(n), v1165_req("v1165_req"), v1165_rsp("v1165_rsp"), v1166_req("v1166_req"), v1166_rsp("v1166_rsp"), v1167_req("v1167_req"), v1167_rsp("v1167_rsp"), v1168("v1168"), v1169("v1169") {
    SC_THREAD(run);
    sensitive << clk.pos();
    async_reset_signal_is(rst, false);
  }
  void run() {
    v1165_req.Reset();
    v1165_rsp.Reset();
    v1166_req.Reset();
    v1166_rsp.Reset();
    v1167_req.Reset();
    v1167_rsp.Reset();
    v1168.Reset();
    v1169.Reset();
    wait();
    int32_t dcred1[1];	// L1812
    for (int v1171 = 0; v1171 < 1; v1171++) {	// L1813
      dcred1[v1171] = 0;	// L1813
    }
    int32_t sp1[1];	// L1814
    for (int v1173 = 0; v1173 < 1; v1173++) {	// L1815
      sp1[v1173] = 0;	// L1815
    }
    ac_int<17, false> zw1;	// L1816
    zw1 = 0;	// L1817
    int32_t v1175;
    v1167_req.Push( (ac_int<34, false>)(((0) + (0))) << 1 );
    v1175 = v1167_rsp.Pop();	// L1818
    ac_int<33, true> v1176 = v1175;	// L1819
    ac_int<33, true> v1177 = v1176 - 1;	// L1820
    int v1178 = v1177;	// L1821
    for (int v1179 = 0; v1179 < v1178; v1179 += 1) {	// L1822
      ac_int<17, true> v1180 = zw1;	// L1823
      v1168.Push(v1180);	// L1824
    }
    l_S_t_1_t2: for (int t2 = 0; t2 < 215; t2++) {	// L1826
      int32_t v1182 = v1169.Pop();	// L1827
      int32_t v1183 = dcred1[0];	// L1828
      ac_int<33, true> v1184 = v1183;	// L1829
      ac_int<33, true> v1185 = v1182;	// L1830
      ac_int<33, true> v1186 = v1184 + v1185;	// L1831
      int32_t v1187 = v1186;	// L1832
      dcred1[0] = v1187;	// L1833
      ac_int<17, false> w1;	// L1834
      w1 = 0;	// L1835
      int32_t v1189 = sp1[0];	// L1836
      bool v1190 = v1189 < 215;	// L1837
      ac_int<33, true> v1191 = t2;	// L1839
      ac_int<33, true> v1192 = v1189;	// L1840
      bool v1193 = v1191 >= v1192;	// L1841
      bool v1194 = v1190 & v1193;	// L1842
      if (v1194) {	// L1843
        int32_t v1195 = sp1[0];	// L1844
        int v1196 = v1195;	// L1845
        int32_t v1197;
        v1166_req.Push( (ac_int<41, false>)(((0) * 215 + (v1196))) << 1 );
        v1197 = v1166_rsp.Pop();	// L1846
        bool v1198 = v1197 == 0;	// L1847
        if (v1198) {	// L1848
          int32_t v1199 = sp1[0];	// L1849
          ac_int<33, true> v1200 = v1199;	// L1850
          ac_int<33, true> v1201 = v1200 + 1;	// L1851
          int32_t v1202 = v1201;	// L1852
          sp1[0] = v1202;	// L1853
        } else {
          int32_t v1203 = dcred1[0];	// L1855
          bool v1204 = v1203 > 0;	// L1856
          if (v1204) {	// L1857
            ac_int<17, true> v1205 = w1;	// L1858
            ac_int<17, true> v1206;
            ac_int<17, true> _bs_v1206 = v1205;
            _bs_v1206[0] = 1;
            v1206 = _bs_v1206;	// L1859
            w1 = v1206;	// L1860
            int32_t v1207 = sp1[0];	// L1861
            int v1208 = v1207;	// L1862
            half v1209;
            v1165_req.Push( (ac_int<25, false>)(((0) * 215 + (v1208))) << 1 );
            v1209 = v1165_rsp.Pop();	// L1863
            uint16_t v1210;
            half _bc_v1210 = v1209;
            std::memcpy(&v1210, &_bc_v1210, sizeof(v1210));	// L1864
            ac_int<17, true> v1211 = w1;	// L1865
            ac_int<17, true> v1212;
            ac_int<17, true> _bs_v1212 = v1211;
            _bs_v1212.set_slc(1, ac_int<16, false>(v1210));
            v1212 = _bs_v1212;	// L1866
            w1 = v1212;	// L1867
            int32_t v1213 = dcred1[0];	// L1868
            ac_int<33, true> v1214 = v1213;	// L1869
            ac_int<33, true> v1215 = v1214 - 1;	// L1870
            int32_t v1216 = v1215;	// L1871
            dcred1[0] = v1216;	// L1872
            int32_t v1217 = sp1[0];	// L1873
            ac_int<33, true> v1218 = v1217;	// L1874
            ac_int<33, true> v1219 = v1218 + 1;	// L1875
            int32_t v1220 = v1219;	// L1876
            sp1[0] = v1220;	// L1877
          }
        }
      }
      ac_int<17, true> v1221 = w1;	// L1881
      v1168.Push(v1221);	// L1882
    }
#ifndef __SYNTHESIS__
    __allo_done++; // csim: this kernel finished its single pass
#endif
    while (1) { wait(); }
  }
};

SC_MODULE(drv_n_0) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::Out< ac_int<25, false> > v1222_req;
  Connections::In< half > v1222_rsp;
  Connections::Out< ac_int<41, false> > v1223_req;
  Connections::In< int32_t > v1223_rsp;
  Connections::Out< ac_int<34, false> > v1224_req;
  Connections::In< int32_t > v1224_rsp;
  Connections::Out< ac_int<17, true> > v1225;
  Connections::In< int32_t > v1226;
  SC_HAS_PROCESS(drv_n_0);
  drv_n_0(sc_module_name n) : sc_module(n), v1222_req("v1222_req"), v1222_rsp("v1222_rsp"), v1223_req("v1223_req"), v1223_rsp("v1223_rsp"), v1224_req("v1224_req"), v1224_rsp("v1224_rsp"), v1225("v1225"), v1226("v1226") {
    SC_THREAD(run);
    sensitive << clk.pos();
    async_reset_signal_is(rst, false);
  }
  void run() {
    v1222_req.Reset();
    v1222_rsp.Reset();
    v1223_req.Reset();
    v1223_rsp.Reset();
    v1224_req.Reset();
    v1224_rsp.Reset();
    v1225.Reset();
    v1226.Reset();
    wait();
    int32_t dcred2[1];	// L1895
    for (int v1228 = 0; v1228 < 1; v1228++) {	// L1896
      dcred2[v1228] = 0;	// L1896
    }
    int32_t sp2[1];	// L1897
    for (int v1230 = 0; v1230 < 1; v1230++) {	// L1898
      sp2[v1230] = 0;	// L1898
    }
    ac_int<17, false> zw2;	// L1899
    zw2 = 0;	// L1900
    int32_t v1232;
    v1224_req.Push( (ac_int<34, false>)(((0) + (0))) << 1 );
    v1232 = v1224_rsp.Pop();	// L1901
    ac_int<33, true> v1233 = v1232;	// L1902
    ac_int<33, true> v1234 = v1233 - 1;	// L1903
    int v1235 = v1234;	// L1904
    for (int v1236 = 0; v1236 < v1235; v1236 += 1) {	// L1905
      ac_int<17, true> v1237 = zw2;	// L1906
      v1225.Push(v1237);	// L1907
    }
    l_S_t_1_t3: for (int t3 = 0; t3 < 215; t3++) {	// L1909
      int32_t v1239 = v1226.Pop();	// L1910
      int32_t v1240 = dcred2[0];	// L1911
      ac_int<33, true> v1241 = v1240;	// L1912
      ac_int<33, true> v1242 = v1239;	// L1913
      ac_int<33, true> v1243 = v1241 + v1242;	// L1914
      int32_t v1244 = v1243;	// L1915
      dcred2[0] = v1244;	// L1916
      ac_int<17, false> w2;	// L1917
      w2 = 0;	// L1918
      int32_t v1246 = sp2[0];	// L1919
      bool v1247 = v1246 < 215;	// L1920
      ac_int<33, true> v1248 = t3;	// L1922
      ac_int<33, true> v1249 = v1246;	// L1923
      bool v1250 = v1248 >= v1249;	// L1924
      bool v1251 = v1247 & v1250;	// L1925
      if (v1251) {	// L1926
        int32_t v1252 = sp2[0];	// L1927
        int v1253 = v1252;	// L1928
        int32_t v1254;
        v1223_req.Push( (ac_int<41, false>)(((0) * 215 + (v1253))) << 1 );
        v1254 = v1223_rsp.Pop();	// L1929
        bool v1255 = v1254 == 0;	// L1930
        if (v1255) {	// L1931
          int32_t v1256 = sp2[0];	// L1932
          ac_int<33, true> v1257 = v1256;	// L1933
          ac_int<33, true> v1258 = v1257 + 1;	// L1934
          int32_t v1259 = v1258;	// L1935
          sp2[0] = v1259;	// L1936
        } else {
          int32_t v1260 = dcred2[0];	// L1938
          bool v1261 = v1260 > 0;	// L1939
          if (v1261) {	// L1940
            ac_int<17, true> v1262 = w2;	// L1941
            ac_int<17, true> v1263;
            ac_int<17, true> _bs_v1263 = v1262;
            _bs_v1263[0] = 1;
            v1263 = _bs_v1263;	// L1942
            w2 = v1263;	// L1943
            int32_t v1264 = sp2[0];	// L1944
            int v1265 = v1264;	// L1945
            half v1266;
            v1222_req.Push( (ac_int<25, false>)(((0) * 215 + (v1265))) << 1 );
            v1266 = v1222_rsp.Pop();	// L1946
            uint16_t v1267;
            half _bc_v1267 = v1266;
            std::memcpy(&v1267, &_bc_v1267, sizeof(v1267));	// L1947
            ac_int<17, true> v1268 = w2;	// L1948
            ac_int<17, true> v1269;
            ac_int<17, true> _bs_v1269 = v1268;
            _bs_v1269.set_slc(1, ac_int<16, false>(v1267));
            v1269 = _bs_v1269;	// L1949
            w2 = v1269;	// L1950
            int32_t v1270 = dcred2[0];	// L1951
            ac_int<33, true> v1271 = v1270;	// L1952
            ac_int<33, true> v1272 = v1271 - 1;	// L1953
            int32_t v1273 = v1272;	// L1954
            dcred2[0] = v1273;	// L1955
            int32_t v1274 = sp2[0];	// L1956
            ac_int<33, true> v1275 = v1274;	// L1957
            ac_int<33, true> v1276 = v1275 + 1;	// L1958
            int32_t v1277 = v1276;	// L1959
            sp2[0] = v1277;	// L1960
          }
        }
      }
      ac_int<17, true> v1278 = w2;	// L1964
      v1225.Push(v1278);	// L1965
    }
#ifndef __SYNTHESIS__
    __allo_done++; // csim: this kernel finished its single pass
#endif
    while (1) { wait(); }
  }
};

SC_MODULE(drv_s_0) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::Out< ac_int<25, false> > v1279_req;
  Connections::In< half > v1279_rsp;
  Connections::Out< ac_int<41, false> > v1280_req;
  Connections::In< int32_t > v1280_rsp;
  Connections::Out< ac_int<34, false> > v1281_req;
  Connections::In< int32_t > v1281_rsp;
  Connections::Out< ac_int<17, true> > v1282;
  Connections::In< int32_t > v1283;
  SC_HAS_PROCESS(drv_s_0);
  drv_s_0(sc_module_name n) : sc_module(n), v1279_req("v1279_req"), v1279_rsp("v1279_rsp"), v1280_req("v1280_req"), v1280_rsp("v1280_rsp"), v1281_req("v1281_req"), v1281_rsp("v1281_rsp"), v1282("v1282"), v1283("v1283") {
    SC_THREAD(run);
    sensitive << clk.pos();
    async_reset_signal_is(rst, false);
  }
  void run() {
    v1279_req.Reset();
    v1279_rsp.Reset();
    v1280_req.Reset();
    v1280_rsp.Reset();
    v1281_req.Reset();
    v1281_rsp.Reset();
    v1282.Reset();
    v1283.Reset();
    wait();
    int32_t dcred3[1];	// L1978
    for (int v1285 = 0; v1285 < 1; v1285++) {	// L1979
      dcred3[v1285] = 0;	// L1979
    }
    int32_t sp3[1];	// L1980
    for (int v1287 = 0; v1287 < 1; v1287++) {	// L1981
      sp3[v1287] = 0;	// L1981
    }
    ac_int<17, false> zw3;	// L1982
    zw3 = 0;	// L1983
    int32_t v1289;
    v1281_req.Push( (ac_int<34, false>)(((0) + (0))) << 1 );
    v1289 = v1281_rsp.Pop();	// L1984
    ac_int<33, true> v1290 = v1289;	// L1985
    ac_int<33, true> v1291 = v1290 - 1;	// L1986
    int v1292 = v1291;	// L1987
    for (int v1293 = 0; v1293 < v1292; v1293 += 1) {	// L1988
      ac_int<17, true> v1294 = zw3;	// L1989
      v1282.Push(v1294);	// L1990
    }
    l_S_t_1_t4: for (int t4 = 0; t4 < 215; t4++) {	// L1992
      int32_t v1296 = v1283.Pop();	// L1993
      int32_t v1297 = dcred3[0];	// L1994
      ac_int<33, true> v1298 = v1297;	// L1995
      ac_int<33, true> v1299 = v1296;	// L1996
      ac_int<33, true> v1300 = v1298 + v1299;	// L1997
      int32_t v1301 = v1300;	// L1998
      dcred3[0] = v1301;	// L1999
      ac_int<17, false> w3;	// L2000
      w3 = 0;	// L2001
      int32_t v1303 = sp3[0];	// L2002
      bool v1304 = v1303 < 215;	// L2003
      ac_int<33, true> v1305 = t4;	// L2005
      ac_int<33, true> v1306 = v1303;	// L2006
      bool v1307 = v1305 >= v1306;	// L2007
      bool v1308 = v1304 & v1307;	// L2008
      if (v1308) {	// L2009
        int32_t v1309 = sp3[0];	// L2010
        int v1310 = v1309;	// L2011
        int32_t v1311;
        v1280_req.Push( (ac_int<41, false>)(((0) * 215 + (v1310))) << 1 );
        v1311 = v1280_rsp.Pop();	// L2012
        bool v1312 = v1311 == 0;	// L2013
        if (v1312) {	// L2014
          int32_t v1313 = sp3[0];	// L2015
          ac_int<33, true> v1314 = v1313;	// L2016
          ac_int<33, true> v1315 = v1314 + 1;	// L2017
          int32_t v1316 = v1315;	// L2018
          sp3[0] = v1316;	// L2019
        } else {
          int32_t v1317 = dcred3[0];	// L2021
          bool v1318 = v1317 > 0;	// L2022
          if (v1318) {	// L2023
            ac_int<17, true> v1319 = w3;	// L2024
            ac_int<17, true> v1320;
            ac_int<17, true> _bs_v1320 = v1319;
            _bs_v1320[0] = 1;
            v1320 = _bs_v1320;	// L2025
            w3 = v1320;	// L2026
            int32_t v1321 = sp3[0];	// L2027
            int v1322 = v1321;	// L2028
            half v1323;
            v1279_req.Push( (ac_int<25, false>)(((0) * 215 + (v1322))) << 1 );
            v1323 = v1279_rsp.Pop();	// L2029
            uint16_t v1324;
            half _bc_v1324 = v1323;
            std::memcpy(&v1324, &_bc_v1324, sizeof(v1324));	// L2030
            ac_int<17, true> v1325 = w3;	// L2031
            ac_int<17, true> v1326;
            ac_int<17, true> _bs_v1326 = v1325;
            _bs_v1326.set_slc(1, ac_int<16, false>(v1324));
            v1326 = _bs_v1326;	// L2032
            w3 = v1326;	// L2033
            int32_t v1327 = dcred3[0];	// L2034
            ac_int<33, true> v1328 = v1327;	// L2035
            ac_int<33, true> v1329 = v1328 - 1;	// L2036
            int32_t v1330 = v1329;	// L2037
            dcred3[0] = v1330;	// L2038
            int32_t v1331 = sp3[0];	// L2039
            ac_int<33, true> v1332 = v1331;	// L2040
            ac_int<33, true> v1333 = v1332 + 1;	// L2041
            int32_t v1334 = v1333;	// L2042
            sp3[0] = v1334;	// L2043
          }
        }
      }
      ac_int<17, true> v1335 = w3;	// L2047
      v1282.Push(v1335);	// L2048
    }
#ifndef __SYNTHESIS__
    __allo_done++; // csim: this kernel finished its single pass
#endif
    while (1) { wait(); }
  }
};

SC_MODULE(col_w_0) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::Out< ac_int<25, false> > v1336_req;
  Connections::Out< ac_int<34, false> > v1337_req;
  Connections::In< int32_t > v1337_rsp;
  Connections::Out< int32_t > v1338;
  Connections::In< ac_int<17, true> > v1339;
  SC_HAS_PROCESS(col_w_0);
  col_w_0(sc_module_name n) : sc_module(n), v1336_req("v1336_req"), v1337_req("v1337_req"), v1337_rsp("v1337_rsp"), v1338("v1338"), v1339("v1339") {
    SC_THREAD(run);
    sensitive << clk.pos();
    async_reset_signal_is(rst, false);
  }
  void run() {
    v1336_req.Reset();
    v1337_req.Reset();
    v1337_rsp.Reset();
    v1338.Reset();
    v1339.Reset();
    wait();
    int32_t k2[1];	// L2061
    for (int v1341 = 0; v1341 < 1; v1341++) {	// L2062
      k2[v1341] = 0;	// L2062
    }
    int32_t cret[1];	// L2063
    for (int v1343 = 0; v1343 < 1; v1343++) {	// L2064
      cret[v1343] = 0;	// L2064
    }
    int32_t zc;	// L2065
    zc = 0;	// L2066
    int32_t v1345;
    v1337_req.Push( (ac_int<34, false>)(((0) + (0))) << 1 );
    v1345 = v1337_rsp.Pop();	// L2067
    ac_int<33, true> v1346 = v1345;	// L2068
    ac_int<33, true> v1347 = v1346 - 1;	// L2069
    int v1348 = v1347;	// L2070
    for (int v1349 = 0; v1349 < v1348; v1349 += 1) {	// L2071
      int32_t v1350 = zc;	// L2072
      v1338.Push(v1350);	// L2073
    }
    cret[0] = 2;	// L2075
    int32_t v1351 = cret[0];	// L2076
    v1338.Push(v1351);	// L2077
    l_S_t_1_t5: for (int t5 = 0; t5 < 215; t5++) {	// L2078
      ac_int<17, false> v1353 = v1339.Pop();	// L2079
      ac_int<17, false> w4;	// L2080
      w4 = v1353;	// L2081
      cret[0] = 0;	// L2082
      ac_int<17, true> v1355 = w4;	// L2083
      bool v1356;
      ac_int<17, true> _bs_v1356 = v1355;
      v1356 = _bs_v1356[0];	// L2084
      int32_t v1357 = v1356;	// L2085
      bool v1358 = v1357 == 1;	// L2086
      if (v1358) {	// L2087
        cret[0] = 1;	// L2088
        int32_t v1359 = k2[0];	// L2089
        bool v1360 = v1359 < 215;	// L2090
        if (v1360) {	// L2091
          ac_int<17, true> v1361 = w4;	// L2092
          int16_t v1362;
          ac_int<17, true> _bs_v1362 = v1361;
          v1362 = _bs_v1362.slc<16>(1);	// L2093
          half v1363;
          uint16_t _bc_v1363 = v1362;
          std::memcpy(&v1363, &_bc_v1363, sizeof(v1363));	// L2094
          int32_t v1364 = k2[0];	// L2095
          int v1365 = v1364;	// L2096
          v1336_req.Push( ((ac_int<25, false>)(_fbits(v1363)) << 9) | ((ac_int<25, false>)(((0) * 215 + (v1365))) << 1) | (ac_int<25, false>)1 );	// L2097
          int32_t v1366 = k2[0];	// L2098
          ac_int<33, true> v1367 = v1366;	// L2099
          ac_int<33, true> v1368 = v1367 + 1;	// L2100
          int32_t v1369 = v1368;	// L2101
          k2[0] = v1369;	// L2102
        }
      }
      int32_t v1370 = cret[0];	// L2105
      v1338.Push(v1370);	// L2106
    }
#ifndef __SYNTHESIS__
    __allo_done++; // csim: this kernel finished its single pass
#endif
    while (1) { wait(); }
  }
};

SC_MODULE(col_e_0) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::Out< ac_int<25, false> > v1371_req;
  Connections::Out< ac_int<34, false> > v1372_req;
  Connections::In< int32_t > v1372_rsp;
  Connections::Out< int32_t > v1373;
  Connections::In< ac_int<17, true> > v1374;
  SC_HAS_PROCESS(col_e_0);
  col_e_0(sc_module_name n) : sc_module(n), v1371_req("v1371_req"), v1372_req("v1372_req"), v1372_rsp("v1372_rsp"), v1373("v1373"), v1374("v1374") {
    SC_THREAD(run);
    sensitive << clk.pos();
    async_reset_signal_is(rst, false);
  }
  void run() {
    v1371_req.Reset();
    v1372_req.Reset();
    v1372_rsp.Reset();
    v1373.Reset();
    v1374.Reset();
    wait();
    int32_t k3[1];	// L2119
    for (int v1376 = 0; v1376 < 1; v1376++) {	// L2120
      k3[v1376] = 0;	// L2120
    }
    int32_t cret1[1];	// L2121
    for (int v1378 = 0; v1378 < 1; v1378++) {	// L2122
      cret1[v1378] = 0;	// L2122
    }
    int32_t zc1;	// L2123
    zc1 = 0;	// L2124
    int32_t v1380;
    v1372_req.Push( (ac_int<34, false>)(((0) + (0))) << 1 );
    v1380 = v1372_rsp.Pop();	// L2125
    ac_int<33, true> v1381 = v1380;	// L2126
    ac_int<33, true> v1382 = v1381 - 1;	// L2127
    int v1383 = v1382;	// L2128
    for (int v1384 = 0; v1384 < v1383; v1384 += 1) {	// L2129
      int32_t v1385 = zc1;	// L2130
      v1373.Push(v1385);	// L2131
    }
    cret1[0] = 2;	// L2133
    int32_t v1386 = cret1[0];	// L2134
    v1373.Push(v1386);	// L2135
    l_S_t_1_t6: for (int t6 = 0; t6 < 215; t6++) {	// L2136
      ac_int<17, false> v1388 = v1374.Pop();	// L2137
      ac_int<17, false> w5;	// L2138
      w5 = v1388;	// L2139
      cret1[0] = 0;	// L2140
      ac_int<17, true> v1390 = w5;	// L2141
      bool v1391;
      ac_int<17, true> _bs_v1391 = v1390;
      v1391 = _bs_v1391[0];	// L2142
      int32_t v1392 = v1391;	// L2143
      bool v1393 = v1392 == 1;	// L2144
      if (v1393) {	// L2145
        cret1[0] = 1;	// L2146
        int32_t v1394 = k3[0];	// L2147
        bool v1395 = v1394 < 215;	// L2148
        if (v1395) {	// L2149
          ac_int<17, true> v1396 = w5;	// L2150
          int16_t v1397;
          ac_int<17, true> _bs_v1397 = v1396;
          v1397 = _bs_v1397.slc<16>(1);	// L2151
          half v1398;
          uint16_t _bc_v1398 = v1397;
          std::memcpy(&v1398, &_bc_v1398, sizeof(v1398));	// L2152
          int32_t v1399 = k3[0];	// L2153
          int v1400 = v1399;	// L2154
          v1371_req.Push( ((ac_int<25, false>)(_fbits(v1398)) << 9) | ((ac_int<25, false>)(((0) * 215 + (v1400))) << 1) | (ac_int<25, false>)1 );	// L2155
          int32_t v1401 = k3[0];	// L2156
          ac_int<33, true> v1402 = v1401;	// L2157
          ac_int<33, true> v1403 = v1402 + 1;	// L2158
          int32_t v1404 = v1403;	// L2159
          k3[0] = v1404;	// L2160
        }
      }
      int32_t v1405 = cret1[0];	// L2163
      v1373.Push(v1405);	// L2164
    }
#ifndef __SYNTHESIS__
    __allo_done++; // csim: this kernel finished its single pass
#endif
    while (1) { wait(); }
  }
};

SC_MODULE(col_n_0) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::Out< ac_int<25, false> > v1406_req;
  Connections::Out< ac_int<34, false> > v1407_req;
  Connections::In< int32_t > v1407_rsp;
  Connections::Out< int32_t > v1408;
  Connections::In< ac_int<17, true> > v1409;
  SC_HAS_PROCESS(col_n_0);
  col_n_0(sc_module_name n) : sc_module(n), v1406_req("v1406_req"), v1407_req("v1407_req"), v1407_rsp("v1407_rsp"), v1408("v1408"), v1409("v1409") {
    SC_THREAD(run);
    sensitive << clk.pos();
    async_reset_signal_is(rst, false);
  }
  void run() {
    v1406_req.Reset();
    v1407_req.Reset();
    v1407_rsp.Reset();
    v1408.Reset();
    v1409.Reset();
    wait();
    int32_t k4[1];	// L2177
    for (int v1411 = 0; v1411 < 1; v1411++) {	// L2178
      k4[v1411] = 0;	// L2178
    }
    int32_t cret2[1];	// L2179
    for (int v1413 = 0; v1413 < 1; v1413++) {	// L2180
      cret2[v1413] = 0;	// L2180
    }
    int32_t zc2;	// L2181
    zc2 = 0;	// L2182
    int32_t v1415;
    v1407_req.Push( (ac_int<34, false>)(((0) + (0))) << 1 );
    v1415 = v1407_rsp.Pop();	// L2183
    ac_int<33, true> v1416 = v1415;	// L2184
    ac_int<33, true> v1417 = v1416 - 1;	// L2185
    int v1418 = v1417;	// L2186
    for (int v1419 = 0; v1419 < v1418; v1419 += 1) {	// L2187
      int32_t v1420 = zc2;	// L2188
      v1408.Push(v1420);	// L2189
    }
    cret2[0] = 2;	// L2191
    int32_t v1421 = cret2[0];	// L2192
    v1408.Push(v1421);	// L2193
    l_S_t_1_t7: for (int t7 = 0; t7 < 215; t7++) {	// L2194
      ac_int<17, false> v1423 = v1409.Pop();	// L2195
      ac_int<17, false> w6;	// L2196
      w6 = v1423;	// L2197
      cret2[0] = 0;	// L2198
      ac_int<17, true> v1425 = w6;	// L2199
      bool v1426;
      ac_int<17, true> _bs_v1426 = v1425;
      v1426 = _bs_v1426[0];	// L2200
      int32_t v1427 = v1426;	// L2201
      bool v1428 = v1427 == 1;	// L2202
      if (v1428) {	// L2203
        cret2[0] = 1;	// L2204
        int32_t v1429 = k4[0];	// L2205
        bool v1430 = v1429 < 215;	// L2206
        if (v1430) {	// L2207
          ac_int<17, true> v1431 = w6;	// L2208
          int16_t v1432;
          ac_int<17, true> _bs_v1432 = v1431;
          v1432 = _bs_v1432.slc<16>(1);	// L2209
          half v1433;
          uint16_t _bc_v1433 = v1432;
          std::memcpy(&v1433, &_bc_v1433, sizeof(v1433));	// L2210
          int32_t v1434 = k4[0];	// L2211
          int v1435 = v1434;	// L2212
          v1406_req.Push( ((ac_int<25, false>)(_fbits(v1433)) << 9) | ((ac_int<25, false>)(((0) * 215 + (v1435))) << 1) | (ac_int<25, false>)1 );	// L2213
          int32_t v1436 = k4[0];	// L2214
          ac_int<33, true> v1437 = v1436;	// L2215
          ac_int<33, true> v1438 = v1437 + 1;	// L2216
          int32_t v1439 = v1438;	// L2217
          k4[0] = v1439;	// L2218
        }
      }
      int32_t v1440 = cret2[0];	// L2221
      v1408.Push(v1440);	// L2222
    }
#ifndef __SYNTHESIS__
    __allo_done++; // csim: this kernel finished its single pass
#endif
    while (1) { wait(); }
  }
};

SC_MODULE(col_s_0) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::Out< ac_int<25, false> > v1441_req;
  Connections::Out< ac_int<34, false> > v1442_req;
  Connections::In< int32_t > v1442_rsp;
  Connections::Out< int32_t > v1443;
  Connections::In< ac_int<17, true> > v1444;
  SC_HAS_PROCESS(col_s_0);
  col_s_0(sc_module_name n) : sc_module(n), v1441_req("v1441_req"), v1442_req("v1442_req"), v1442_rsp("v1442_rsp"), v1443("v1443"), v1444("v1444") {
    SC_THREAD(run);
    sensitive << clk.pos();
    async_reset_signal_is(rst, false);
  }
  void run() {
    v1441_req.Reset();
    v1442_req.Reset();
    v1442_rsp.Reset();
    v1443.Reset();
    v1444.Reset();
    wait();
    int32_t k5[1];	// L2235
    for (int v1446 = 0; v1446 < 1; v1446++) {	// L2236
      k5[v1446] = 0;	// L2236
    }
    int32_t cret3[1];	// L2237
    for (int v1448 = 0; v1448 < 1; v1448++) {	// L2238
      cret3[v1448] = 0;	// L2238
    }
    int32_t zc3;	// L2239
    zc3 = 0;	// L2240
    int32_t v1450;
    v1442_req.Push( (ac_int<34, false>)(((0) + (0))) << 1 );
    v1450 = v1442_rsp.Pop();	// L2241
    ac_int<33, true> v1451 = v1450;	// L2242
    ac_int<33, true> v1452 = v1451 - 1;	// L2243
    int v1453 = v1452;	// L2244
    for (int v1454 = 0; v1454 < v1453; v1454 += 1) {	// L2245
      int32_t v1455 = zc3;	// L2246
      v1443.Push(v1455);	// L2247
    }
    cret3[0] = 2;	// L2249
    int32_t v1456 = cret3[0];	// L2250
    v1443.Push(v1456);	// L2251
    l_S_t_1_t8: for (int t8 = 0; t8 < 215; t8++) {	// L2252
      ac_int<17, false> v1458 = v1444.Pop();	// L2253
      ac_int<17, false> w7;	// L2254
      w7 = v1458;	// L2255
      cret3[0] = 0;	// L2256
      ac_int<17, true> v1460 = w7;	// L2257
      bool v1461;
      ac_int<17, true> _bs_v1461 = v1460;
      v1461 = _bs_v1461[0];	// L2258
      int32_t v1462 = v1461;	// L2259
      bool v1463 = v1462 == 1;	// L2260
      if (v1463) {	// L2261
        cret3[0] = 1;	// L2262
        int32_t v1464 = k5[0];	// L2263
        bool v1465 = v1464 < 215;	// L2264
        if (v1465) {	// L2265
          ac_int<17, true> v1466 = w7;	// L2266
          int16_t v1467;
          ac_int<17, true> _bs_v1467 = v1466;
          v1467 = _bs_v1467.slc<16>(1);	// L2267
          half v1468;
          uint16_t _bc_v1468 = v1467;
          std::memcpy(&v1468, &_bc_v1468, sizeof(v1468));	// L2268
          int32_t v1469 = k5[0];	// L2269
          int v1470 = v1469;	// L2270
          v1441_req.Push( ((ac_int<25, false>)(_fbits(v1468)) << 9) | ((ac_int<25, false>)(((0) * 215 + (v1470))) << 1) | (ac_int<25, false>)1 );	// L2271
          int32_t v1471 = k5[0];	// L2272
          ac_int<33, true> v1472 = v1471;	// L2273
          ac_int<33, true> v1473 = v1472 + 1;	// L2274
          int32_t v1474 = v1473;	// L2275
          k5[0] = v1474;	// L2276
        }
      }
      int32_t v1475 = cret3[0];	// L2279
      v1443.Push(v1475);	// L2280
    }
#ifndef __SYNTHESIS__
    __allo_done++; // csim: this kernel finished its single pass
#endif
    while (1) { wait(); }
  }
};

SC_MODULE(rdrv_w_0) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::Out< ac_int<41, false> > v1476_req;
  Connections::In< int32_t > v1476_rsp;
  Connections::Out< ac_int<34, false> > v1477_req;
  Connections::In< int32_t > v1477_rsp;
  Connections::Out< ac_int<26, true> > v1478;
  Connections::In< int32_t > v1479;
  SC_HAS_PROCESS(rdrv_w_0);
  rdrv_w_0(sc_module_name n) : sc_module(n), v1476_req("v1476_req"), v1476_rsp("v1476_rsp"), v1477_req("v1477_req"), v1477_rsp("v1477_rsp"), v1478("v1478"), v1479("v1479") {
    SC_THREAD(run);
    sensitive << clk.pos();
    async_reset_signal_is(rst, false);
  }
  void run() {
    v1476_req.Reset();
    v1476_rsp.Reset();
    v1477_req.Reset();
    v1477_rsp.Reset();
    v1478.Reset();
    v1479.Reset();
    wait();
    int32_t dcred4[1];	// L2292
    for (int v1481 = 0; v1481 < 1; v1481++) {	// L2293
      dcred4[v1481] = 0;	// L2293
    }
    int32_t sp4[1];	// L2294
    for (int v1483 = 0; v1483 < 1; v1483++) {	// L2295
      sp4[v1483] = 0;	// L2295
    }
    ac_int<26, false> zp;	// L2296
    zp = 0;	// L2297
    int32_t v1485;
    v1477_req.Push( (ac_int<34, false>)(((0) + (0))) << 1 );
    v1485 = v1477_rsp.Pop();	// L2298
    ac_int<33, true> v1486 = v1485;	// L2299
    ac_int<33, true> v1487 = v1486 - 1;	// L2300
    int v1488 = v1487;	// L2301
    for (int v1489 = 0; v1489 < v1488; v1489 += 1) {	// L2302
      ac_int<26, true> v1490 = zp;	// L2303
      v1478.Push(v1490);	// L2304
    }
    l_S_t_1_t9: for (int t9 = 0; t9 < 215; t9++) {	// L2306
      int32_t v1492 = v1479.Pop();	// L2307
      int32_t v1493 = dcred4[0];	// L2308
      ac_int<33, true> v1494 = v1493;	// L2309
      ac_int<33, true> v1495 = v1492;	// L2310
      ac_int<33, true> v1496 = v1494 + v1495;	// L2311
      int32_t v1497 = v1496;	// L2312
      dcred4[0] = v1497;	// L2313
      ac_int<26, false> pw;	// L2314
      pw = 0;	// L2315
      int32_t v1499 = sp4[0];	// L2316
      bool v1500 = v1499 < 215;	// L2317
      if (v1500) {	// L2318
        ac_int<26, false> cand;	// L2319
        cand = 0;	// L2320
        int32_t v1502 = sp4[0];	// L2321
        int v1503 = v1502;	// L2322
        int32_t v1504;
        v1476_req.Push( (ac_int<41, false>)(((0) * 215 + (v1503))) << 1 );
        v1504 = v1476_rsp.Pop();	// L2323
        ac_int<26, false> v1505 = v1504;	// L2324
        ac_int<26, true> v1506 = cand;	// L2325
        ac_int<26, true> v1507;
        ac_int<26, true> _bs_v1507 = v1506;
        _bs_v1507.set_slc(0, ac_int<26, false>(v1505));
        v1507 = _bs_v1507;	// L2326
        cand = v1507;	// L2327
        ac_int<26, true> v1508 = cand;	// L2328
        bool v1509;
        ac_int<26, true> _bs_v1509 = v1508;
        v1509 = _bs_v1509[25];	// L2329
        int32_t v1510 = v1509;	// L2330
        bool v1511 = v1510 == 0;	// L2331
        if (v1511) {	// L2332
          int32_t v1512 = sp4[0];	// L2333
          ac_int<33, true> v1513 = v1512;	// L2334
          ac_int<33, true> v1514 = v1513 + 1;	// L2335
          int32_t v1515 = v1514;	// L2336
          sp4[0] = v1515;	// L2337
        } else {
          int32_t v1516 = dcred4[0];	// L2339
          bool v1517 = v1516 > 0;	// L2340
          if (v1517) {	// L2341
            ac_int<26, true> v1518 = cand;	// L2342
            pw = v1518;	// L2343
            int32_t v1519 = dcred4[0];	// L2344
            ac_int<33, true> v1520 = v1519;	// L2345
            ac_int<33, true> v1521 = v1520 - 1;	// L2346
            int32_t v1522 = v1521;	// L2347
            dcred4[0] = v1522;	// L2348
            int32_t v1523 = sp4[0];	// L2349
            ac_int<33, true> v1524 = v1523;	// L2350
            ac_int<33, true> v1525 = v1524 + 1;	// L2351
            int32_t v1526 = v1525;	// L2352
            sp4[0] = v1526;	// L2353
          }
        }
      }
      ac_int<26, true> v1527 = pw;	// L2357
      v1478.Push(v1527);	// L2358
    }
#ifndef __SYNTHESIS__
    __allo_done++; // csim: this kernel finished its single pass
#endif
    while (1) { wait(); }
  }
};

SC_MODULE(rdrv_e_0) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::Out< ac_int<41, false> > v1528_req;
  Connections::In< int32_t > v1528_rsp;
  Connections::Out< ac_int<34, false> > v1529_req;
  Connections::In< int32_t > v1529_rsp;
  Connections::Out< ac_int<26, true> > v1530;
  Connections::In< int32_t > v1531;
  SC_HAS_PROCESS(rdrv_e_0);
  rdrv_e_0(sc_module_name n) : sc_module(n), v1528_req("v1528_req"), v1528_rsp("v1528_rsp"), v1529_req("v1529_req"), v1529_rsp("v1529_rsp"), v1530("v1530"), v1531("v1531") {
    SC_THREAD(run);
    sensitive << clk.pos();
    async_reset_signal_is(rst, false);
  }
  void run() {
    v1528_req.Reset();
    v1528_rsp.Reset();
    v1529_req.Reset();
    v1529_rsp.Reset();
    v1530.Reset();
    v1531.Reset();
    wait();
    int32_t dcred5[1];	// L2370
    for (int v1533 = 0; v1533 < 1; v1533++) {	// L2371
      dcred5[v1533] = 0;	// L2371
    }
    int32_t sp5[1];	// L2372
    for (int v1535 = 0; v1535 < 1; v1535++) {	// L2373
      sp5[v1535] = 0;	// L2373
    }
    ac_int<26, false> zp1;	// L2374
    zp1 = 0;	// L2375
    int32_t v1537;
    v1529_req.Push( (ac_int<34, false>)(((0) + (0))) << 1 );
    v1537 = v1529_rsp.Pop();	// L2376
    ac_int<33, true> v1538 = v1537;	// L2377
    ac_int<33, true> v1539 = v1538 - 1;	// L2378
    int v1540 = v1539;	// L2379
    for (int v1541 = 0; v1541 < v1540; v1541 += 1) {	// L2380
      ac_int<26, true> v1542 = zp1;	// L2381
      v1530.Push(v1542);	// L2382
    }
    l_S_t_1_t10: for (int t10 = 0; t10 < 215; t10++) {	// L2384
      int32_t v1544 = v1531.Pop();	// L2385
      int32_t v1545 = dcred5[0];	// L2386
      ac_int<33, true> v1546 = v1545;	// L2387
      ac_int<33, true> v1547 = v1544;	// L2388
      ac_int<33, true> v1548 = v1546 + v1547;	// L2389
      int32_t v1549 = v1548;	// L2390
      dcred5[0] = v1549;	// L2391
      ac_int<26, false> pw1;	// L2392
      pw1 = 0;	// L2393
      int32_t v1551 = sp5[0];	// L2394
      bool v1552 = v1551 < 215;	// L2395
      if (v1552) {	// L2396
        ac_int<26, false> cand1;	// L2397
        cand1 = 0;	// L2398
        int32_t v1554 = sp5[0];	// L2399
        int v1555 = v1554;	// L2400
        int32_t v1556;
        v1528_req.Push( (ac_int<41, false>)(((0) * 215 + (v1555))) << 1 );
        v1556 = v1528_rsp.Pop();	// L2401
        ac_int<26, false> v1557 = v1556;	// L2402
        ac_int<26, true> v1558 = cand1;	// L2403
        ac_int<26, true> v1559;
        ac_int<26, true> _bs_v1559 = v1558;
        _bs_v1559.set_slc(0, ac_int<26, false>(v1557));
        v1559 = _bs_v1559;	// L2404
        cand1 = v1559;	// L2405
        ac_int<26, true> v1560 = cand1;	// L2406
        bool v1561;
        ac_int<26, true> _bs_v1561 = v1560;
        v1561 = _bs_v1561[25];	// L2407
        int32_t v1562 = v1561;	// L2408
        bool v1563 = v1562 == 0;	// L2409
        if (v1563) {	// L2410
          int32_t v1564 = sp5[0];	// L2411
          ac_int<33, true> v1565 = v1564;	// L2412
          ac_int<33, true> v1566 = v1565 + 1;	// L2413
          int32_t v1567 = v1566;	// L2414
          sp5[0] = v1567;	// L2415
        } else {
          int32_t v1568 = dcred5[0];	// L2417
          bool v1569 = v1568 > 0;	// L2418
          if (v1569) {	// L2419
            ac_int<26, true> v1570 = cand1;	// L2420
            pw1 = v1570;	// L2421
            int32_t v1571 = dcred5[0];	// L2422
            ac_int<33, true> v1572 = v1571;	// L2423
            ac_int<33, true> v1573 = v1572 - 1;	// L2424
            int32_t v1574 = v1573;	// L2425
            dcred5[0] = v1574;	// L2426
            int32_t v1575 = sp5[0];	// L2427
            ac_int<33, true> v1576 = v1575;	// L2428
            ac_int<33, true> v1577 = v1576 + 1;	// L2429
            int32_t v1578 = v1577;	// L2430
            sp5[0] = v1578;	// L2431
          }
        }
      }
      ac_int<26, true> v1579 = pw1;	// L2435
      v1530.Push(v1579);	// L2436
    }
#ifndef __SYNTHESIS__
    __allo_done++; // csim: this kernel finished its single pass
#endif
    while (1) { wait(); }
  }
};

SC_MODULE(rdrv_n_0) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::Out< ac_int<41, false> > v1580_req;
  Connections::In< int32_t > v1580_rsp;
  Connections::Out< ac_int<34, false> > v1581_req;
  Connections::In< int32_t > v1581_rsp;
  Connections::Out< ac_int<26, true> > v1582;
  Connections::In< int32_t > v1583;
  SC_HAS_PROCESS(rdrv_n_0);
  rdrv_n_0(sc_module_name n) : sc_module(n), v1580_req("v1580_req"), v1580_rsp("v1580_rsp"), v1581_req("v1581_req"), v1581_rsp("v1581_rsp"), v1582("v1582"), v1583("v1583") {
    SC_THREAD(run);
    sensitive << clk.pos();
    async_reset_signal_is(rst, false);
  }
  void run() {
    v1580_req.Reset();
    v1580_rsp.Reset();
    v1581_req.Reset();
    v1581_rsp.Reset();
    v1582.Reset();
    v1583.Reset();
    wait();
    int32_t dcred6[1];	// L2448
    for (int v1585 = 0; v1585 < 1; v1585++) {	// L2449
      dcred6[v1585] = 0;	// L2449
    }
    int32_t sp6[1];	// L2450
    for (int v1587 = 0; v1587 < 1; v1587++) {	// L2451
      sp6[v1587] = 0;	// L2451
    }
    ac_int<26, false> zp2;	// L2452
    zp2 = 0;	// L2453
    int32_t v1589;
    v1581_req.Push( (ac_int<34, false>)(((0) + (0))) << 1 );
    v1589 = v1581_rsp.Pop();	// L2454
    ac_int<33, true> v1590 = v1589;	// L2455
    ac_int<33, true> v1591 = v1590 - 1;	// L2456
    int v1592 = v1591;	// L2457
    for (int v1593 = 0; v1593 < v1592; v1593 += 1) {	// L2458
      ac_int<26, true> v1594 = zp2;	// L2459
      v1582.Push(v1594);	// L2460
    }
    l_S_t_1_t11: for (int t11 = 0; t11 < 215; t11++) {	// L2462
      int32_t v1596 = v1583.Pop();	// L2463
      int32_t v1597 = dcred6[0];	// L2464
      ac_int<33, true> v1598 = v1597;	// L2465
      ac_int<33, true> v1599 = v1596;	// L2466
      ac_int<33, true> v1600 = v1598 + v1599;	// L2467
      int32_t v1601 = v1600;	// L2468
      dcred6[0] = v1601;	// L2469
      ac_int<26, false> pw2;	// L2470
      pw2 = 0;	// L2471
      int32_t v1603 = sp6[0];	// L2472
      bool v1604 = v1603 < 215;	// L2473
      if (v1604) {	// L2474
        ac_int<26, false> cand2;	// L2475
        cand2 = 0;	// L2476
        int32_t v1606 = sp6[0];	// L2477
        int v1607 = v1606;	// L2478
        int32_t v1608;
        v1580_req.Push( (ac_int<41, false>)(((0) * 215 + (v1607))) << 1 );
        v1608 = v1580_rsp.Pop();	// L2479
        ac_int<26, false> v1609 = v1608;	// L2480
        ac_int<26, true> v1610 = cand2;	// L2481
        ac_int<26, true> v1611;
        ac_int<26, true> _bs_v1611 = v1610;
        _bs_v1611.set_slc(0, ac_int<26, false>(v1609));
        v1611 = _bs_v1611;	// L2482
        cand2 = v1611;	// L2483
        ac_int<26, true> v1612 = cand2;	// L2484
        bool v1613;
        ac_int<26, true> _bs_v1613 = v1612;
        v1613 = _bs_v1613[25];	// L2485
        int32_t v1614 = v1613;	// L2486
        bool v1615 = v1614 == 0;	// L2487
        if (v1615) {	// L2488
          int32_t v1616 = sp6[0];	// L2489
          ac_int<33, true> v1617 = v1616;	// L2490
          ac_int<33, true> v1618 = v1617 + 1;	// L2491
          int32_t v1619 = v1618;	// L2492
          sp6[0] = v1619;	// L2493
        } else {
          int32_t v1620 = dcred6[0];	// L2495
          bool v1621 = v1620 > 0;	// L2496
          if (v1621) {	// L2497
            ac_int<26, true> v1622 = cand2;	// L2498
            pw2 = v1622;	// L2499
            int32_t v1623 = dcred6[0];	// L2500
            ac_int<33, true> v1624 = v1623;	// L2501
            ac_int<33, true> v1625 = v1624 - 1;	// L2502
            int32_t v1626 = v1625;	// L2503
            dcred6[0] = v1626;	// L2504
            int32_t v1627 = sp6[0];	// L2505
            ac_int<33, true> v1628 = v1627;	// L2506
            ac_int<33, true> v1629 = v1628 + 1;	// L2507
            int32_t v1630 = v1629;	// L2508
            sp6[0] = v1630;	// L2509
          }
        }
      }
      ac_int<26, true> v1631 = pw2;	// L2513
      v1582.Push(v1631);	// L2514
    }
#ifndef __SYNTHESIS__
    __allo_done++; // csim: this kernel finished its single pass
#endif
    while (1) { wait(); }
  }
};

SC_MODULE(rdrv_s_0) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::Out< ac_int<41, false> > v1632_req;
  Connections::In< int32_t > v1632_rsp;
  Connections::Out< ac_int<34, false> > v1633_req;
  Connections::In< int32_t > v1633_rsp;
  Connections::Out< ac_int<26, true> > v1634;
  Connections::In< int32_t > v1635;
  SC_HAS_PROCESS(rdrv_s_0);
  rdrv_s_0(sc_module_name n) : sc_module(n), v1632_req("v1632_req"), v1632_rsp("v1632_rsp"), v1633_req("v1633_req"), v1633_rsp("v1633_rsp"), v1634("v1634"), v1635("v1635") {
    SC_THREAD(run);
    sensitive << clk.pos();
    async_reset_signal_is(rst, false);
  }
  void run() {
    v1632_req.Reset();
    v1632_rsp.Reset();
    v1633_req.Reset();
    v1633_rsp.Reset();
    v1634.Reset();
    v1635.Reset();
    wait();
    int32_t dcred7[1];	// L2526
    for (int v1637 = 0; v1637 < 1; v1637++) {	// L2527
      dcred7[v1637] = 0;	// L2527
    }
    int32_t sp7[1];	// L2528
    for (int v1639 = 0; v1639 < 1; v1639++) {	// L2529
      sp7[v1639] = 0;	// L2529
    }
    ac_int<26, false> zp3;	// L2530
    zp3 = 0;	// L2531
    int32_t v1641;
    v1633_req.Push( (ac_int<34, false>)(((0) + (0))) << 1 );
    v1641 = v1633_rsp.Pop();	// L2532
    ac_int<33, true> v1642 = v1641;	// L2533
    ac_int<33, true> v1643 = v1642 - 1;	// L2534
    int v1644 = v1643;	// L2535
    for (int v1645 = 0; v1645 < v1644; v1645 += 1) {	// L2536
      ac_int<26, true> v1646 = zp3;	// L2537
      v1634.Push(v1646);	// L2538
    }
    l_S_t_1_t12: for (int t12 = 0; t12 < 215; t12++) {	// L2540
      int32_t v1648 = v1635.Pop();	// L2541
      int32_t v1649 = dcred7[0];	// L2542
      ac_int<33, true> v1650 = v1649;	// L2543
      ac_int<33, true> v1651 = v1648;	// L2544
      ac_int<33, true> v1652 = v1650 + v1651;	// L2545
      int32_t v1653 = v1652;	// L2546
      dcred7[0] = v1653;	// L2547
      ac_int<26, false> pw3;	// L2548
      pw3 = 0;	// L2549
      int32_t v1655 = sp7[0];	// L2550
      bool v1656 = v1655 < 215;	// L2551
      if (v1656) {	// L2552
        ac_int<26, false> cand3;	// L2553
        cand3 = 0;	// L2554
        int32_t v1658 = sp7[0];	// L2555
        int v1659 = v1658;	// L2556
        int32_t v1660;
        v1632_req.Push( (ac_int<41, false>)(((0) * 215 + (v1659))) << 1 );
        v1660 = v1632_rsp.Pop();	// L2557
        ac_int<26, false> v1661 = v1660;	// L2558
        ac_int<26, true> v1662 = cand3;	// L2559
        ac_int<26, true> v1663;
        ac_int<26, true> _bs_v1663 = v1662;
        _bs_v1663.set_slc(0, ac_int<26, false>(v1661));
        v1663 = _bs_v1663;	// L2560
        cand3 = v1663;	// L2561
        ac_int<26, true> v1664 = cand3;	// L2562
        bool v1665;
        ac_int<26, true> _bs_v1665 = v1664;
        v1665 = _bs_v1665[25];	// L2563
        int32_t v1666 = v1665;	// L2564
        bool v1667 = v1666 == 0;	// L2565
        if (v1667) {	// L2566
          int32_t v1668 = sp7[0];	// L2567
          ac_int<33, true> v1669 = v1668;	// L2568
          ac_int<33, true> v1670 = v1669 + 1;	// L2569
          int32_t v1671 = v1670;	// L2570
          sp7[0] = v1671;	// L2571
        } else {
          int32_t v1672 = dcred7[0];	// L2573
          bool v1673 = v1672 > 0;	// L2574
          if (v1673) {	// L2575
            ac_int<26, true> v1674 = cand3;	// L2576
            pw3 = v1674;	// L2577
            int32_t v1675 = dcred7[0];	// L2578
            ac_int<33, true> v1676 = v1675;	// L2579
            ac_int<33, true> v1677 = v1676 - 1;	// L2580
            int32_t v1678 = v1677;	// L2581
            dcred7[0] = v1678;	// L2582
            int32_t v1679 = sp7[0];	// L2583
            ac_int<33, true> v1680 = v1679;	// L2584
            ac_int<33, true> v1681 = v1680 + 1;	// L2585
            int32_t v1682 = v1681;	// L2586
            sp7[0] = v1682;	// L2587
          }
        }
      }
      ac_int<26, true> v1683 = pw3;	// L2591
      v1634.Push(v1683);	// L2592
    }
#ifndef __SYNTHESIS__
    __allo_done++; // csim: this kernel finished its single pass
#endif
    while (1) { wait(); }
  }
};

SC_MODULE(rclc_w_0) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::Out< ac_int<41, false> > v1684_req;
  Connections::Out< ac_int<34, false> > v1685_req;
  Connections::In< int32_t > v1685_rsp;
  Connections::Out< int32_t > v1686;
  Connections::In< ac_int<26, true> > v1687;
  SC_HAS_PROCESS(rclc_w_0);
  rclc_w_0(sc_module_name n) : sc_module(n), v1684_req("v1684_req"), v1685_req("v1685_req"), v1685_rsp("v1685_rsp"), v1686("v1686"), v1687("v1687") {
    SC_THREAD(run);
    sensitive << clk.pos();
    async_reset_signal_is(rst, false);
  }
  void run() {
    v1684_req.Reset();
    v1685_req.Reset();
    v1685_rsp.Reset();
    v1686.Reset();
    v1687.Reset();
    wait();
    int32_t k6[1];	// L2606
    for (int v1689 = 0; v1689 < 1; v1689++) {	// L2607
      k6[v1689] = 0;	// L2607
    }
    int32_t cret4[1];	// L2608
    for (int v1691 = 0; v1691 < 1; v1691++) {	// L2609
      cret4[v1691] = 0;	// L2609
    }
    int32_t zc4;	// L2610
    zc4 = 0;	// L2611
    int32_t v1693;
    v1685_req.Push( (ac_int<34, false>)(((0) + (0))) << 1 );
    v1693 = v1685_rsp.Pop();	// L2612
    ac_int<33, true> v1694 = v1693;	// L2613
    ac_int<33, true> v1695 = v1694 - 1;	// L2614
    int v1696 = v1695;	// L2615
    for (int v1697 = 0; v1697 < v1696; v1697 += 1) {	// L2616
      int32_t v1698 = zc4;	// L2617
      v1686.Push(v1698);	// L2618
    }
    cret4[0] = 2;	// L2620
    int32_t v1699 = cret4[0];	// L2621
    v1686.Push(v1699);	// L2622
    l_S_t_1_t13: for (int t13 = 0; t13 < 215; t13++) {	// L2623
      ac_int<26, false> v1701 = v1687.Pop();	// L2624
      ac_int<26, false> pw4;	// L2625
      pw4 = v1701;	// L2626
      cret4[0] = 0;	// L2627
      ac_int<26, true> v1703 = pw4;	// L2628
      bool v1704;
      ac_int<26, true> _bs_v1704 = v1703;
      v1704 = _bs_v1704[25];	// L2629
      int32_t v1705 = v1704;	// L2630
      bool v1706 = v1705 == 1;	// L2631
      if (v1706) {	// L2632
        cret4[0] = 1;	// L2633
        int32_t v1707 = k6[0];	// L2634
        bool v1708 = v1707 < 215;	// L2635
        if (v1708) {	// L2636
          ac_int<26, true> v1709 = pw4;	// L2637
          int32_t v1710 = v1709;	// L2638
          int32_t v1711 = v1710 & 67108863;	// L2639
          int32_t v1712 = k6[0];	// L2640
          int v1713 = v1712;	// L2641
          v1684_req.Push( ((ac_int<41, false>)(v1711) << 9) | ((ac_int<41, false>)(((0) * 215 + (v1713))) << 1) | (ac_int<41, false>)1 );	// L2642
          int32_t v1714 = k6[0];	// L2643
          ac_int<33, true> v1715 = v1714;	// L2644
          ac_int<33, true> v1716 = v1715 + 1;	// L2645
          int32_t v1717 = v1716;	// L2646
          k6[0] = v1717;	// L2647
        }
      }
      int32_t v1718 = cret4[0];	// L2650
      v1686.Push(v1718);	// L2651
    }
#ifndef __SYNTHESIS__
    __allo_done++; // csim: this kernel finished its single pass
#endif
    while (1) { wait(); }
  }
};

SC_MODULE(rclc_e_0) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::Out< ac_int<41, false> > v1719_req;
  Connections::Out< ac_int<34, false> > v1720_req;
  Connections::In< int32_t > v1720_rsp;
  Connections::Out< int32_t > v1721;
  Connections::In< ac_int<26, true> > v1722;
  SC_HAS_PROCESS(rclc_e_0);
  rclc_e_0(sc_module_name n) : sc_module(n), v1719_req("v1719_req"), v1720_req("v1720_req"), v1720_rsp("v1720_rsp"), v1721("v1721"), v1722("v1722") {
    SC_THREAD(run);
    sensitive << clk.pos();
    async_reset_signal_is(rst, false);
  }
  void run() {
    v1719_req.Reset();
    v1720_req.Reset();
    v1720_rsp.Reset();
    v1721.Reset();
    v1722.Reset();
    wait();
    int32_t k7[1];	// L2665
    for (int v1724 = 0; v1724 < 1; v1724++) {	// L2666
      k7[v1724] = 0;	// L2666
    }
    int32_t cret5[1];	// L2667
    for (int v1726 = 0; v1726 < 1; v1726++) {	// L2668
      cret5[v1726] = 0;	// L2668
    }
    int32_t zc5;	// L2669
    zc5 = 0;	// L2670
    int32_t v1728;
    v1720_req.Push( (ac_int<34, false>)(((0) + (0))) << 1 );
    v1728 = v1720_rsp.Pop();	// L2671
    ac_int<33, true> v1729 = v1728;	// L2672
    ac_int<33, true> v1730 = v1729 - 1;	// L2673
    int v1731 = v1730;	// L2674
    for (int v1732 = 0; v1732 < v1731; v1732 += 1) {	// L2675
      int32_t v1733 = zc5;	// L2676
      v1721.Push(v1733);	// L2677
    }
    cret5[0] = 2;	// L2679
    int32_t v1734 = cret5[0];	// L2680
    v1721.Push(v1734);	// L2681
    l_S_t_1_t14: for (int t14 = 0; t14 < 215; t14++) {	// L2682
      ac_int<26, false> v1736 = v1722.Pop();	// L2683
      ac_int<26, false> pw5;	// L2684
      pw5 = v1736;	// L2685
      cret5[0] = 0;	// L2686
      ac_int<26, true> v1738 = pw5;	// L2687
      bool v1739;
      ac_int<26, true> _bs_v1739 = v1738;
      v1739 = _bs_v1739[25];	// L2688
      int32_t v1740 = v1739;	// L2689
      bool v1741 = v1740 == 1;	// L2690
      if (v1741) {	// L2691
        cret5[0] = 1;	// L2692
        int32_t v1742 = k7[0];	// L2693
        bool v1743 = v1742 < 215;	// L2694
        if (v1743) {	// L2695
          ac_int<26, true> v1744 = pw5;	// L2696
          int32_t v1745 = v1744;	// L2697
          int32_t v1746 = v1745 & 67108863;	// L2698
          int32_t v1747 = k7[0];	// L2699
          int v1748 = v1747;	// L2700
          v1719_req.Push( ((ac_int<41, false>)(v1746) << 9) | ((ac_int<41, false>)(((0) * 215 + (v1748))) << 1) | (ac_int<41, false>)1 );	// L2701
          int32_t v1749 = k7[0];	// L2702
          ac_int<33, true> v1750 = v1749;	// L2703
          ac_int<33, true> v1751 = v1750 + 1;	// L2704
          int32_t v1752 = v1751;	// L2705
          k7[0] = v1752;	// L2706
        }
      }
      int32_t v1753 = cret5[0];	// L2709
      v1721.Push(v1753);	// L2710
    }
#ifndef __SYNTHESIS__
    __allo_done++; // csim: this kernel finished its single pass
#endif
    while (1) { wait(); }
  }
};

SC_MODULE(rclc_n_0) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::Out< ac_int<41, false> > v1754_req;
  Connections::Out< ac_int<34, false> > v1755_req;
  Connections::In< int32_t > v1755_rsp;
  Connections::Out< int32_t > v1756;
  Connections::In< ac_int<26, true> > v1757;
  SC_HAS_PROCESS(rclc_n_0);
  rclc_n_0(sc_module_name n) : sc_module(n), v1754_req("v1754_req"), v1755_req("v1755_req"), v1755_rsp("v1755_rsp"), v1756("v1756"), v1757("v1757") {
    SC_THREAD(run);
    sensitive << clk.pos();
    async_reset_signal_is(rst, false);
  }
  void run() {
    v1754_req.Reset();
    v1755_req.Reset();
    v1755_rsp.Reset();
    v1756.Reset();
    v1757.Reset();
    wait();
    int32_t k8[1];	// L2724
    for (int v1759 = 0; v1759 < 1; v1759++) {	// L2725
      k8[v1759] = 0;	// L2725
    }
    int32_t cret6[1];	// L2726
    for (int v1761 = 0; v1761 < 1; v1761++) {	// L2727
      cret6[v1761] = 0;	// L2727
    }
    int32_t zc6;	// L2728
    zc6 = 0;	// L2729
    int32_t v1763;
    v1755_req.Push( (ac_int<34, false>)(((0) + (0))) << 1 );
    v1763 = v1755_rsp.Pop();	// L2730
    ac_int<33, true> v1764 = v1763;	// L2731
    ac_int<33, true> v1765 = v1764 - 1;	// L2732
    int v1766 = v1765;	// L2733
    for (int v1767 = 0; v1767 < v1766; v1767 += 1) {	// L2734
      int32_t v1768 = zc6;	// L2735
      v1756.Push(v1768);	// L2736
    }
    cret6[0] = 2;	// L2738
    int32_t v1769 = cret6[0];	// L2739
    v1756.Push(v1769);	// L2740
    l_S_t_1_t15: for (int t15 = 0; t15 < 215; t15++) {	// L2741
      ac_int<26, false> v1771 = v1757.Pop();	// L2742
      ac_int<26, false> pw6;	// L2743
      pw6 = v1771;	// L2744
      cret6[0] = 0;	// L2745
      ac_int<26, true> v1773 = pw6;	// L2746
      bool v1774;
      ac_int<26, true> _bs_v1774 = v1773;
      v1774 = _bs_v1774[25];	// L2747
      int32_t v1775 = v1774;	// L2748
      bool v1776 = v1775 == 1;	// L2749
      if (v1776) {	// L2750
        cret6[0] = 1;	// L2751
        int32_t v1777 = k8[0];	// L2752
        bool v1778 = v1777 < 215;	// L2753
        if (v1778) {	// L2754
          ac_int<26, true> v1779 = pw6;	// L2755
          int32_t v1780 = v1779;	// L2756
          int32_t v1781 = v1780 & 67108863;	// L2757
          int32_t v1782 = k8[0];	// L2758
          int v1783 = v1782;	// L2759
          v1754_req.Push( ((ac_int<41, false>)(v1781) << 9) | ((ac_int<41, false>)(((0) * 215 + (v1783))) << 1) | (ac_int<41, false>)1 );	// L2760
          int32_t v1784 = k8[0];	// L2761
          ac_int<33, true> v1785 = v1784;	// L2762
          ac_int<33, true> v1786 = v1785 + 1;	// L2763
          int32_t v1787 = v1786;	// L2764
          k8[0] = v1787;	// L2765
        }
      }
      int32_t v1788 = cret6[0];	// L2768
      v1756.Push(v1788);	// L2769
    }
#ifndef __SYNTHESIS__
    __allo_done++; // csim: this kernel finished its single pass
#endif
    while (1) { wait(); }
  }
};

SC_MODULE(rclc_s_0) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::Out< ac_int<41, false> > v1789_req;
  Connections::Out< ac_int<34, false> > v1790_req;
  Connections::In< int32_t > v1790_rsp;
  Connections::Out< int32_t > v1791;
  Connections::In< ac_int<26, true> > v1792;
  SC_HAS_PROCESS(rclc_s_0);
  rclc_s_0(sc_module_name n) : sc_module(n), v1789_req("v1789_req"), v1790_req("v1790_req"), v1790_rsp("v1790_rsp"), v1791("v1791"), v1792("v1792") {
    SC_THREAD(run);
    sensitive << clk.pos();
    async_reset_signal_is(rst, false);
  }
  void run() {
    v1789_req.Reset();
    v1790_req.Reset();
    v1790_rsp.Reset();
    v1791.Reset();
    v1792.Reset();
    wait();
    int32_t k9[1];	// L2783
    for (int v1794 = 0; v1794 < 1; v1794++) {	// L2784
      k9[v1794] = 0;	// L2784
    }
    int32_t cret7[1];	// L2785
    for (int v1796 = 0; v1796 < 1; v1796++) {	// L2786
      cret7[v1796] = 0;	// L2786
    }
    int32_t zc7;	// L2787
    zc7 = 0;	// L2788
    int32_t v1798;
    v1790_req.Push( (ac_int<34, false>)(((0) + (0))) << 1 );
    v1798 = v1790_rsp.Pop();	// L2789
    ac_int<33, true> v1799 = v1798;	// L2790
    ac_int<33, true> v1800 = v1799 - 1;	// L2791
    int v1801 = v1800;	// L2792
    for (int v1802 = 0; v1802 < v1801; v1802 += 1) {	// L2793
      int32_t v1803 = zc7;	// L2794
      v1791.Push(v1803);	// L2795
    }
    cret7[0] = 2;	// L2797
    int32_t v1804 = cret7[0];	// L2798
    v1791.Push(v1804);	// L2799
    l_S_t_1_t16: for (int t16 = 0; t16 < 215; t16++) {	// L2800
      ac_int<26, false> v1806 = v1792.Pop();	// L2801
      ac_int<26, false> pw7;	// L2802
      pw7 = v1806;	// L2803
      cret7[0] = 0;	// L2804
      ac_int<26, true> v1808 = pw7;	// L2805
      bool v1809;
      ac_int<26, true> _bs_v1809 = v1808;
      v1809 = _bs_v1809[25];	// L2806
      int32_t v1810 = v1809;	// L2807
      bool v1811 = v1810 == 1;	// L2808
      if (v1811) {	// L2809
        cret7[0] = 1;	// L2810
        int32_t v1812 = k9[0];	// L2811
        bool v1813 = v1812 < 215;	// L2812
        if (v1813) {	// L2813
          ac_int<26, true> v1814 = pw7;	// L2814
          int32_t v1815 = v1814;	// L2815
          int32_t v1816 = v1815 & 67108863;	// L2816
          int32_t v1817 = k9[0];	// L2817
          int v1818 = v1817;	// L2818
          v1789_req.Push( ((ac_int<41, false>)(v1816) << 9) | ((ac_int<41, false>)(((0) * 215 + (v1818))) << 1) | (ac_int<41, false>)1 );	// L2819
          int32_t v1819 = k9[0];	// L2820
          ac_int<33, true> v1820 = v1819;	// L2821
          ac_int<33, true> v1821 = v1820 + 1;	// L2822
          int32_t v1822 = v1821;	// L2823
          k9[0] = v1822;	// L2824
        }
      }
      int32_t v1823 = cret7[0];	// L2827
      v1791.Push(v1823);	// L2828
    }
#ifndef __SYNTHESIS__
    __allo_done++; // csim: this kernel finished its single pass
#endif
    while (1) { wait(); }
  }
};

SC_MODULE(top) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::Combinational< ac_int<17, true> > v1845_in;
  Connections::Combinational< ac_int<17, true> > v1845_out;
  AlloFifo< ac_int<17, true>, 8 > v1845_fifo;
  Connections::Combinational< ac_int<17, true> > v1846_in;
  Connections::Combinational< ac_int<17, true> > v1846_out;
  AlloFifo< ac_int<17, true>, 8 > v1846_fifo;
  Connections::Combinational< ac_int<17, true> > v1847_in;
  Connections::Combinational< ac_int<17, true> > v1847_out;
  AlloFifo< ac_int<17, true>, 8 > v1847_fifo;
  Connections::Combinational< ac_int<17, true> > v1848_in;
  Connections::Combinational< ac_int<17, true> > v1848_out;
  AlloFifo< ac_int<17, true>, 8 > v1848_fifo;
  Connections::Combinational< ac_int<17, true> > v1849_in;
  Connections::Combinational< ac_int<17, true> > v1849_out;
  AlloFifo< ac_int<17, true>, 8 > v1849_fifo;
  Connections::Combinational< ac_int<17, true> > v1850_in;
  Connections::Combinational< ac_int<17, true> > v1850_out;
  AlloFifo< ac_int<17, true>, 8 > v1850_fifo;
  Connections::Combinational< ac_int<17, true> > v1851_in;
  Connections::Combinational< ac_int<17, true> > v1851_out;
  AlloFifo< ac_int<17, true>, 8 > v1851_fifo;
  Connections::Combinational< ac_int<17, true> > v1852_in;
  Connections::Combinational< ac_int<17, true> > v1852_out;
  AlloFifo< ac_int<17, true>, 8 > v1852_fifo;
  Connections::Combinational< ac_int<26, true> > v1853_in;
  Connections::Combinational< ac_int<26, true> > v1853_out;
  AlloFifo< ac_int<26, true>, 8 > v1853_fifo;
  Connections::Combinational< ac_int<26, true> > v1854_in;
  Connections::Combinational< ac_int<26, true> > v1854_out;
  AlloFifo< ac_int<26, true>, 8 > v1854_fifo;
  Connections::Combinational< ac_int<26, true> > v1855_in;
  Connections::Combinational< ac_int<26, true> > v1855_out;
  AlloFifo< ac_int<26, true>, 8 > v1855_fifo;
  Connections::Combinational< ac_int<26, true> > v1856_in;
  Connections::Combinational< ac_int<26, true> > v1856_out;
  AlloFifo< ac_int<26, true>, 8 > v1856_fifo;
  Connections::Combinational< ac_int<26, true> > v1857_in;
  Connections::Combinational< ac_int<26, true> > v1857_out;
  AlloFifo< ac_int<26, true>, 8 > v1857_fifo;
  Connections::Combinational< ac_int<26, true> > v1858_in;
  Connections::Combinational< ac_int<26, true> > v1858_out;
  AlloFifo< ac_int<26, true>, 8 > v1858_fifo;
  Connections::Combinational< ac_int<26, true> > v1859_in;
  Connections::Combinational< ac_int<26, true> > v1859_out;
  AlloFifo< ac_int<26, true>, 8 > v1859_fifo;
  Connections::Combinational< ac_int<26, true> > v1860_in;
  Connections::Combinational< ac_int<26, true> > v1860_out;
  AlloFifo< ac_int<26, true>, 8 > v1860_fifo;
  Connections::Combinational< int32_t > v1861_in;
  Connections::Combinational< int32_t > v1861_out;
  AlloFifo< int32_t, 8 > v1861_fifo;
  Connections::Combinational< int32_t > v1862_in;
  Connections::Combinational< int32_t > v1862_out;
  AlloFifo< int32_t, 8 > v1862_fifo;
  Connections::Combinational< int32_t > v1863_in;
  Connections::Combinational< int32_t > v1863_out;
  AlloFifo< int32_t, 8 > v1863_fifo;
  Connections::Combinational< int32_t > v1864_in;
  Connections::Combinational< int32_t > v1864_out;
  AlloFifo< int32_t, 8 > v1864_fifo;
  Connections::Combinational< int32_t > v1865_in;
  Connections::Combinational< int32_t > v1865_out;
  AlloFifo< int32_t, 8 > v1865_fifo;
  Connections::Combinational< int32_t > v1866_in;
  Connections::Combinational< int32_t > v1866_out;
  AlloFifo< int32_t, 8 > v1866_fifo;
  Connections::Combinational< int32_t > v1867_in;
  Connections::Combinational< int32_t > v1867_out;
  AlloFifo< int32_t, 8 > v1867_fifo;
  Connections::Combinational< int32_t > v1868_in;
  Connections::Combinational< int32_t > v1868_out;
  AlloFifo< int32_t, 8 > v1868_fifo;
  Connections::Combinational< int32_t > v1869_in;
  Connections::Combinational< int32_t > v1869_out;
  AlloFifo< int32_t, 8 > v1869_fifo;
  Connections::Combinational< int32_t > v1870_in;
  Connections::Combinational< int32_t > v1870_out;
  AlloFifo< int32_t, 8 > v1870_fifo;
  Connections::Combinational< int32_t > v1871_in;
  Connections::Combinational< int32_t > v1871_out;
  AlloFifo< int32_t, 8 > v1871_fifo;
  Connections::Combinational< int32_t > v1872_in;
  Connections::Combinational< int32_t > v1872_out;
  AlloFifo< int32_t, 8 > v1872_fifo;
  Connections::Combinational< int32_t > v1873_in;
  Connections::Combinational< int32_t > v1873_out;
  AlloFifo< int32_t, 8 > v1873_fifo;
  Connections::Combinational< int32_t > v1874_in;
  Connections::Combinational< int32_t > v1874_out;
  AlloFifo< int32_t, 8 > v1874_fifo;
  Connections::Combinational< int32_t > v1875_in;
  Connections::Combinational< int32_t > v1875_out;
  AlloFifo< int32_t, 8 > v1875_fifo;
  Connections::Combinational< int32_t > v1876_in;
  Connections::Combinational< int32_t > v1876_out;
  AlloFifo< int32_t, 8 > v1876_fifo;
  node_0_0 u0;
  drv_w_0 u1;
  drv_e_0 u2;
  drv_n_0 u3;
  drv_s_0 u4;
  col_w_0 u5;
  col_e_0 u6;
  col_n_0 u7;
  col_s_0 u8;
  rdrv_w_0 u9;
  rdrv_e_0 u10;
  rdrv_n_0 u11;
  rdrv_s_0 u12;
  rclc_w_0 u13;
  rclc_e_0 u14;
  rclc_n_0 u15;
  rclc_s_0 u16;
  Connections::Combinational< ac_int<34, false> > mp0_0_req_ch;
  Connections::Combinational< int32_t > mp0_0_rsp_ch;
  AlloMem< int32_t, 1, 1, 32 > mp0_0_mem;
  Connections::Combinational< ac_int<25, false> > mp1_0_req_ch;
  Connections::Combinational< half > mp1_0_rsp_ch;
  AlloMem< half, 215, 8, 16 > mp1_0_mem;
  Connections::Combinational< ac_int<41, false> > mp1_1_req_ch;
  Connections::Combinational< int32_t > mp1_1_rsp_ch;
  AlloMem< int32_t, 215, 8, 32 > mp1_1_mem;
  Connections::Combinational< ac_int<34, false> > mp1_2_req_ch;
  Connections::Combinational< int32_t > mp1_2_rsp_ch;
  AlloMem< int32_t, 1, 1, 32 > mp1_2_mem;
  Connections::Combinational< ac_int<25, false> > mp2_0_req_ch;
  Connections::Combinational< half > mp2_0_rsp_ch;
  AlloMem< half, 215, 8, 16 > mp2_0_mem;
  Connections::Combinational< ac_int<41, false> > mp2_1_req_ch;
  Connections::Combinational< int32_t > mp2_1_rsp_ch;
  AlloMem< int32_t, 215, 8, 32 > mp2_1_mem;
  Connections::Combinational< ac_int<34, false> > mp2_2_req_ch;
  Connections::Combinational< int32_t > mp2_2_rsp_ch;
  AlloMem< int32_t, 1, 1, 32 > mp2_2_mem;
  Connections::Combinational< ac_int<25, false> > mp3_0_req_ch;
  Connections::Combinational< half > mp3_0_rsp_ch;
  AlloMem< half, 215, 8, 16 > mp3_0_mem;
  Connections::Combinational< ac_int<41, false> > mp3_1_req_ch;
  Connections::Combinational< int32_t > mp3_1_rsp_ch;
  AlloMem< int32_t, 215, 8, 32 > mp3_1_mem;
  Connections::Combinational< ac_int<34, false> > mp3_2_req_ch;
  Connections::Combinational< int32_t > mp3_2_rsp_ch;
  AlloMem< int32_t, 1, 1, 32 > mp3_2_mem;
  Connections::Combinational< ac_int<25, false> > mp4_0_req_ch;
  Connections::Combinational< half > mp4_0_rsp_ch;
  AlloMem< half, 215, 8, 16 > mp4_0_mem;
  Connections::Combinational< ac_int<41, false> > mp4_1_req_ch;
  Connections::Combinational< int32_t > mp4_1_rsp_ch;
  AlloMem< int32_t, 215, 8, 32 > mp4_1_mem;
  Connections::Combinational< ac_int<34, false> > mp4_2_req_ch;
  Connections::Combinational< int32_t > mp4_2_rsp_ch;
  AlloMem< int32_t, 1, 1, 32 > mp4_2_mem;
  Connections::Combinational< ac_int<25, false> > mp5_0_req_ch;
  AlloMemW< half, 215, 8, 16 > mp5_0_mem;
  Connections::Combinational< ac_int<34, false> > mp5_1_req_ch;
  Connections::Combinational< int32_t > mp5_1_rsp_ch;
  AlloMem< int32_t, 1, 1, 32 > mp5_1_mem;
  Connections::Combinational< ac_int<25, false> > mp6_0_req_ch;
  AlloMemW< half, 215, 8, 16 > mp6_0_mem;
  Connections::Combinational< ac_int<34, false> > mp6_1_req_ch;
  Connections::Combinational< int32_t > mp6_1_rsp_ch;
  AlloMem< int32_t, 1, 1, 32 > mp6_1_mem;
  Connections::Combinational< ac_int<25, false> > mp7_0_req_ch;
  AlloMemW< half, 215, 8, 16 > mp7_0_mem;
  Connections::Combinational< ac_int<34, false> > mp7_1_req_ch;
  Connections::Combinational< int32_t > mp7_1_rsp_ch;
  AlloMem< int32_t, 1, 1, 32 > mp7_1_mem;
  Connections::Combinational< ac_int<25, false> > mp8_0_req_ch;
  AlloMemW< half, 215, 8, 16 > mp8_0_mem;
  Connections::Combinational< ac_int<34, false> > mp8_1_req_ch;
  Connections::Combinational< int32_t > mp8_1_rsp_ch;
  AlloMem< int32_t, 1, 1, 32 > mp8_1_mem;
  Connections::Combinational< ac_int<41, false> > mp9_0_req_ch;
  Connections::Combinational< int32_t > mp9_0_rsp_ch;
  AlloMem< int32_t, 215, 8, 32 > mp9_0_mem;
  Connections::Combinational< ac_int<34, false> > mp9_1_req_ch;
  Connections::Combinational< int32_t > mp9_1_rsp_ch;
  AlloMem< int32_t, 1, 1, 32 > mp9_1_mem;
  Connections::Combinational< ac_int<41, false> > mp10_0_req_ch;
  Connections::Combinational< int32_t > mp10_0_rsp_ch;
  AlloMem< int32_t, 215, 8, 32 > mp10_0_mem;
  Connections::Combinational< ac_int<34, false> > mp10_1_req_ch;
  Connections::Combinational< int32_t > mp10_1_rsp_ch;
  AlloMem< int32_t, 1, 1, 32 > mp10_1_mem;
  Connections::Combinational< ac_int<41, false> > mp11_0_req_ch;
  Connections::Combinational< int32_t > mp11_0_rsp_ch;
  AlloMem< int32_t, 215, 8, 32 > mp11_0_mem;
  Connections::Combinational< ac_int<34, false> > mp11_1_req_ch;
  Connections::Combinational< int32_t > mp11_1_rsp_ch;
  AlloMem< int32_t, 1, 1, 32 > mp11_1_mem;
  Connections::Combinational< ac_int<41, false> > mp12_0_req_ch;
  Connections::Combinational< int32_t > mp12_0_rsp_ch;
  AlloMem< int32_t, 215, 8, 32 > mp12_0_mem;
  Connections::Combinational< ac_int<34, false> > mp12_1_req_ch;
  Connections::Combinational< int32_t > mp12_1_rsp_ch;
  AlloMem< int32_t, 1, 1, 32 > mp12_1_mem;
  Connections::Combinational< ac_int<41, false> > mp13_0_req_ch;
  AlloMemW< int32_t, 215, 8, 32 > mp13_0_mem;
  Connections::Combinational< ac_int<34, false> > mp13_1_req_ch;
  Connections::Combinational< int32_t > mp13_1_rsp_ch;
  AlloMem< int32_t, 1, 1, 32 > mp13_1_mem;
  Connections::Combinational< ac_int<41, false> > mp14_0_req_ch;
  AlloMemW< int32_t, 215, 8, 32 > mp14_0_mem;
  Connections::Combinational< ac_int<34, false> > mp14_1_req_ch;
  Connections::Combinational< int32_t > mp14_1_rsp_ch;
  AlloMem< int32_t, 1, 1, 32 > mp14_1_mem;
  Connections::Combinational< ac_int<41, false> > mp15_0_req_ch;
  AlloMemW< int32_t, 215, 8, 32 > mp15_0_mem;
  Connections::Combinational< ac_int<34, false> > mp15_1_req_ch;
  Connections::Combinational< int32_t > mp15_1_rsp_ch;
  AlloMem< int32_t, 1, 1, 32 > mp15_1_mem;
  Connections::Combinational< ac_int<41, false> > mp16_0_req_ch;
  AlloMemW< int32_t, 215, 8, 32 > mp16_0_mem;
  Connections::Combinational< ac_int<34, false> > mp16_1_req_ch;
  Connections::Combinational< int32_t > mp16_1_rsp_ch;
  AlloMem< int32_t, 1, 1, 32 > mp16_1_mem;
  SC_CTOR(top) : v1845_in("v1845_in"), v1845_out("v1845_out"), v1845_fifo("v1845_fifo"), v1846_in("v1846_in"), v1846_out("v1846_out"), v1846_fifo("v1846_fifo"), v1847_in("v1847_in"), v1847_out("v1847_out"), v1847_fifo("v1847_fifo"), v1848_in("v1848_in"), v1848_out("v1848_out"), v1848_fifo("v1848_fifo"), v1849_in("v1849_in"), v1849_out("v1849_out"), v1849_fifo("v1849_fifo"), v1850_in("v1850_in"), v1850_out("v1850_out"), v1850_fifo("v1850_fifo"), v1851_in("v1851_in"), v1851_out("v1851_out"), v1851_fifo("v1851_fifo"), v1852_in("v1852_in"), v1852_out("v1852_out"), v1852_fifo("v1852_fifo"), v1853_in("v1853_in"), v1853_out("v1853_out"), v1853_fifo("v1853_fifo"), v1854_in("v1854_in"), v1854_out("v1854_out"), v1854_fifo("v1854_fifo"), v1855_in("v1855_in"), v1855_out("v1855_out"), v1855_fifo("v1855_fifo"), v1856_in("v1856_in"), v1856_out("v1856_out"), v1856_fifo("v1856_fifo"), v1857_in("v1857_in"), v1857_out("v1857_out"), v1857_fifo("v1857_fifo"), v1858_in("v1858_in"), v1858_out("v1858_out"), v1858_fifo("v1858_fifo"), v1859_in("v1859_in"), v1859_out("v1859_out"), v1859_fifo("v1859_fifo"), v1860_in("v1860_in"), v1860_out("v1860_out"), v1860_fifo("v1860_fifo"), v1861_in("v1861_in"), v1861_out("v1861_out"), v1861_fifo("v1861_fifo"), v1862_in("v1862_in"), v1862_out("v1862_out"), v1862_fifo("v1862_fifo"), v1863_in("v1863_in"), v1863_out("v1863_out"), v1863_fifo("v1863_fifo"), v1864_in("v1864_in"), v1864_out("v1864_out"), v1864_fifo("v1864_fifo"), v1865_in("v1865_in"), v1865_out("v1865_out"), v1865_fifo("v1865_fifo"), v1866_in("v1866_in"), v1866_out("v1866_out"), v1866_fifo("v1866_fifo"), v1867_in("v1867_in"), v1867_out("v1867_out"), v1867_fifo("v1867_fifo"), v1868_in("v1868_in"), v1868_out("v1868_out"), v1868_fifo("v1868_fifo"), v1869_in("v1869_in"), v1869_out("v1869_out"), v1869_fifo("v1869_fifo"), v1870_in("v1870_in"), v1870_out("v1870_out"), v1870_fifo("v1870_fifo"), v1871_in("v1871_in"), v1871_out("v1871_out"), v1871_fifo("v1871_fifo"), v1872_in("v1872_in"), v1872_out("v1872_out"), v1872_fifo("v1872_fifo"), v1873_in("v1873_in"), v1873_out("v1873_out"), v1873_fifo("v1873_fifo"), v1874_in("v1874_in"), v1874_out("v1874_out"), v1874_fifo("v1874_fifo"), v1875_in("v1875_in"), v1875_out("v1875_out"), v1875_fifo("v1875_fifo"), v1876_in("v1876_in"), v1876_out("v1876_out"), v1876_fifo("v1876_fifo"), u0("u0"), u1("u1"), u2("u2"), u3("u3"), u4("u4"), u5("u5"), u6("u6"), u7("u7"), u8("u8"), u9("u9"), u10("u10"), u11("u11"), u12("u12"), u13("u13"), u14("u14"), u15("u15"), u16("u16"), mp0_0_req_ch("mp0_0_req_ch"), mp0_0_rsp_ch("mp0_0_rsp_ch"), mp0_0_mem("mp0_0_mem"), mp1_0_req_ch("mp1_0_req_ch"), mp1_0_rsp_ch("mp1_0_rsp_ch"), mp1_0_mem("mp1_0_mem"), mp1_1_req_ch("mp1_1_req_ch"), mp1_1_rsp_ch("mp1_1_rsp_ch"), mp1_1_mem("mp1_1_mem"), mp1_2_req_ch("mp1_2_req_ch"), mp1_2_rsp_ch("mp1_2_rsp_ch"), mp1_2_mem("mp1_2_mem"), mp2_0_req_ch("mp2_0_req_ch"), mp2_0_rsp_ch("mp2_0_rsp_ch"), mp2_0_mem("mp2_0_mem"), mp2_1_req_ch("mp2_1_req_ch"), mp2_1_rsp_ch("mp2_1_rsp_ch"), mp2_1_mem("mp2_1_mem"), mp2_2_req_ch("mp2_2_req_ch"), mp2_2_rsp_ch("mp2_2_rsp_ch"), mp2_2_mem("mp2_2_mem"), mp3_0_req_ch("mp3_0_req_ch"), mp3_0_rsp_ch("mp3_0_rsp_ch"), mp3_0_mem("mp3_0_mem"), mp3_1_req_ch("mp3_1_req_ch"), mp3_1_rsp_ch("mp3_1_rsp_ch"), mp3_1_mem("mp3_1_mem"), mp3_2_req_ch("mp3_2_req_ch"), mp3_2_rsp_ch("mp3_2_rsp_ch"), mp3_2_mem("mp3_2_mem"), mp4_0_req_ch("mp4_0_req_ch"), mp4_0_rsp_ch("mp4_0_rsp_ch"), mp4_0_mem("mp4_0_mem"), mp4_1_req_ch("mp4_1_req_ch"), mp4_1_rsp_ch("mp4_1_rsp_ch"), mp4_1_mem("mp4_1_mem"), mp4_2_req_ch("mp4_2_req_ch"), mp4_2_rsp_ch("mp4_2_rsp_ch"), mp4_2_mem("mp4_2_mem"), mp5_0_req_ch("mp5_0_req_ch"), mp5_0_mem("mp5_0_mem"), mp5_1_req_ch("mp5_1_req_ch"), mp5_1_rsp_ch("mp5_1_rsp_ch"), mp5_1_mem("mp5_1_mem"), mp6_0_req_ch("mp6_0_req_ch"), mp6_0_mem("mp6_0_mem"), mp6_1_req_ch("mp6_1_req_ch"), mp6_1_rsp_ch("mp6_1_rsp_ch"), mp6_1_mem("mp6_1_mem"), mp7_0_req_ch("mp7_0_req_ch"), mp7_0_mem("mp7_0_mem"), mp7_1_req_ch("mp7_1_req_ch"), mp7_1_rsp_ch("mp7_1_rsp_ch"), mp7_1_mem("mp7_1_mem"), mp8_0_req_ch("mp8_0_req_ch"), mp8_0_mem("mp8_0_mem"), mp8_1_req_ch("mp8_1_req_ch"), mp8_1_rsp_ch("mp8_1_rsp_ch"), mp8_1_mem("mp8_1_mem"), mp9_0_req_ch("mp9_0_req_ch"), mp9_0_rsp_ch("mp9_0_rsp_ch"), mp9_0_mem("mp9_0_mem"), mp9_1_req_ch("mp9_1_req_ch"), mp9_1_rsp_ch("mp9_1_rsp_ch"), mp9_1_mem("mp9_1_mem"), mp10_0_req_ch("mp10_0_req_ch"), mp10_0_rsp_ch("mp10_0_rsp_ch"), mp10_0_mem("mp10_0_mem"), mp10_1_req_ch("mp10_1_req_ch"), mp10_1_rsp_ch("mp10_1_rsp_ch"), mp10_1_mem("mp10_1_mem"), mp11_0_req_ch("mp11_0_req_ch"), mp11_0_rsp_ch("mp11_0_rsp_ch"), mp11_0_mem("mp11_0_mem"), mp11_1_req_ch("mp11_1_req_ch"), mp11_1_rsp_ch("mp11_1_rsp_ch"), mp11_1_mem("mp11_1_mem"), mp12_0_req_ch("mp12_0_req_ch"), mp12_0_rsp_ch("mp12_0_rsp_ch"), mp12_0_mem("mp12_0_mem"), mp12_1_req_ch("mp12_1_req_ch"), mp12_1_rsp_ch("mp12_1_rsp_ch"), mp12_1_mem("mp12_1_mem"), mp13_0_req_ch("mp13_0_req_ch"), mp13_0_mem("mp13_0_mem"), mp13_1_req_ch("mp13_1_req_ch"), mp13_1_rsp_ch("mp13_1_rsp_ch"), mp13_1_mem("mp13_1_mem"), mp14_0_req_ch("mp14_0_req_ch"), mp14_0_mem("mp14_0_mem"), mp14_1_req_ch("mp14_1_req_ch"), mp14_1_rsp_ch("mp14_1_rsp_ch"), mp14_1_mem("mp14_1_mem"), mp15_0_req_ch("mp15_0_req_ch"), mp15_0_mem("mp15_0_mem"), mp15_1_req_ch("mp15_1_req_ch"), mp15_1_rsp_ch("mp15_1_rsp_ch"), mp15_1_mem("mp15_1_mem"), mp16_0_req_ch("mp16_0_req_ch"), mp16_0_mem("mp16_0_mem"), mp16_1_req_ch("mp16_1_req_ch"), mp16_1_rsp_ch("mp16_1_rsp_ch"), mp16_1_mem("mp16_1_mem") {
    u0.clk(clk);
    u0.rst(rst);
    u0.v0_req(mp0_0_req_ch);
    u0.v0_rsp(mp0_0_rsp_ch);
    u0.v1(v1854_in);
    u0.v2(v1855_in);
    u0.v3(v1858_in);
    u0.v4(v1859_in);
    u0.v5(v1846_in);
    u0.v6(v1847_in);
    u0.v7(v1850_in);
    u0.v8(v1851_in);
    u0.v9(v1861_in);
    u0.v10(v1864_in);
    u0.v11(v1865_in);
    u0.v12(v1868_in);
    u0.v13(v1869_in);
    u0.v14(v1872_in);
    u0.v15(v1873_in);
    u0.v16(v1876_in);
    u0.v17(v1853_out);
    u0.v18(v1856_out);
    u0.v19(v1857_out);
    u0.v20(v1860_out);
    u0.v21(v1862_out);
    u0.v22(v1863_out);
    u0.v23(v1866_out);
    u0.v24(v1867_out);
    u0.v25(v1875_out);
    u0.v26(v1874_out);
    u0.v27(v1871_out);
    u0.v28(v1870_out);
    u0.v29(v1845_out);
    u0.v30(v1848_out);
    u0.v31(v1849_out);
    u0.v32(v1852_out);
    u1.clk(clk);
    u1.rst(rst);
    u1.v1108_req(mp1_0_req_ch);
    u1.v1108_rsp(mp1_0_rsp_ch);
    u1.v1109_req(mp1_1_req_ch);
    u1.v1109_rsp(mp1_1_rsp_ch);
    u1.v1110_req(mp1_2_req_ch);
    u1.v1110_rsp(mp1_2_rsp_ch);
    u1.v1111(v1845_in);
    u1.v1112(v1869_out);
    u2.clk(clk);
    u2.rst(rst);
    u2.v1165_req(mp2_0_req_ch);
    u2.v1165_rsp(mp2_0_rsp_ch);
    u2.v1166_req(mp2_1_req_ch);
    u2.v1166_rsp(mp2_1_rsp_ch);
    u2.v1167_req(mp2_2_req_ch);
    u2.v1167_rsp(mp2_2_rsp_ch);
    u2.v1168(v1848_in);
    u2.v1169(v1872_out);
    u3.clk(clk);
    u3.rst(rst);
    u3.v1222_req(mp3_0_req_ch);
    u3.v1222_rsp(mp3_0_rsp_ch);
    u3.v1223_req(mp3_1_req_ch);
    u3.v1223_rsp(mp3_1_rsp_ch);
    u3.v1224_req(mp3_2_req_ch);
    u3.v1224_rsp(mp3_2_rsp_ch);
    u3.v1225(v1849_in);
    u3.v1226(v1873_out);
    u4.clk(clk);
    u4.rst(rst);
    u4.v1279_req(mp4_0_req_ch);
    u4.v1279_rsp(mp4_0_rsp_ch);
    u4.v1280_req(mp4_1_req_ch);
    u4.v1280_rsp(mp4_1_rsp_ch);
    u4.v1281_req(mp4_2_req_ch);
    u4.v1281_rsp(mp4_2_rsp_ch);
    u4.v1282(v1852_in);
    u4.v1283(v1876_out);
    u5.clk(clk);
    u5.rst(rst);
    u5.v1336_req(mp5_0_req_ch);
    u5.v1337_req(mp5_1_req_ch);
    u5.v1337_rsp(mp5_1_rsp_ch);
    u5.v1338(v1871_in);
    u5.v1339(v1847_out);
    u6.clk(clk);
    u6.rst(rst);
    u6.v1371_req(mp6_0_req_ch);
    u6.v1372_req(mp6_1_req_ch);
    u6.v1372_rsp(mp6_1_rsp_ch);
    u6.v1373(v1870_in);
    u6.v1374(v1846_out);
    u7.clk(clk);
    u7.rst(rst);
    u7.v1406_req(mp7_0_req_ch);
    u7.v1407_req(mp7_1_req_ch);
    u7.v1407_rsp(mp7_1_rsp_ch);
    u7.v1408(v1875_in);
    u7.v1409(v1851_out);
    u8.clk(clk);
    u8.rst(rst);
    u8.v1441_req(mp8_0_req_ch);
    u8.v1442_req(mp8_1_req_ch);
    u8.v1442_rsp(mp8_1_rsp_ch);
    u8.v1443(v1874_in);
    u8.v1444(v1850_out);
    u9.clk(clk);
    u9.rst(rst);
    u9.v1476_req(mp9_0_req_ch);
    u9.v1476_rsp(mp9_0_rsp_ch);
    u9.v1477_req(mp9_1_req_ch);
    u9.v1477_rsp(mp9_1_rsp_ch);
    u9.v1478(v1853_in);
    u9.v1479(v1861_out);
    u10.clk(clk);
    u10.rst(rst);
    u10.v1528_req(mp10_0_req_ch);
    u10.v1528_rsp(mp10_0_rsp_ch);
    u10.v1529_req(mp10_1_req_ch);
    u10.v1529_rsp(mp10_1_rsp_ch);
    u10.v1530(v1856_in);
    u10.v1531(v1864_out);
    u11.clk(clk);
    u11.rst(rst);
    u11.v1580_req(mp11_0_req_ch);
    u11.v1580_rsp(mp11_0_rsp_ch);
    u11.v1581_req(mp11_1_req_ch);
    u11.v1581_rsp(mp11_1_rsp_ch);
    u11.v1582(v1857_in);
    u11.v1583(v1865_out);
    u12.clk(clk);
    u12.rst(rst);
    u12.v1632_req(mp12_0_req_ch);
    u12.v1632_rsp(mp12_0_rsp_ch);
    u12.v1633_req(mp12_1_req_ch);
    u12.v1633_rsp(mp12_1_rsp_ch);
    u12.v1634(v1860_in);
    u12.v1635(v1868_out);
    u13.clk(clk);
    u13.rst(rst);
    u13.v1684_req(mp13_0_req_ch);
    u13.v1685_req(mp13_1_req_ch);
    u13.v1685_rsp(mp13_1_rsp_ch);
    u13.v1686(v1863_in);
    u13.v1687(v1855_out);
    u14.clk(clk);
    u14.rst(rst);
    u14.v1719_req(mp14_0_req_ch);
    u14.v1720_req(mp14_1_req_ch);
    u14.v1720_rsp(mp14_1_rsp_ch);
    u14.v1721(v1862_in);
    u14.v1722(v1854_out);
    u15.clk(clk);
    u15.rst(rst);
    u15.v1754_req(mp15_0_req_ch);
    u15.v1755_req(mp15_1_req_ch);
    u15.v1755_rsp(mp15_1_rsp_ch);
    u15.v1756(v1867_in);
    u15.v1757(v1859_out);
    u16.clk(clk);
    u16.rst(rst);
    u16.v1789_req(mp16_0_req_ch);
    u16.v1790_req(mp16_1_req_ch);
    u16.v1790_rsp(mp16_1_rsp_ch);
    u16.v1791(v1866_in);
    u16.v1792(v1858_out);
    v1845_fifo.clk(clk);
    v1845_fifo.rst(rst);
    v1845_fifo.in(v1845_in);
    v1845_fifo.out(v1845_out);
    v1846_fifo.clk(clk);
    v1846_fifo.rst(rst);
    v1846_fifo.in(v1846_in);
    v1846_fifo.out(v1846_out);
    v1847_fifo.clk(clk);
    v1847_fifo.rst(rst);
    v1847_fifo.in(v1847_in);
    v1847_fifo.out(v1847_out);
    v1848_fifo.clk(clk);
    v1848_fifo.rst(rst);
    v1848_fifo.in(v1848_in);
    v1848_fifo.out(v1848_out);
    v1849_fifo.clk(clk);
    v1849_fifo.rst(rst);
    v1849_fifo.in(v1849_in);
    v1849_fifo.out(v1849_out);
    v1850_fifo.clk(clk);
    v1850_fifo.rst(rst);
    v1850_fifo.in(v1850_in);
    v1850_fifo.out(v1850_out);
    v1851_fifo.clk(clk);
    v1851_fifo.rst(rst);
    v1851_fifo.in(v1851_in);
    v1851_fifo.out(v1851_out);
    v1852_fifo.clk(clk);
    v1852_fifo.rst(rst);
    v1852_fifo.in(v1852_in);
    v1852_fifo.out(v1852_out);
    v1853_fifo.clk(clk);
    v1853_fifo.rst(rst);
    v1853_fifo.in(v1853_in);
    v1853_fifo.out(v1853_out);
    v1854_fifo.clk(clk);
    v1854_fifo.rst(rst);
    v1854_fifo.in(v1854_in);
    v1854_fifo.out(v1854_out);
    v1855_fifo.clk(clk);
    v1855_fifo.rst(rst);
    v1855_fifo.in(v1855_in);
    v1855_fifo.out(v1855_out);
    v1856_fifo.clk(clk);
    v1856_fifo.rst(rst);
    v1856_fifo.in(v1856_in);
    v1856_fifo.out(v1856_out);
    v1857_fifo.clk(clk);
    v1857_fifo.rst(rst);
    v1857_fifo.in(v1857_in);
    v1857_fifo.out(v1857_out);
    v1858_fifo.clk(clk);
    v1858_fifo.rst(rst);
    v1858_fifo.in(v1858_in);
    v1858_fifo.out(v1858_out);
    v1859_fifo.clk(clk);
    v1859_fifo.rst(rst);
    v1859_fifo.in(v1859_in);
    v1859_fifo.out(v1859_out);
    v1860_fifo.clk(clk);
    v1860_fifo.rst(rst);
    v1860_fifo.in(v1860_in);
    v1860_fifo.out(v1860_out);
    v1861_fifo.clk(clk);
    v1861_fifo.rst(rst);
    v1861_fifo.in(v1861_in);
    v1861_fifo.out(v1861_out);
    v1862_fifo.clk(clk);
    v1862_fifo.rst(rst);
    v1862_fifo.in(v1862_in);
    v1862_fifo.out(v1862_out);
    v1863_fifo.clk(clk);
    v1863_fifo.rst(rst);
    v1863_fifo.in(v1863_in);
    v1863_fifo.out(v1863_out);
    v1864_fifo.clk(clk);
    v1864_fifo.rst(rst);
    v1864_fifo.in(v1864_in);
    v1864_fifo.out(v1864_out);
    v1865_fifo.clk(clk);
    v1865_fifo.rst(rst);
    v1865_fifo.in(v1865_in);
    v1865_fifo.out(v1865_out);
    v1866_fifo.clk(clk);
    v1866_fifo.rst(rst);
    v1866_fifo.in(v1866_in);
    v1866_fifo.out(v1866_out);
    v1867_fifo.clk(clk);
    v1867_fifo.rst(rst);
    v1867_fifo.in(v1867_in);
    v1867_fifo.out(v1867_out);
    v1868_fifo.clk(clk);
    v1868_fifo.rst(rst);
    v1868_fifo.in(v1868_in);
    v1868_fifo.out(v1868_out);
    v1869_fifo.clk(clk);
    v1869_fifo.rst(rst);
    v1869_fifo.in(v1869_in);
    v1869_fifo.out(v1869_out);
    v1870_fifo.clk(clk);
    v1870_fifo.rst(rst);
    v1870_fifo.in(v1870_in);
    v1870_fifo.out(v1870_out);
    v1871_fifo.clk(clk);
    v1871_fifo.rst(rst);
    v1871_fifo.in(v1871_in);
    v1871_fifo.out(v1871_out);
    v1872_fifo.clk(clk);
    v1872_fifo.rst(rst);
    v1872_fifo.in(v1872_in);
    v1872_fifo.out(v1872_out);
    v1873_fifo.clk(clk);
    v1873_fifo.rst(rst);
    v1873_fifo.in(v1873_in);
    v1873_fifo.out(v1873_out);
    v1874_fifo.clk(clk);
    v1874_fifo.rst(rst);
    v1874_fifo.in(v1874_in);
    v1874_fifo.out(v1874_out);
    v1875_fifo.clk(clk);
    v1875_fifo.rst(rst);
    v1875_fifo.in(v1875_in);
    v1875_fifo.out(v1875_out);
    v1876_fifo.clk(clk);
    v1876_fifo.rst(rst);
    v1876_fifo.in(v1876_in);
    v1876_fifo.out(v1876_out);
    mp0_0_mem.clk(clk);
    mp0_0_mem.rst(rst);
    mp0_0_mem.req(mp0_0_req_ch);
    mp0_0_mem.rsp(mp0_0_rsp_ch);
    mp1_0_mem.clk(clk);
    mp1_0_mem.rst(rst);
    mp1_0_mem.req(mp1_0_req_ch);
    mp1_0_mem.rsp(mp1_0_rsp_ch);
    mp1_1_mem.clk(clk);
    mp1_1_mem.rst(rst);
    mp1_1_mem.req(mp1_1_req_ch);
    mp1_1_mem.rsp(mp1_1_rsp_ch);
    mp1_2_mem.clk(clk);
    mp1_2_mem.rst(rst);
    mp1_2_mem.req(mp1_2_req_ch);
    mp1_2_mem.rsp(mp1_2_rsp_ch);
    mp2_0_mem.clk(clk);
    mp2_0_mem.rst(rst);
    mp2_0_mem.req(mp2_0_req_ch);
    mp2_0_mem.rsp(mp2_0_rsp_ch);
    mp2_1_mem.clk(clk);
    mp2_1_mem.rst(rst);
    mp2_1_mem.req(mp2_1_req_ch);
    mp2_1_mem.rsp(mp2_1_rsp_ch);
    mp2_2_mem.clk(clk);
    mp2_2_mem.rst(rst);
    mp2_2_mem.req(mp2_2_req_ch);
    mp2_2_mem.rsp(mp2_2_rsp_ch);
    mp3_0_mem.clk(clk);
    mp3_0_mem.rst(rst);
    mp3_0_mem.req(mp3_0_req_ch);
    mp3_0_mem.rsp(mp3_0_rsp_ch);
    mp3_1_mem.clk(clk);
    mp3_1_mem.rst(rst);
    mp3_1_mem.req(mp3_1_req_ch);
    mp3_1_mem.rsp(mp3_1_rsp_ch);
    mp3_2_mem.clk(clk);
    mp3_2_mem.rst(rst);
    mp3_2_mem.req(mp3_2_req_ch);
    mp3_2_mem.rsp(mp3_2_rsp_ch);
    mp4_0_mem.clk(clk);
    mp4_0_mem.rst(rst);
    mp4_0_mem.req(mp4_0_req_ch);
    mp4_0_mem.rsp(mp4_0_rsp_ch);
    mp4_1_mem.clk(clk);
    mp4_1_mem.rst(rst);
    mp4_1_mem.req(mp4_1_req_ch);
    mp4_1_mem.rsp(mp4_1_rsp_ch);
    mp4_2_mem.clk(clk);
    mp4_2_mem.rst(rst);
    mp4_2_mem.req(mp4_2_req_ch);
    mp4_2_mem.rsp(mp4_2_rsp_ch);
    mp5_0_mem.clk(clk);
    mp5_0_mem.rst(rst);
    mp5_0_mem.req(mp5_0_req_ch);
    mp5_1_mem.clk(clk);
    mp5_1_mem.rst(rst);
    mp5_1_mem.req(mp5_1_req_ch);
    mp5_1_mem.rsp(mp5_1_rsp_ch);
    mp6_0_mem.clk(clk);
    mp6_0_mem.rst(rst);
    mp6_0_mem.req(mp6_0_req_ch);
    mp6_1_mem.clk(clk);
    mp6_1_mem.rst(rst);
    mp6_1_mem.req(mp6_1_req_ch);
    mp6_1_mem.rsp(mp6_1_rsp_ch);
    mp7_0_mem.clk(clk);
    mp7_0_mem.rst(rst);
    mp7_0_mem.req(mp7_0_req_ch);
    mp7_1_mem.clk(clk);
    mp7_1_mem.rst(rst);
    mp7_1_mem.req(mp7_1_req_ch);
    mp7_1_mem.rsp(mp7_1_rsp_ch);
    mp8_0_mem.clk(clk);
    mp8_0_mem.rst(rst);
    mp8_0_mem.req(mp8_0_req_ch);
    mp8_1_mem.clk(clk);
    mp8_1_mem.rst(rst);
    mp8_1_mem.req(mp8_1_req_ch);
    mp8_1_mem.rsp(mp8_1_rsp_ch);
    mp9_0_mem.clk(clk);
    mp9_0_mem.rst(rst);
    mp9_0_mem.req(mp9_0_req_ch);
    mp9_0_mem.rsp(mp9_0_rsp_ch);
    mp9_1_mem.clk(clk);
    mp9_1_mem.rst(rst);
    mp9_1_mem.req(mp9_1_req_ch);
    mp9_1_mem.rsp(mp9_1_rsp_ch);
    mp10_0_mem.clk(clk);
    mp10_0_mem.rst(rst);
    mp10_0_mem.req(mp10_0_req_ch);
    mp10_0_mem.rsp(mp10_0_rsp_ch);
    mp10_1_mem.clk(clk);
    mp10_1_mem.rst(rst);
    mp10_1_mem.req(mp10_1_req_ch);
    mp10_1_mem.rsp(mp10_1_rsp_ch);
    mp11_0_mem.clk(clk);
    mp11_0_mem.rst(rst);
    mp11_0_mem.req(mp11_0_req_ch);
    mp11_0_mem.rsp(mp11_0_rsp_ch);
    mp11_1_mem.clk(clk);
    mp11_1_mem.rst(rst);
    mp11_1_mem.req(mp11_1_req_ch);
    mp11_1_mem.rsp(mp11_1_rsp_ch);
    mp12_0_mem.clk(clk);
    mp12_0_mem.rst(rst);
    mp12_0_mem.req(mp12_0_req_ch);
    mp12_0_mem.rsp(mp12_0_rsp_ch);
    mp12_1_mem.clk(clk);
    mp12_1_mem.rst(rst);
    mp12_1_mem.req(mp12_1_req_ch);
    mp12_1_mem.rsp(mp12_1_rsp_ch);
    mp13_0_mem.clk(clk);
    mp13_0_mem.rst(rst);
    mp13_0_mem.req(mp13_0_req_ch);
    mp13_1_mem.clk(clk);
    mp13_1_mem.rst(rst);
    mp13_1_mem.req(mp13_1_req_ch);
    mp13_1_mem.rsp(mp13_1_rsp_ch);
    mp14_0_mem.clk(clk);
    mp14_0_mem.rst(rst);
    mp14_0_mem.req(mp14_0_req_ch);
    mp14_1_mem.clk(clk);
    mp14_1_mem.rst(rst);
    mp14_1_mem.req(mp14_1_req_ch);
    mp14_1_mem.rsp(mp14_1_rsp_ch);
    mp15_0_mem.clk(clk);
    mp15_0_mem.rst(rst);
    mp15_0_mem.req(mp15_0_req_ch);
    mp15_1_mem.clk(clk);
    mp15_1_mem.rst(rst);
    mp15_1_mem.req(mp15_1_req_ch);
    mp15_1_mem.rsp(mp15_1_rsp_ch);
    mp16_0_mem.clk(clk);
    mp16_0_mem.rst(rst);
    mp16_0_mem.req(mp16_0_req_ch);
    mp16_1_mem.clk(clk);
    mp16_1_mem.rst(rst);
    mp16_1_mem.req(mp16_1_req_ch);
    mp16_1_mem.rsp(mp16_1_rsp_ch);
  }
};

SC_MODULE(tb) {
  sc_clock clk;
  sc_signal<bool> rst;
  top dut;
  SC_HAS_PROCESS(tb);
  tb(sc_module_name n) : sc_module(n), clk("clk", 1, SC_NS), dut("dut") {
    dut.clk(clk); dut.rst(rst);
    SC_THREAD(src); sensitive << clk.posedge_event(); async_reset_signal_is(rst, false);
    SC_THREAD(snk); sensitive << clk.posedge_event(); async_reset_signal_is(rst, false);
  }
  void src() {
    wait();
  }
  void snk() {
    wait();
  }
};

int sc_main(int, char *[]) {
  static tb t("t");
  { std::ifstream _f("input0.data"); long long _v; for (int f = 0; f < 1; ++f) { _f >> _v; t.dut.mp0_0_mem.mem[f] = (int32_t)_v; } }
  { std::ifstream _f("input1.data"); half _v; for (int f = 0; f < 215; ++f) { _f >> _v; t.dut.mp1_0_mem.mem[f] = (half)_v; } }
  { std::ifstream _f("input2.data"); long long _v; for (int f = 0; f < 215; ++f) { _f >> _v; t.dut.mp1_1_mem.mem[f] = (int32_t)_v; } }
  { std::ifstream _f("input0.data"); long long _v; for (int f = 0; f < 1; ++f) { _f >> _v; t.dut.mp1_2_mem.mem[f] = (int32_t)_v; } }
  { std::ifstream _f("input3.data"); half _v; for (int f = 0; f < 215; ++f) { _f >> _v; t.dut.mp2_0_mem.mem[f] = (half)_v; } }
  { std::ifstream _f("input4.data"); long long _v; for (int f = 0; f < 215; ++f) { _f >> _v; t.dut.mp2_1_mem.mem[f] = (int32_t)_v; } }
  { std::ifstream _f("input0.data"); long long _v; for (int f = 0; f < 1; ++f) { _f >> _v; t.dut.mp2_2_mem.mem[f] = (int32_t)_v; } }
  { std::ifstream _f("input5.data"); half _v; for (int f = 0; f < 215; ++f) { _f >> _v; t.dut.mp3_0_mem.mem[f] = (half)_v; } }
  { std::ifstream _f("input6.data"); long long _v; for (int f = 0; f < 215; ++f) { _f >> _v; t.dut.mp3_1_mem.mem[f] = (int32_t)_v; } }
  { std::ifstream _f("input0.data"); long long _v; for (int f = 0; f < 1; ++f) { _f >> _v; t.dut.mp3_2_mem.mem[f] = (int32_t)_v; } }
  { std::ifstream _f("input7.data"); half _v; for (int f = 0; f < 215; ++f) { _f >> _v; t.dut.mp4_0_mem.mem[f] = (half)_v; } }
  { std::ifstream _f("input8.data"); long long _v; for (int f = 0; f < 215; ++f) { _f >> _v; t.dut.mp4_1_mem.mem[f] = (int32_t)_v; } }
  { std::ifstream _f("input0.data"); long long _v; for (int f = 0; f < 1; ++f) { _f >> _v; t.dut.mp4_2_mem.mem[f] = (int32_t)_v; } }
  { std::ifstream _f("input0.data"); long long _v; for (int f = 0; f < 1; ++f) { _f >> _v; t.dut.mp5_1_mem.mem[f] = (int32_t)_v; } }
  { std::ifstream _f("input0.data"); long long _v; for (int f = 0; f < 1; ++f) { _f >> _v; t.dut.mp6_1_mem.mem[f] = (int32_t)_v; } }
  { std::ifstream _f("input0.data"); long long _v; for (int f = 0; f < 1; ++f) { _f >> _v; t.dut.mp7_1_mem.mem[f] = (int32_t)_v; } }
  { std::ifstream _f("input0.data"); long long _v; for (int f = 0; f < 1; ++f) { _f >> _v; t.dut.mp8_1_mem.mem[f] = (int32_t)_v; } }
  { std::ifstream _f("input9.data"); long long _v; for (int f = 0; f < 215; ++f) { _f >> _v; t.dut.mp9_0_mem.mem[f] = (int32_t)_v; } }
  { std::ifstream _f("input0.data"); long long _v; for (int f = 0; f < 1; ++f) { _f >> _v; t.dut.mp9_1_mem.mem[f] = (int32_t)_v; } }
  { std::ifstream _f("input10.data"); long long _v; for (int f = 0; f < 215; ++f) { _f >> _v; t.dut.mp10_0_mem.mem[f] = (int32_t)_v; } }
  { std::ifstream _f("input0.data"); long long _v; for (int f = 0; f < 1; ++f) { _f >> _v; t.dut.mp10_1_mem.mem[f] = (int32_t)_v; } }
  { std::ifstream _f("input11.data"); long long _v; for (int f = 0; f < 215; ++f) { _f >> _v; t.dut.mp11_0_mem.mem[f] = (int32_t)_v; } }
  { std::ifstream _f("input0.data"); long long _v; for (int f = 0; f < 1; ++f) { _f >> _v; t.dut.mp11_1_mem.mem[f] = (int32_t)_v; } }
  { std::ifstream _f("input12.data"); long long _v; for (int f = 0; f < 215; ++f) { _f >> _v; t.dut.mp12_0_mem.mem[f] = (int32_t)_v; } }
  { std::ifstream _f("input0.data"); long long _v; for (int f = 0; f < 1; ++f) { _f >> _v; t.dut.mp12_1_mem.mem[f] = (int32_t)_v; } }
  { std::ifstream _f("input0.data"); long long _v; for (int f = 0; f < 1; ++f) { _f >> _v; t.dut.mp13_1_mem.mem[f] = (int32_t)_v; } }
  { std::ifstream _f("input0.data"); long long _v; for (int f = 0; f < 1; ++f) { _f >> _v; t.dut.mp14_1_mem.mem[f] = (int32_t)_v; } }
  { std::ifstream _f("input0.data"); long long _v; for (int f = 0; f < 1; ++f) { _f >> _v; t.dut.mp15_1_mem.mem[f] = (int32_t)_v; } }
  { std::ifstream _f("input0.data"); long long _v; for (int f = 0; f < 1; ++f) { _f >> _v; t.dut.mp16_1_mem.mem[f] = (int32_t)_v; } }
  t.rst = 0; sc_start(1, SC_NS);
  t.rst = 1;
  for (long long _c = 0; _c < 630000LL && __allo_done < 17; ++_c) sc_start(1, SC_NS); // wait for completion
  sc_start(256, SC_NS); // settle in-flight memory writes
  { std::ofstream _f("output0.data");
    for (int f = 0; f < 215; ++f) {
      float _s = 0;
      _s += t.dut.mp5_0_mem.mem[f].to_float();
      _f << std::setprecision(9) << _s << "\n";
    } }
  { std::ofstream _f("output1.data");
    for (int f = 0; f < 215; ++f) {
      float _s = 0;
      _s += t.dut.mp6_0_mem.mem[f].to_float();
      _f << std::setprecision(9) << _s << "\n";
    } }
  { std::ofstream _f("output2.data");
    for (int f = 0; f < 215; ++f) {
      float _s = 0;
      _s += t.dut.mp7_0_mem.mem[f].to_float();
      _f << std::setprecision(9) << _s << "\n";
    } }
  { std::ofstream _f("output3.data");
    for (int f = 0; f < 215; ++f) {
      float _s = 0;
      _s += t.dut.mp8_0_mem.mem[f].to_float();
      _f << std::setprecision(9) << _s << "\n";
    } }
  { std::ofstream _f("output4.data");
    for (int f = 0; f < 215; ++f) {
      long long _s = 0;
      _s += (long long) t.dut.mp13_0_mem.mem[f];
      _f << _s << "\n";
    } }
  { std::ofstream _f("output5.data");
    for (int f = 0; f < 215; ++f) {
      long long _s = 0;
      _s += (long long) t.dut.mp14_0_mem.mem[f];
      _f << _s << "\n";
    } }
  { std::ofstream _f("output6.data");
    for (int f = 0; f < 215; ++f) {
      long long _s = 0;
      _s += (long long) t.dut.mp15_0_mem.mem[f];
      _f << _s << "\n";
    } }
  { std::ofstream _f("output7.data");
    for (int f = 0; f < 215; ++f) {
      long long _s = 0;
      _s += (long long) t.dut.mp16_0_mem.mem[f];
      _f << _s << "\n";
    } }
  return 0;
}
