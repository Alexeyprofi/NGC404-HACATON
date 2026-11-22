cmake_minimum_required(VERSION 3.21)
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
    target_link_options(run PRIVATE -static-libgcc -static-libstdc++ -Wl,-Bstatic -lwinpthread -Wl,-Bdynamic)
    target_link_libraries(run PRIVATE OpenSSL::SSL OpenSSL::Crypto ws2_32 crypt32)

    get_filename_component(MINGW_BIN_DIR "${CMAKE_CXX_COMPILER}" DIRECTORY)
    file(GLOB RUNTIME_DLLS 
        "${MINGW_BIN_DIR}/libssl-*.dll" 
        "${MINGW_BIN_DIR}/libcrypto-*.dll"
        "${MINGW_BIN_DIR}/libwinpthread-*.dll"
    )

    if(RUNTIME_DLLS)
        add_custom_command(TARGET run POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_if_different ${RUNTIME_DLLS} $<TARGET_FILE_DIR:run>
            COMMENT "Копирование необходимых DLL в папку сборки..."
        )
    endif()
else()
    find_package(Threads REQUIRED)
    target_link_libraries(run PRIVATE OpenSSL::SSL OpenSSL::Crypto Threads::Threads)
endif()