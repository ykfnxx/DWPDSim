#include <pybind11/pybind11.h>

namespace py = pybind11;

PYBIND11_MODULE(_core, module) {
    module.doc() = "C++ core for DWPDSim";
    module.attr("CORE_VERSION") = "0.3.0";
}
