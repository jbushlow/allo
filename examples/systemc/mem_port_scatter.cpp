
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
// The reused Vivado-emitter body prints Vitis ap_(u)int types; alias them to
// Catapult's ac_int so the same body compiles. (TODO: emit ac_int/ac_fixed
// natively via a type-name override, like getCatapultTypeName in the Catapult
// emitter, and drop this shim.)
template <int W> using ap_int = ac_int<W, true>;
template <int W> using ap_uint = ac_int<W, false>;

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
    wait();
    while (1) {
      ac_int<1 + ADDRW + DATAW, false> r = req.Pop();
      ac_int<ADDRW, false> a = r.template slc<ADDRW>(1);
      mem[a] = (T)(int64_t)r.template slc<DATAW>(1 + ADDRW).to_int64();
      wait();
    }
  }
};

SC_MODULE(scat_0) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::In< int32_t > v0;
  Connections::Out< ac_int<36, false> > v1_req;
  SC_HAS_PROCESS(scat_0);
  scat_0(sc_module_name n) : sc_module(n), v0("v0"), v1_req("v1_req") {
    SC_THREAD(run);
    sensitive << clk.pos();
    async_reset_signal_is(rst, false);
  }
  void run() {
    v0.Reset();
    v1_req.Reset();
    wait();
    while (1) {
      l_S_i_0_i: for (int i = 0; i < 8; i++) {	// L4
        int32_t v3 = v0.Pop();	// L5
        ap_int<33> v4 = v3;	// L6
        ap_int<33> v5 = v4 + 1;	// L7
        int32_t v6 = v5;	// L8
        v1_req.Push( ((ac_int<36, false>)(v6) << 4) | ((ac_int<36, false>)(((((i * -1) + 7)))) << 1) | (ac_int<36, false>)1 );	// L9
      }
    }
  }
};

SC_MODULE(top) {
  sc_in_clk clk;
  sc_in<bool> rst;
  Connections::In< int32_t > v7;
  scat_0 u0;
  Connections::Combinational< ac_int<36, false> > v8_req_ch;
  AlloMemW< int32_t, 8, 3, 32 > v8_mem;
  SC_CTOR(top) : v7("v7"), u0("u0"), v8_req_ch("v8_req_ch"), v8_mem("v8_mem") {
    u0.clk(clk);
    u0.rst(rst);
    u0.v0(v7);
    u0.v1_req(v8_req_ch);
    v8_mem.clk(clk);
    v8_mem.rst(rst);
    v8_mem.req(v8_req_ch);
  }
};

SC_MODULE(tb) {
  sc_clock clk;
  sc_signal<bool> rst;
  top dut;
  Connections::Combinational< int32_t > ch_v7;
  SC_HAS_PROCESS(tb);
  tb(sc_module_name n) : sc_module(n), clk("clk", 1, SC_NS), dut("dut"), ch_v7("ch_v7") {
    dut.clk(clk); dut.rst(rst);
    dut.v7(ch_v7);
    SC_THREAD(src); sensitive << clk.posedge_event(); async_reset_signal_is(rst, false);
    SC_THREAD(snk); sensitive << clk.posedge_event(); async_reset_signal_is(rst, false);
  }
  void src() {
    ch_v7.ResetWrite();
    wait();
    { std::ifstream _f("input0.data"); int32_t _v; for (int f = 0; f < 8; ++f) { _f >> _v; ch_v7.Push(_v); } }
  }
  void snk() {
    wait();
  }
};

int sc_main(int, char *[]) {
  tb t("t");
  t.rst = 0; sc_start(1, SC_NS);
  t.rst = 1;
  sc_start(264, SC_NS);
  { std::ofstream _f("output0.data"); for (int f = 0; f < 8; ++f) _f << t.dut.v8_mem.mem[f] << "\n"; }
  return 0;
}
