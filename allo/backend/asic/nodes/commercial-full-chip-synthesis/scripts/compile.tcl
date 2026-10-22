#=========================================================================
# compile.tcl
#=========================================================================
# Author : Christopher Torng
# Date   : September 30, 2018

#-------------------------------------------------------------------------
# Pre-compile checks
#-------------------------------------------------------------------------

# The check_timing command checks for constraint problems such as
# undefined clocking, undefined input arrival times, and undefined output
# constraints. These constraint problems could cause you to overlook
# timing violations. For this reason, the check_timing command is
# recommended whenever you apply new constraints such as clock
# definitions, I/O delays, or timing exceptions.

redirect -tee \
  ${dc_reports_dir}/${dc_design_name}.premapped.checktiming.rpt \
  {check_timing}

# Check design for consistency
#
# Most problems with synthesis will be caught in this report

check_design -summary
check_design \
  > ${dc_reports_dir}/${dc_design_name}.premapped.checkdesign.rpt

#-------------------------------------------------------------------------
# Compile
#-------------------------------------------------------------------------

puts "Info: DC compile_ultra options = $compile_ultra_options"

eval "compile_ultra $compile_ultra_options"

# High-effort area optimization
#
# This command was introduced in I-2013.12 and performs monotonic
# gate-to-gate optimization on mapped designs. It is supposed to improve
# area without degrading timing or leakage.

if { $dc_high_effort_area_opt == True } {
  optimize_netlist -area
}

# Uniquify by prefixing every module in the design with the design name.
# This is useful for hierarchical LVS when multiple blocks use modules
# with the same name but different definitions.

if { $dc_uniquify_with_design_name == True } {
  set uniquify_naming_style "${dc_design_name}_%s_%d"
  uniquify -force
}

# Uniquification can create hierarchical designs after the pre-compile
# multiple-port-net constraint was applied. Reapply it to the complete active
# hierarchy, excluding hardened macro library designs, and run an incremental
# compile so constant port nets are buffered in the emitted gate netlist.

set port_net_fix_designs [get_designs -hierarchical *]
if {[info exists allo_asic_macro_modules]} {
  foreach macro_name $allo_asic_macro_modules {
    set macro_designs [get_designs -quiet $macro_name]
    if {[sizeof_collection $macro_designs] > 0} {
      set port_net_fix_designs \
        [remove_from_collection $port_net_fix_designs $macro_designs]
    }
  }
}
set_fix_multiple_port_nets \
  -all \
  -buffer_constants \
  $port_net_fix_designs
eval "compile_ultra -incremental $compile_ultra_options"

#-------------------------------------------------------------------------
# Post-compile
#-------------------------------------------------------------------------

# Check the final design after incremental multiple-port-net repair.

check_design -summary
check_design > ${dc_reports_dir}/${dc_design_name}.mapped.checkdesign.rpt

# Synopsys Formality

set_svf -off

# Use naming rules to preserve structs and use case insensitive names
# to guarantee unique names even with case insensitive tools 

define_name_rules verilog -preserve_struct_ports -case_insensitive

report_names     \
  -rules verilog \
  > ${dc_reports_dir}/${dc_design_name}.mapped.naming.rpt

# Replace special characters with non-special ones before writing out a
# Verilog netlist (e.g., "\bus[5]" -> "bus_5_")

change_names -rules verilog -hierarchy
