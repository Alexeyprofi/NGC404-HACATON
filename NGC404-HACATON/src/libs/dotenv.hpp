#pragma once

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <string>
#include <unordered_map>
#include <vector>

namespace fs = std::filesystem;

namespace env_utils {
    // Убираем пробелы, табы, переводы строк (\r, \n) и внешние кавычки
    inline std::string trim_and_unquote(std::string s) {
        size_t start = s.find_first_not_of(" \t\r\n");
        if (start == std::string::npos) return "";
        size_t end = s.find_last_not_of(" \t\r\n");
        s = s.substr(start, end - start + 1);

        // Снимаем одинарные или двойные кавычки, если значение обёрнуто в них
        if (s.size() >= 2 &&
            ((s.front() == '"' && s.back() == '"') ||
            (s.front() == '\'' && s.back() == '\''))) {
            s = s.substr(1, s.size() - 2);
        }
        return s;
    }

    // Парсим конкретный файл .env в словарь ключ -> значение
    inline std::unordered_map<std::string, std::string> parse_dotenv_file(const fs::path& file_path) {
        std::unordered_map<std::string, std::string> result;
        std::ifstream file(file_path, std::ios::binary);
        if (!file.is_open()) {
            return result;
        }

        std::string line;
        while (std::getline(file, line)) {
            size_t first_char = line.find_first_not_of(" \t\r\n");
            // Пропускаем пустые строки и комментарии (#)
            if (first_char == std::string::npos || line[first_char] == '#') {
                continue;
            }

            // Поддержка синтаксиса "export KEY=VAL"
            if (line.compare(first_char, 7, "export ") == 0) {
                first_char += 7;
            }

            size_t eq_pos = line.find('=', first_char);
            if (eq_pos == std::string::npos) {
                continue;
            }

            std::string key = trim_and_unquote(line.substr(first_char, eq_pos - first_char));
            std::string val = trim_and_unquote(line.substr(eq_pos + 1));

            if (!key.empty()) {
                result[key] = val;
            }
        }
        return result;
    }

    // Загружаем .env (ищем в текущей папке, рядом с бинарником и в папке build/)
    inline std::unordered_map<std::string, std::string> load_dotenv(const char* argv0 = nullptr) {
        std::vector<fs::path> search_paths = {
            fs::current_path() / ".env"
        };

        if (argv0 != nullptr) {
            std::error_code ec;
            fs::path exe_dir = fs::weakly_canonical(fs::u8path(argv0), ec).parent_path();
            if (!exe_dir.empty()) {
                search_paths.push_back(exe_dir / ".env");
                search_paths.push_back(exe_dir.parent_path() / ".env");
            }
        }

        for (const auto& p : search_paths) {
            std::error_code ec;
            if (fs::exists(p, ec) && fs::is_regular_file(p, ec)) {
                return parse_dotenv_file(p);
            }
        }
        return {};
    }

    // Получаем переменную: 1) Системное окружение (приоритет жюри) -> 2) .env -> 3) default
    inline std::string get_var(
        const std::string& key,
        const std::unordered_map<std::string, std::string>& dotenv_map,
        const std::string& default_val = ""
    ) {
        const char* sys_val = std::getenv(key.c_str());
        if (sys_val != nullptr && sys_val[0] != '\0') {
            return std::string(sys_val);
        }

        auto it = dotenv_map.find(key);
        if (it != dotenv_map.end() && !it->second.empty()) {
            return it->second;
        }

        return default_val;
    }

} // namespace env_utils