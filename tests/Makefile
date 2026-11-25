# Host tests for the parts of the firmware that have no hardware in them.
#   make        build and run
CXX      ?= g++
CXXFLAGS ?= -O2 -Wall -Wextra -std=c++17
FW        = ../firmware/StaticFire_Stand

all: link_proto_test
	./link_proto_test

link_proto_test: link_proto_test.cpp $(FW)/sf_link_proto.h $(FW)/sf_stand_link.h
	$(CXX) $(CXXFLAGS) -o link_proto_test link_proto_test.cpp

clean:
	rm -f link_proto_test
.PHONY: all clean

# ---------------------------------------------------------------------
# Compile the stand firmware against a do-nothing Arduino shim: catches a
# typo, a wrong type or a bad printf format without a board. It is NOT a
# build - the real one is arduino-cli / the Arduino IDE - but it is the only
# check of the sketch itself available on a workstation.
# ---------------------------------------------------------------------
# The target has 32 bit pointers, a workstation does not: the casts of flash
# addresses (existing code) are silenced here, nothing else is.
SKETCH = $(FW)/StaticFire_Stand.ino
SYN = $(CXX) $(CXXFLAGS) -Wno-unused-parameter -fpermissive -Wno-int-to-pointer-cast \
      -Istub -I$(FW) -fsyntax-only
syntax:
	$(SYN) -x c++ $(SKETCH)
	$(SYN) $(FW)/flashlog.cpp
	@echo "stand firmware compiles clean against the shim"

check: all syntax
.PHONY: syntax check
