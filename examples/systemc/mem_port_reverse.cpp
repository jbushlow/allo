
//===------------------------------------------------------------*- C++ -*-===//
// Automatically generated file for SystemC (Catapult HLS / MatchLib Connections).
//===----------------------------------------------------------------------===//
#include <systemc.h>
#include <mc_connections.h>   // MatchLib Connections (LI valid/ready channels)
#include <mc_scverify.h>      // SCVerify testbench macros (CCS_MAIN / CCS_DESIGN)
#include <ac_int.h>
#include <ac_fixed.h>
#include <stdint.h>
#include <iostream>
#include <fstream>
#include <algorithm>
// The reused body emits bare max()/min() for the Allo max/min intrinsics (Vitis
// resolves them via hls::); bind them to std:: so the same body compiles here.
using std::max;
using std::min;
// The reused Vivado-emitter body prints Vitis ap_(u)int types; alias them to
// Catapult's ac_int so the same body compiles. (TODO: emit ac_int/ac_fixed
// natively via a type-name override, like getCatapultTypeName in the Catapult
// emitter, and drop this shim.)
// For W<=64, ap_(u)int is a plain ac_int alias. For W>64, ac_int has NO implicit
// conversion to a native int, so the reused body's narrowing `int32_t x = wide;`
// (e.g. a GEMM accumulator widened past 64 bits) fails to compile. Add one via a
// thin subclass ONLY in that range — ac_int's own operators remain exact/derived-
// to-base matches, so arithmetic still resolves to them (no builtin ambiguity).
template <int W, bool Big = (W > 64)> struct ap_sel {
  using s = ac_int<W, true>;
  using u = ac_int<W, false>;
};
template <int W> struct ap_sel<W, true> {
  struct s : ac_int<W, true> {
    using ac_int<W, true>::ac_int;
    operator long long() const { return this->to_int64(); }
  };
  struct u : ac_int<W, false> {
    using ac_int<W, false>::ac_int;
    operator unsigned long long() const { return this->to_uint64(); }
  };
};
template <int W> using ap_int = typename ap_sel<W>::s;
template <int W> using ap_uint = typename ap_sel<W>::u;

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
        mem[a] = (T)(int64_t)r.template slc<DATAW>(1 + ADDRW).to_int64();
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
      mem[z] = 0;
    wait();
    while (1) {
      ac_int<1 + ADDRW + DATAW, false> r = req.Pop();
      ac_int<ADDRW, false> a = r.template slc<ADDRW>(1);
      mem[a] = (T)(int64_t)r.template slc<DATAW>(1 + ADDRW).to_int64();
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

SC_MODULE(rev_0) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::Out< ac_int<36, false> > v0_req;
  Connections::In< int32_t > v0_rsp;
  Connections::Out< int32_t > v1;
  SC_HAS_PROCESS(rev_0);
  rev_0(sc_module_name n) : sc_module(n), v0_req("v0_req"), v0_rsp("v0_rsp"), v1("v1") {
    SC_THREAD(run);
    sensitive << clk.pos();
    async_reset_signal_is(rst, false);
  }
  void run() {
    v0_req.Reset();
    v0_rsp.Reset();
    v1.Reset();
    wait();
    while (1) {
      l_S_i_0_i: for (int i = 0; i < 8; i++) {	// L4
        int32_t v3;
        v0_req.Push( (ac_int<36, false>)(((((i * -1) + 7)))) << 1 );
        v3 = v0_rsp.Pop();	// L5
        ap_int<33> v4 = v3;	// L6
        ap_int<33> v5 = v4 + 1;	// L7
        int32_t v6 = v5;	// L8
        v1.Push(v6);	// L9
      }
    }
  }
};

SC_MODULE(top) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::Out< int32_t > v8;
  rev_0 u0;
  Connections::Combinational< ac_int<36, false> > mp0_0_req_ch;
  Connections::Combinational< int32_t > mp0_0_rsp_ch;
  AlloMem< int32_t, 8, 3, 32 > mp0_0_mem;
  SC_CTOR(top) : v8("v8"), u0("u0"), mp0_0_req_ch("mp0_0_req_ch"), mp0_0_rsp_ch("mp0_0_rsp_ch"), mp0_0_mem("mp0_0_mem") {
    u0.clk(clk);
    u0.rst(rst);
    u0.v0_req(mp0_0_req_ch);
    u0.v0_rsp(mp0_0_rsp_ch);
    u0.v1(v8);
    mp0_0_mem.clk(clk);
    mp0_0_mem.rst(rst);
    mp0_0_mem.req(mp0_0_req_ch);
    mp0_0_mem.rsp(mp0_0_rsp_ch);
  }
};

SC_MODULE(tb) {
  sc_clock clk;
  sc_signal<bool> rst;
  top dut;
  Connections::Combinational< int32_t > ch_v8;
  SC_HAS_PROCESS(tb);
  tb(sc_module_name n) : sc_module(n), clk("clk", 1, SC_NS), dut("dut"), ch_v8("ch_v8") {
    dut.clk(clk); dut.rst(rst);
    dut.v8(ch_v8);
    SC_THREAD(src); sensitive << clk.posedge_event(); async_reset_signal_is(rst, false);
    SC_THREAD(snk); sensitive << clk.posedge_event(); async_reset_signal_is(rst, false);
  }
  void src() {
    wait();
  }
  void snk() {
    ch_v8.ResetRead();
    wait();
    { std::ofstream _f("output0.data"); for (int f = 0; f < 8; ++f) _f << ch_v8.Pop() << "\n"; }
    sc_stop();
  }
};

int sc_main(int, char *[]) {
  tb t("t");
  { std::ifstream _f("input0.data"); int32_t _v; for (int f = 0; f < 8; ++f) { _f >> _v; t.dut.mp0_0_mem.mem[f] = _v; } }
  t.rst = 0; sc_start(1, SC_NS);
  t.rst = 1;
  sc_start();
  return 0;
}
