ИИ-помощник покупателя (C++)
Готовые бинарные файлы уже скомпилированы и готовы к запуску без предварительной сборки:
- ./run (в корне репозитория) — готовый исполняемый файл для Linux (Ubuntu 24.04).
- build/run.exe — готовый исполняемый файл для Windows (в комплекте со всеми необходимыми .dll).




Запуск на Linux (основной режим проверки)
В корне репозитория находится готовый скомпилированный исполняемый файл run.
    Переменные окружения:
        Программа считывает настройки из системного окружения (приоритет) или из файла .env в рабочей директории:
        - LLM_BASE_URL
        - LLM_MODEL
        - LLM_API_KEY
        - LLM_USER_AGENT

    Команды запуска:
        Пакетный режим (автопроверка stdin -> stdout):
        ./run --catalog data/catalog.json < examples/01-selection/request.json > response.json

Интерактивный режим (живой диалог с жюри):
./run --catalog data/catalog.json --interactive


Зависимости: 
- справочники categories.json и cities.json автоматически подгружаются из папки ./data/ либо из директории, в которой расположен переданный catalog.json.
- рядом с программой run должен лежать prompt.txt и system_l2.txt






Локальный запуск на Windows
Для успешного запуска уже скомпилированного run.exe из директории build/ рядом с исполняемым файлом должны находиться:
- .env — файл с конфигурациями (если переменные не заданы в системе)
- data — папка с файлами catalog.json, categories.json, cities.json
- .dll файлы — libcrypto-3-x64.dll, libssl-3-x64.dll, libwinpthread-1.dll (уже лежат в папке build/ и копируются автоматически при пересборке через CMake)

Интерактивный режим (живой диалог с жюри):
run.exe --catalog data\catalog.json --interactive




Инструкция по самостоятельной пересборке (при необходимости)
Кросс-сборка бинарника для Linux из Windows через Docker:
    docker build -f Dockerfile.build -t avito-builder .

Команда для PowerShell:
docker run --rm -v "${PWD}:/out" avito-builder cp /app/build_linux/run /out/run
Команда для Cmd:
docker run --rm -v "%cd%:/out" avito-builder cp /app/build_linux/run /out/run










Локальная сборка через CMake:
cd build
cmake ..
cmake --build .
После компиляции запускать исполняемый файл run (или run.exe) в папке build.