cmake_minimum_required(VERSION 3.18)

project(DWPDSim LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(Python REQUIRED COMPONENTS Interpreter Development.Module)
find_package(pybind11 CONFIG REQUIRED)

add_library(
    dwpdsim_core
    STATIC
    cpp/src/metrics.cpp
    cpp/src/policies.cpp
    cpp/src/radix_tree.cpp
    cpp/src/simulator.cpp
    cpp/src/storage.cpp
    cpp/src/trace_writer.cpp
)
target_include_directories(dwpdsim_core PUBLIC cpp/include)
set_target_properties(dwpdsim_core PROPERTIES POSITION_INDEPENDENT_CODE ON)

pybind11_add_module(_core MODULE cpp/src/bindings.cpp)
target_link_libraries(_core PRIVATE dwpdsim_core)

install(
    TARGETS _core
    LIBRARY DESTINATION dwpdsim
    RUNTIME DESTINATION dwpdsim
)

option(DWPDSIM_BUILD_TESTS "Build C++ integration tests" OFF)
if(DWPDSIM_BUILD_TESTS)
    enable_testing()
    add_executable(radix_tree_integration_test tests/cpp/radix_tree_integration_test.cpp)
    target_link_libraries(radix_tree_integration_test PRIVATE dwpdsim_core)
    add_test(NAME radix_tree_integration_test COMMAND radix_tree_integration_test)

    add_executable(simulator_integration_test tests/cpp/simulator_integration_test.cpp)
    target_link_libraries(simulator_integration_test PRIVATE dwpdsim_core)
    add_test(NAME simulator_integration_test COMMAND simulator_integration_test)
endif()
