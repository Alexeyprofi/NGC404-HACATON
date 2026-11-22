# Компиляция
	
	mkdir build
	cd build
	cmake ..
	cmake --build .
	

# Запуск
## Основного кода в обычном режиме:
./run --catalog полный/путь/к/каталогу.json < запрос.json > ответ.json 
## Основного кода в интерактивном режиме:
./run -i --catalog полный/путь/к/каталогу.json
## Веб-интерфейса (доступен по 127.0.0.1:8080)
cd web
python server.py --catalog полный/путь/к/каталогу.json
