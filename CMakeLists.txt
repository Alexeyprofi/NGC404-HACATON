cmake_minimum_required(VERSION 3.10.0)
project(Avito_Ai_Assistant_C++ VERSION 0.1.0 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(OpenSSL REQUIRED)


add_executable(run src/main.cpp)
target_include_directories(run PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/src/libs")
target_compile_definitions(run PRIVATE CPPHTTPLIB_OPENSSL_SUPPORT)

if(UNIX)
    target_link_options(run PRIVATE -static-libstdc++ -static-libgcc)
endif()

if(WIN32)
    target_compile_definitions(run PRIVATE 
        _WIN32_WINNT=0x0A00 
        WINVER=0x0A00
        CPPHTTPLIB_OPENSSL_SUPPORT
    )
    target_link_libraries(run PRIVATE OpenSSL::SSL OpenSSL::Crypto ws2_32 crypt32)
else()
    find_package(Threads REQUIRED)
    target_link_libraries(run PRIVATE OpenSSL::SSL OpenSSL::Crypto Threads::Threads)
endif()