# Компиляция
	`` bash
	mkdir build
	cd build
	cmake ..
	cmake --build .
	``

# Запуск
	## Основного кода в обычном режиме:
	`` bash
	cd build
	./run --catalog полный/путь/к/каталогу.json < запрос.json > ответ.json 
	``
	## Основного кода в интерактивном режиме:
	`` bash
	cd build
	./run -i --catalog полный/путь/к/каталогу.json
	``
	## Веб-интерфейса (доступен по 127.0.0.1:8080)
	`` bash
	cd web
	python server.py --catalog полный/путь/к/каталогу.json
	``
