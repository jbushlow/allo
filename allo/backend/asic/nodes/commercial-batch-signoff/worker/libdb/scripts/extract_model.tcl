# Copyright Allo authors. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

#=========================================================================
# extract_model.tcl
#=========================================================================
# Use Synopsys PrimeTime to extract lib
#
# Author : Julian Bushlow
# Date   : June 10, 2026
#

#-------------------------------------------------------------------------
# Extract lib model
#-------------------------------------------------------------------------

update_timing -full

extract_model -library_cell -output ${ptpx_design_name} -format {lib}
