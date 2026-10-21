cmake_minimum_required(VERSION 3.16)
project(diagnostic_fire_replay LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
if(NOT DEFINED SP_ROOT)
  message(FATAL_ERROR "SP_ROOT must name the frozen algorithm checkout")
endif()
find_package(Eigen3 REQUIRED)
find_package(OpenCV REQUIRED COMPONENTS core)
find_package(yaml-cpp REQUIRED)
find_package(fmt REQUIRED)
find_package(spdlog REQUIRED)
add_executable(diagnostic_fire_replay main.cpp
  ${SP_ROOT}/tasks/auto_aim/shooter.cpp ${SP_ROOT}/tools/math_tools.cpp)
target_include_directories(diagnostic_fire_replay PRIVATE ${SP_ROOT}
  ${SP_ROOT}/io/serial/include ${OpenCV_INCLUDE_DIRS})
# Only shoot_current is invoked. Drop the hardware/legacy shooting method and
# its unused links; compile the original source without copying its policy.
target_compile_options(diagnostic_fire_replay PRIVATE -O2 -ffunction-sections -fdata-sections)
target_link_options(diagnostic_fire_replay PRIVATE -Wl,--gc-sections)
target_link_libraries(diagnostic_fire_replay PRIVATE Eigen3::Eigen ${OpenCV_LIBS} yaml-cpp fmt::fmt spdlog::spdlog)
