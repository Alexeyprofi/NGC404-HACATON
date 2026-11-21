// Исправленная версия run для ИИ-помощника покупателя.
// Соответствует CONTRACT.md (v1.1) и LLM_API.md.
//
// Схема работы:
//   1. Одним вызовом Gemma извлекаются условия покупки (город, бюджет, категории,
//      ограничения по характеристикам) и при необходимости формулируются вопросы.
//   2. Всё остальное делает C++: проверка условий, фильтрация каталога, подбор комплектов
//      (перебор по одному товару на категорию), пересчёт цены и объяснение.
//      Текст объявлений в модель не передаётся, поэтому инструкции внутри объявлений
//      на результат не влияют.
//
// Сборка: как main.cpp (см. CMakeLists.txt). Чтобы собрать именно этот файл,
// замените в CMakeLists.txt `src/main.cpp` на `src/main_fixed.cpp`.

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include <CLI11/CLI11.hpp>
#include <nlohmann/json.hpp>
#include <dotenv.hpp>
#include <httplib.h>

#ifdef _WIN32
#include <windows.h>
#endif

using json = nlohmann::json;

namespace {

// ============================================================================
// Справочники (data/categories.json, data/cities.json; общие для учебных и проверочных данных)
// ============================================================================

struct Category { std::string id, name; };
struct City { std::string id, name; };

const std::vector<Category> kCategories = {
    {"table", "Стол"}, {"chair", "Кресло"}, {"lamp", "Лампа"}};

const std::vector<City> kCities = {
    {"taganrog", "Таганрог"}, {"rostov-na-donu", "Ростов-на-Дону"}, {"azov", "Азов"}};

enum class AttrType { Integer, Boolean };

struct AttrSpec {
    std::string key;
    std::string category;
    AttrType type;
    std::string label;
    std::string unit;
};

const std::vector<AttrSpec> kAttrs = {
    {"width_cm", "table", AttrType::Integer, "ширина", "см"},
    {"adjustable_height", "chair", AttrType::Boolean, "регулировка высоты", ""},
    {"color_temperature_k", "lamp", AttrType::Integer, "цветовая температура", "K"}};

constexpr size_t kMaxOptions = 3;
constexpr size_t kMaxDialogBytes = 24000;   // запас до лимита 32 768 байт на все тексты
constexpr size_t kMaxMessageBytes = 4000;

// ============================================================================
// Вспомогательные функции
// ============================================================================

// Нормализация для сравнения: ASCII в нижний регистр, кириллица А-Я/Ё -> а-я/ё, пробелы по краям.
std::string normalize(const std::string& s) {
    std::string out;
    size_t i = 0;
    while (i < s.size()) {
        unsigned char c = static_cast<unsigned char>(s[i]);
        if (c < 0x80) {
            out += static_cast<char>(std::tolower(c));
            ++i;
        } else if ((c & 0xE0) == 0xC0 && i + 1 < s.size()) {
            unsigned cp = ((c & 0x1Fu) << 6) | (static_cast<unsigned char>(s[i + 1]) & 0x3Fu);
            if (cp >= 0x410 && cp <= 0x42F) cp += 0x20;
            else if (cp == 0x401) cp = 0x451;
            out += static_cast<char>(0xC0 | (cp >> 6));
            out += static_cast<char>(0x80 | (cp & 0x3Fu));
            i += 2;
        } else {
            out += s[i];
            ++i;
        }
    }
    const char* ws = " \t\r\n";
    size_t b = out.find_first_not_of(ws);
    if (b == std::string::npos) return "";
    size_t e = out.find_last_not_of(ws);
    return out.substr(b, e - b + 1);
}

// Обрезка по границе UTF-8 символа.
std::string truncate_utf8(const std::string& s, size_t max_bytes) {
    if (s.size() <= max_bytes) return s;
    size_t cut = max_bytes;
    while (cut > 0 && (static_cast<unsigned char>(s[cut]) & 0xC0) == 0x80) --cut;
    return s.substr(0, cut);
}

std::string fmt_rub(long long v) {
    std::string s = std::to_string(v);
    for (int pos = static_cast<int>(s.size()) - 3; pos > 0; pos -= 3) s.insert(pos, " ");
    return s + " ₽";
}

std::string join(const std::vector<std::string>& items, const std::string& sep) {
    std::string out;
    for (const auto& it : items) {
        if (!out.empty()) out += sep;
        out += it;
    }
    return out;
}

const Category* find_category(const std::string& id) {
    for (const auto& c : kCategories) if (c.id == id) return &c;
    return nullptr;
}

std::string category_name(const std::string& id) {
    const Category* c = find_category(id);
    return c ? c->name : id;
}

std::string city_name(const std::string& id) {
    for (const auto& c : kCities) if (c.id == id) return c.name;
    return id;
}

const AttrSpec* find_attr(const std::string& key) {
    for (const auto& a : kAttrs) if (a.key == key) return &a;
    return nullptr;
}

// Сопоставление введённого значения (id или русское название) с id справочника.
std::optional<std::string> match_category(const std::string& raw) {
    std::string n = normalize(raw);
    for (const auto& c : kCategories) {
        if (n == c.id || n == normalize(c.name)) return c.id;
    }
    return std::nullopt;
}

std::optional<std::string> match_city(const std::string& raw) {
    std::string n = normalize(raw);
    for (const auto& c : kCities) {
        if (n == c.id || n == normalize(c.name)) return c.id;
    }
    return std::nullopt;
}

// Бюджет: неотрицательное целое число рублей (допускаются строки из цифр, например "15000").
std::optional<long long> parse_budget(const json& v) {
    constexpr long long kMax = 1'000'000'000'000LL;
    if (v.is_number_integer()) {
        long long x = v.get<long long>();
        if (x >= 0 && x <= kMax) return x;
        return std::nullopt;
    }
    if (v.is_number_float()) {
        double d = v.get<double>();
        if (std::isfinite(d) && d >= 0 && d == std::floor(d) && d <= static_cast<double>(kMax)) {
            return static_cast<long long>(d);
        }
        return std::nullopt;
    }
    if (v.is_string()) {
        std::string digits;
        for (char ch : v.get<std::string>()) if (ch != ' ') digits += ch;
        if (digits.empty() || digits.size() > 12) return std::nullopt;
        for (char ch : digits) if (!std::isdigit(static_cast<unsigned char>(ch))) return std::nullopt;
        return std::stoll(digits);
    }
    return std::nullopt;
}

bool str_field(const json& o, const char* key, std::string& out) {
    if (!o.contains(key) || !o.at(key).is_string()) return false;
    out = o.at(key).get<std::string>();
    return true;
}

bool flag_field(const json& o, const char* key) {
    return o.contains(key) && o.at(key).is_boolean() && o.at(key).get<bool>();
}

// ============================================================================
// Условия покупателя
// ============================================================================

struct Constraint {
    const AttrSpec* spec = nullptr;
    std::optional<double> min, max, exact;
    bool min_exclusive = false, max_exclusive = false;
    std::optional<bool> flag;
};

struct Slots {
    std::string city_id;                 // пусто, если город не задан
    bool city_invalid = false;           // город назван, но не найден в справочнике
    std::optional<long long> budget;     // nullopt, если бюджет не задан или некорректен
    std::vector<std::string> categories; // id категорий без повторов, в порядке назначения
    bool categories_invalid = false;     // названа неподдерживаемая категория
    std::vector<Constraint> constraints;
    std::map<std::string, std::string> question_texts;  // field -> текст вопроса от модели
};

Slots parse_slots(const json& args) {
    Slots s;

    if (args.contains("city") && args.at("city").is_string()) {
        std::string raw = args.at("city").get<std::string>();
        std::string n = normalize(raw);
        if (!n.empty() && n != "null") {
            if (auto id = match_city(raw)) s.city_id = *id;
            else s.city_invalid = true;
        }
    }

    if (args.contains("max_budget") && !args.at("max_budget").is_null()) {
        s.budget = parse_budget(args.at("max_budget"));
    }

    auto add_category = [&](const json& r) {
        if (!r.is_string()) { s.categories_invalid = true; return; }
        std::string raw = r.get<std::string>();
        std::string n = normalize(raw);
        if (n.empty() || n == "null") return;
        auto id = match_category(raw);
        if (!id) { s.categories_invalid = true; return; }
        if (std::find(s.categories.begin(), s.categories.end(), *id) == s.categories.end()) {
            s.categories.push_back(*id);
        }
    };
    if (args.contains("category")) {
        const json& c = args.at("category");
        if (c.is_array()) for (const auto& r : c) add_category(r);
        else if (c.is_string()) add_category(c);
    }

    // Ограничения по характеристикам. Они относятся только к запрошенным категориям.
    if (args.contains("attributes") && args.at("attributes").is_object()) {
        const json& attrs = args.at("attributes");
        for (auto it = attrs.begin(); it != attrs.end(); ++it) {
            const std::string key = it.key();
            const json& val = it.value();

            const AttrSpec* spec = find_attr(key);
            if (!spec) {
                std::fprintf(stderr, "[warn] Неизвестная характеристика игнорируется: %s\n", key.c_str());
                continue;
            }
            if (std::find(s.categories.begin(), s.categories.end(), spec->category) == s.categories.end()) {
                continue;
            }

            Constraint c;
            c.spec = spec;
            if (spec->type == AttrType::Boolean) {
                if (!val.is_boolean()) {
                    std::fprintf(stderr, "[warn] Некорректное значение для %s\n", key.c_str());
                    continue;
                }
                c.flag = val.get<bool>();
            } else if (val.is_number()) {
                c.exact = val.get<double>();
            } else if (val.is_object()) {
                if (val.contains("min") && val.at("min").is_number()) c.min = val.at("min").get<double>();
                if (val.contains("max") && val.at("max").is_number()) c.max = val.at("max").get<double>();
                c.min_exclusive = flag_field(val, "min_exclusive");
                c.max_exclusive = flag_field(val, "max_exclusive");
                if (!c.min && !c.max) continue;
            } else {
                std::fprintf(stderr, "[warn] Некорректное значение для %s\n", key.c_str());
                continue;
            }
            s.constraints.push_back(c);
        }
    }

    if (args.contains("questions") && args.at("questions").is_array()) {
        for (const auto& q : args.at("questions")) {
            std::string field, text;
            if (!q.is_object() || !str_field(q, "field", field) || !str_field(q, "text", text)) continue;
            if (normalize(text).empty() || s.question_texts.count(field)) continue;
            if (field == "city_id" || field == "budget_rub" || field == "categories") {
                s.question_texts[field] = text;
            }
        }
    }

    return s;
}

// ============================================================================
// Каталог
// ============================================================================

struct Ad {
    std::string id, category, title, description, city_id;
    long long price = 0;
    const json* attrs = nullptr;  // указатель на объект attributes внутри каталога
};

// Пропускает объявления, у которых нет обязательных полей или они некорректны.
std::vector<Ad> load_ads(const json& catalog) {
    std::vector<Ad> ads;
    std::map<std::string, bool> seen;
    for (const auto& it : catalog) {
        if (!it.is_object()) continue;
        Ad ad;
        if (!str_field(it, "id", ad.id) || ad.id.empty()) continue;
        if (!str_field(it, "category", ad.category)) continue;
        if (!str_field(it, "title", ad.title)) continue;
        if (!str_field(it, "description", ad.description)) continue;
        if (!str_field(it, "city_id", ad.city_id)) continue;
        if (!it.contains("price_rub") || !it.at("price_rub").is_number_integer()) continue;
        ad.price = it.at("price_rub").get<long long>();
        if (ad.price <= 0) continue;
        if (!it.contains("attributes") || !it.at("attributes").is_object()) continue;
        if (!find_category(ad.category)) continue;
        if (seen.count(ad.id)) continue;
        seen[ad.id] = true;
        ad.attrs = &it.at("attributes");
        ads.push_back(std::move(ad));
    }
    return ads;
}

bool item_matches(const json& attrs, const Constraint& c) {
    if (!attrs.contains(c.spec->key)) return false;  // неизвестное значение не подтверждает условие
    const json& v = attrs.at(c.spec->key);
    if (c.flag) return v.is_boolean() && v.get<bool>() == *c.flag;
    if (!v.is_number()) return false;
    double x = v.get<double>();
    if (c.exact && x != *c.exact) return false;
    if (c.min && (c.min_exclusive ? !(x > *c.min) : !(x >= *c.min))) return false;
    if (c.max && (c.max_exclusive ? !(x < *c.max) : !(x <= *c.max))) return false;
    return true;
}

bool constraints_ok(const Ad& ad, const std::vector<Constraint>& cs) {
    for (const auto& c : cs) {
        if (c.spec->category != ad.category) continue;
        if (!item_matches(*ad.attrs, c)) return false;
    }
    return true;
}

// ============================================================================
// Подбор комплектов: по одному товару из каждой категории, сумма <= бюджета
// ============================================================================

struct Option {
    std::vector<size_t> picks;  // индексы в списке объявлений
    long long total = 0;
};

class ComboSearch {
public:
    ComboSearch(const std::vector<Ad>& ads, const std::vector<std::vector<size_t>>& lists,
                long long budget, size_t max_results)
        : ads_(ads), lists_(lists), budget_(budget), max_(max_results) {}

    void run() {
        std::vector<size_t> chosen;
        dfs(0, 0, chosen);
    }

    const std::vector<Option>& results() const { return best_; }

private:
    const std::vector<Ad>& ads_;
    const std::vector<std::vector<size_t>>& lists_;  // отсортированы по цене по возрастанию
    long long budget_;
    size_t max_;
    std::vector<Option> best_;

    // Порядок: сначала дешевле, при равной цене — лексикографически по ID.
    bool better(const Option& a, const Option& b) const {
        if (a.total != b.total) return a.total < b.total;
        return std::lexicographical_compare(
            a.picks.begin(), a.picks.end(), b.picks.begin(), b.picks.end(),
            [this](size_t x, size_t y) { return ads_[x].id < ads_[y].id; });
    }

    void dfs(size_t depth, long long sum, std::vector<size_t>& chosen) {
        if (depth == lists_.size()) {
            offer(Option{chosen, sum});
            return;
        }
        for (size_t idx : lists_[depth]) {
            long long next = sum + ads_[idx].price;
            if (next > budget_) break;  // список отсортирован, дальше только дороже
            chosen.push_back(idx);
            dfs(depth + 1, next, chosen);
            chosen.pop_back();
        }
    }

    void offer(Option o) {
        if (best_.size() >= max_ && !better(o, best_.back())) return;
        best_.push_back(std::move(o));
        std::sort(best_.begin(), best_.end(),
                  [this](const Option& a, const Option& b) { return better(a, b); });
        if (best_.size() > max_) best_.pop_back();
    }
};

std::string attrs_phrase(const Ad& ad) {
    std::vector<std::string> parts;
    for (const auto& spec : kAttrs) {
        if (spec.category != ad.category || !ad.attrs->contains(spec.key)) continue;
        const json& v = ad.attrs->at(spec.key);
        if (spec.type == AttrType::Boolean) {
            if (v.is_boolean()) parts.push_back(spec.label + (v.get<bool>() ? " есть" : " нет"));
        } else if (v.is_number()) {
            std::string part = spec.label + " " + v.dump();
            if (!spec.unit.empty()) part += " " + spec.unit;
            parts.push_back(part);
        }
    }
    return join(parts, ", ");
}

std::string make_explanation(const std::vector<Ad>& ads, const Option& opt,
                             long long budget, const std::string& city) {
    std::vector<std::string> parts;
    for (size_t idx : opt.picks) {
        const Ad& ad = ads[idx];
        std::string part = category_name(ad.category) + ": «" + ad.title + "»";
        std::string attrs = attrs_phrase(ad);
        if (!attrs.empty()) part += " (" + attrs + ")";
        part += " — " + fmt_rub(ad.price);
        parts.push_back(part);
    }
    return join(parts, "; ") + ". Итого " + fmt_rub(opt.total) + " при бюджете " + fmt_rub(budget) +
           ", самовывоз в городе «" + city + "».";
}

json no_match(const std::string& message) {
    return {{"status", "no_match"}, {"message", message}};
}

json run_search(const Slots& s, const std::vector<Ad>& ads) {
    const long long budget = *s.budget;
    const std::string city = city_name(s.city_id);
    const std::string cats_text = join([&] {
        std::vector<std::string> names;
        for (const auto& c : s.categories) names.push_back(category_name(c));
        return names;
    }(), ", ");
    const std::string constraints_clause =
        s.constraints.empty() ? "" : " с учётом ограничений по характеристикам";

    // Кандидаты: город, категория, цена не выше бюджета, характеристики подходят.
    std::vector<std::vector<size_t>> lists(s.categories.size());
    for (size_t i = 0; i < ads.size(); ++i) {
        const Ad& ad = ads[i];
        if (ad.city_id != s.city_id || ad.price > budget) continue;
        auto pos = std::find(s.categories.begin(), s.categories.end(), ad.category);
        if (pos == s.categories.end()) continue;
        if (!constraints_ok(ad, s.constraints)) continue;
        lists[static_cast<size_t>(pos - s.categories.begin())].push_back(i);
    }

    for (size_t c = 0; c < lists.size(); ++c) {
        if (lists[c].empty()) {
            return no_match("В городе «" + city + "» нет товаров категории «" +
                            category_name(s.categories[c]) + "» в пределах бюджета " + fmt_rub(budget) +
                            constraints_clause + ". Попробуйте изменить бюджет, город или ограничения.");
        }
        std::sort(lists[c].begin(), lists[c].end(), [&](size_t a, size_t b) {
            if (ads[a].price != ads[b].price) return ads[a].price < ads[b].price;
            return ads[a].id < ads[b].id;
        });
    }

    ComboSearch search(ads, lists, budget, kMaxOptions);
    search.run();

    if (search.results().empty()) {
        long long min_total = 0;
        for (const auto& l : lists) min_total += ads[l.front()].price;
        return no_match("Собрать комплект (" + cats_text + ") в городе «" + city + "» в пределах " +
                        fmt_rub(budget) + constraints_clause + " не удалось. Самый дешёвый подходящий комплект стоит " +
                        fmt_rub(min_total) + ". Можно увеличить бюджет или смягчить ограничения.");
    }

    json options = json::array();
    for (const auto& opt : search.results()) {
        json ids = json::array();
        for (size_t idx : opt.picks) ids.push_back(ads[idx].id);
        options.push_back({
            {"listing_ids", ids},
            {"total_rub", opt.total},
            {"explanation", make_explanation(ads, opt, budget, city)}
        });
    }
    return {{"status", "ok"}, {"options", options}};
}

json build_clarify(const Slots& s) {
    json questions = json::array();

    // Если модель дала текст вопроса по этому полю, используем его (кроме случаев с неверным вводом).
    auto text_for = [&](const char* field, const std::string& fallback, bool trust_model) -> std::string {
        if (trust_model) {
            auto it = s.question_texts.find(field);
            if (it != s.question_texts.end()) return it->second;
        }
        return fallback;
    };

    std::vector<std::string> city_names, cat_names;
    for (const auto& c : kCities) city_names.push_back(c.name);
    for (const auto& c : kCategories) cat_names.push_back(normalize(c.name));

    if (s.city_id.empty()) {
        std::string fb = "В каком городе нужен самовывоз? Доступны: " + join(city_names, ", ") + ".";
        if (s.city_invalid) fb = "Такого города в списке нет. " + fb;
        questions.push_back({{"field", "city_id"},
                             {"text", text_for("city_id", fb, !s.city_invalid)}});
    }
    if (!s.budget) {
        questions.push_back({{"field", "budget_rub"},
                             {"text", text_for("budget_rub",
                                               "Какой общий бюджет в рублях вы готовы потратить на весь комплект?",
                                               true)}});
    }
    if (s.categories.empty() || s.categories_invalid) {
        std::string fb = "Какие товары нужны? Сейчас можно подобрать: " + join(cat_names, ", ") + ".";
        if (s.categories_invalid) fb = "Такой категории пока нет. " + fb;
        questions.push_back({{"field", "categories"},
                             {"text", text_for("categories", fb, !s.categories_invalid)}});
    }
    return {{"status", "clarify"}, {"questions", questions}};
}

// ============================================================================
// Запрос к модели
// ============================================================================

struct LlmConfig {
    std::string base_url;
    std::string api_key;
    std::string user_agent;
    std::string model;
};

const char* kSystemPrompt =
    "Ты извлекаешь условия покупки из диалога покупателя. "
    "Отвечай только одним JSON-объектом, без пояснений и Markdown.";

// Текст диалога: от новых реплик к старым, пока не превышен лимит размера.
std::string build_dialog(const json& messages) {
    std::vector<std::string> lines;
    size_t total = 0;
    for (size_t i = messages.size(); i-- > 0;) {
        const json& m = messages.at(i);
        std::string who = m.at("role").get<std::string>() == "user" ? "Покупатель" : "Помощник";
        std::string line = who + ": " + truncate_utf8(m.at("text").get<std::string>(), kMaxMessageBytes) + "\n";
        if (total + line.size() > kMaxDialogBytes) break;  // самые старые реплики отбрасываются
        total += line.size();
        lines.push_back(std::move(line));
    }
    std::string dialog;
    for (auto it = lines.rbegin(); it != lines.rend(); ++it) dialog += *it;
    return dialog;
}

std::string build_user_prompt(const std::string& dialog) {
    std::vector<std::string> cities, cats, attrs;
    for (const auto& c : kCities) cities.push_back(c.id + " — " + c.name);
    for (const auto& c : kCategories) {
        std::string line = "- " + c.id + " (" + c.name + ")";
        for (const auto& a : kAttrs) {
            if (a.category != c.id) continue;
            line += ": " + a.key + " — " +
                    (a.type == AttrType::Boolean ? std::string("true/false")
                                                 : std::string("целое число, ") + a.unit);
        }
        cats.push_back(line);
    }

    return
        "Города (id — название): " + join(cities, ", ") + ".\n"
        "Категории и характеристики:\n" + join(cats, "\n") + "\n\n"
        "Диалог (учитывай всю историю; новая реплика покупателя заменяет прежнее значение того же условия, "
        "остальные условия сохраняются):\n" + dialog + "\n"
        "Верни JSON такого вида:\n"
        "{\"func\": \"sort_print\" или \"clarify\", \"args\": {"
        "\"city\": \"id города или null\", "
        "\"max_budget\": целое число рублей или null, "
        "\"category\": [\"id категории\", ...], "
        "\"attributes\": {\"width_cm\": {\"min\": число, \"max\": число, \"min_exclusive\": false, \"max_exclusive\": true}, "
        "\"adjustable_height\": true, \"color_temperature_k\": 4000}, "
        "\"questions\": [{\"field\": \"city_id|budget_rub|categories\", \"text\": \"вопрос покупателю на русском\"}]"
        "}}\n"
        "Правила:\n"
        "- Не выдумывай значения: если условие не названо, ставь null или [].\n"
        "- Бюджет — общий верхний предел в рублях («15 тысяч» = 15000).\n"
        "- «до», «не более», «не шире», «не больше» — max с max_exclusive=false; «меньше», «уже», «ниже» — max_exclusive=true. "
        "Аналогично для min («от», «не менее» — inclusive; «больше», «шире» — exclusive).\n"
        "- «от X до Y» — min=X и max=Y, оба включены.\n"
        "- Ограничения ставь только для характеристик выбранных категорий.\n"
        "- func = \"sort_print\", если известны город, бюджет и хотя бы одна категория; иначе func = \"clarify\" "
        "и вопросы по каждому недостающему полю.\n";
}

json build_payload(const LlmConfig& cfg, const std::string& dialog) {
    return {
        {"model", cfg.model},
        {"messages", json::array({
            {{"role", "system"}, {"content", kSystemPrompt}},
            {{"role", "user"}, {"content", build_user_prompt(dialog)}}
        })},
        {"temperature", 0.0},
        {"max_tokens", 400},
        {"stream", false},
        {"reasoning_effort", "none"},
        {"response_format", {{"type", "json_object"}}}
    };
}

// Возвращает JSON-объект из ответа модели. Бросает исключение при ошибке сервиса.
json call_llm(const LlmConfig& cfg, const json& payload) {
    std::string body = payload.dump();
    if (body.size() > 65000) {
        throw std::runtime_error("запрос к модели превышает лимит 65536 байт");
    }

    std::string host = cfg.base_url;
    std::string prefix;
    size_t scheme = cfg.base_url.find("://");
    size_t slash = cfg.base_url.find('/', scheme == std::string::npos ? 0 : scheme + 3);
    if (slash != std::string::npos) {
        host = cfg.base_url.substr(0, slash);
        prefix = cfg.base_url.substr(slash);
    }
    while (!prefix.empty() && prefix.back() == '/') prefix.pop_back();
    std::string endpoint = prefix.find("/chat/completions") != std::string::npos
                               ? prefix
                               : prefix + "/chat/completions";

    httplib::Client cli(host);
    cli.set_connection_timeout(10);
    cli.set_read_timeout(90);  // по контракту ожидание модели до 90 секунд
#ifdef _WIN32
    cli.enable_server_certificate_verification(false);
#endif

    httplib::Headers headers = {
        {"Authorization", "Bearer " + cfg.api_key},
        {"User-Agent", cfg.user_agent}
    };

    // Повтор только при 429/503 и только один раз: общий лимит времени 120 секунд.
    for (int attempt = 1; attempt <= 2; ++attempt) {
        auto res = cli.Post(endpoint, headers, body, "application/json");
        if (!res) {
            throw std::runtime_error("сетевая ошибка: " + httplib::to_string(res.error()));
        }

        if (res->status == 200) {
            json resp = json::parse(res->body, nullptr, false);
            if (resp.is_discarded()) throw std::runtime_error("ответ шлюза не является JSON");
            std::string content = resp.at("choices").at(0).at("message").at("content").get<std::string>();

            size_t first = content.find('{');
            size_t last = content.rfind('}');
            if (first == std::string::npos || last == std::string::npos || last < first) {
                throw std::runtime_error("ответ модели не содержит JSON");
            }
            json parsed = json::parse(content.substr(first, last - first + 1), nullptr, false);
            if (parsed.is_discarded() || !parsed.is_object()) {
                throw std::runtime_error("ответ модели не является JSON-объектом");
            }
            return parsed;
        }

        if ((res->status == 429 || res->status == 503) && attempt == 1) {
            int wait_sec = 3;
            if (res->has_header("Retry-After")) {
                try { wait_sec = std::clamp(std::stoi(res->get_header_value("Retry-After")), 1, 5); }
                catch (...) {}
            }
            std::fprintf(stderr, "[gateway retry] HTTP %d, ждём %d сек...\n", res->status, wait_sec);
            std::this_thread::sleep_for(std::chrono::seconds(wait_sec));
            continue;
        }

        throw std::runtime_error("HTTP " + std::to_string(res->status) + ": " + res->body);
    }
    throw std::runtime_error("исчерпаны попытки обращения к модели");
}

// ============================================================================
// Обработка запроса
// ============================================================================

std::string role_of(const json& m) { return m.at("role").get<std::string>(); }

// Проверка структуры по CONTRACT.md, раздел 3.
void validate_request(const json& req) {
    if (!req.is_object() || !req.contains("messages") || !req.at("messages").is_array() ||
        req.at("messages").empty()) {
        throw std::runtime_error("запрос должен содержать непустой массив messages");
    }
    const json& msgs = req.at("messages");
    for (size_t i = 0; i < msgs.size(); ++i) {
        const json& m = msgs.at(i);
        if (!m.is_object() || !m.contains("role") || !m.at("role").is_string() ||
            !m.contains("text") || !m.at("text").is_string() || m.at("text").get<std::string>().empty()) {
            throw std::runtime_error("некорректное сообщение в messages");
        }
        std::string role = role_of(m);
        std::string expected = (i % 2 == 0) ? "user" : "assistant";
        if (role != expected) throw std::runtime_error("роли сообщений должны чередоваться, начиная с user");
    }
    if (role_of(msgs.back()) != "user") throw std::runtime_error("последнее сообщение должно быть от user");
}

json process_request(const json& request, const std::vector<Ad>& ads, const LlmConfig& cfg) {
    const json& messages = request.at("messages");
    std::string dialog = build_dialog(messages);

    json llm_out = call_llm(cfg, build_payload(cfg, dialog));
    json args = (llm_out.contains("args") && llm_out.at("args").is_object()) ? llm_out.at("args") : json::object();
    std::fprintf(stderr, "[L1] func: %s | args: %s\n",
                 llm_out.value("func", std::string("?")).c_str(), args.dump().c_str());

    Slots s = parse_slots(args);

    bool complete = !s.city_id.empty() && s.budget.has_value() &&
                    !s.categories.empty() && !s.categories_invalid;
    if (complete) return run_search(s, ads);
    return build_clarify(s);
}

void interactive_mode(const std::vector<Ad>& ads, const LlmConfig& cfg) {
    std::fprintf(stderr, "[log] Интерактивный режим. Команды: 'exit' — выход, 'clear' — очистить диалог\n\n");
    json history = json::array();
    while (true) {
        std::cout << "Вы > " << std::flush;
        std::string input;
        if (!std::getline(std::cin, input)) break;
        if (input.empty()) continue;
        if (input == "exit") break;
        if (input == "clear") {
            history = json::array();
            std::fprintf(stderr, "[log] История диалога сброшена.\n\n");
            continue;
        }

        history.push_back({{"role", "user"}, {"text", input}});
        try {
            json request = {{"messages", history}};
            json response = process_request(request, ads, cfg);
            std::string status = response.at("status").get<std::string>();
            std::cout << "\nПомощник [" << status << "] >\n";

            std::string assistant_text;
            if (status == "clarify") {
                for (const auto& q : response.at("questions")) {
                    std::cout << "  - " << q.at("text").get<std::string>() << "\n";
                    assistant_text += q.at("text").get<std::string>() + " ";
                }
            } else if (status == "ok") {
                size_t n = 0;
                for (const auto& opt : response.at("options")) {
                    ++n;
                    std::cout << "  Вариант #" << n << " — " << fmt_rub(opt.at("total_rub").get<long long>()) << "\n"
                              << "  ID товаров: " << opt.at("listing_ids").dump() << "\n"
                              << "  Описание: " << opt.at("explanation").get<std::string>() << "\n";
                    assistant_text += opt.at("explanation").get<std::string>() + " ";
                }
            } else {
                assistant_text = response.at("message").get<std::string>();
                std::cout << "  " << assistant_text << "\n";
            }
            history.push_back({{"role", "assistant"}, {"text", assistant_text}});
        } catch (const std::exception& e) {
            std::fprintf(stderr, "[error] %s\n", e.what());
            history.erase(history.size() - 1);  // убираем реплику, на которую не получен ответ
        }
        std::cout << "\n";
    }
}

}  // namespace

int main(int argc, char** argv) {
#ifdef _WIN32
    SetConsoleOutputCP(CP_UTF8);
    SetConsoleCP(CP_UTF8);
#endif

    CLI::App app{"Avito AI Assistant: подбор комплектов для покупателя"};
    std::string catalog_path;
    bool interactive = false;
    app.add_option("-c,--catalog", catalog_path, "Путь к catalog.json")->required();
    app.add_flag("-i,--interactive", interactive, "Интерактивный чат в консоли");
    try {
        app.parse(argc, argv);
    } catch (const CLI::ParseError& e) {
        return app.exit(e);
    }

    json catalog;
    try {
        std::ifstream f(catalog_path, std::ios::binary);
        if (!f) throw std::runtime_error("не удалось открыть файл " + catalog_path);
        catalog = json::parse(f);
        if (!catalog.is_array()) throw std::runtime_error("каталог должен быть массивом объявлений");
    } catch (const std::exception& e) {
        std::fprintf(stderr, "[error] каталог: %s\n", e.what());
        return 1;
    }
    std::vector<Ad> ads = load_ads(catalog);
    std::fprintf(stderr, "[log] Объявлений в каталоге: %zu (пригодных: %zu)\n", catalog.size(), ads.size());

    static auto dotenv = env_utils::load_dotenv(argv[0]);
    LlmConfig cfg;
    cfg.base_url = env_utils::get_var("LLM_BASE_URL", dotenv,
                                      "https://sfedu-hackathon-ai-gateway.sfedu-hackathon-ai-gateway.workers.dev/v1");
    cfg.api_key = env_utils::get_var("LLM_API_KEY", dotenv);
    cfg.user_agent = env_utils::get_var("LLM_USER_AGENT", dotenv, "hackathon-cpp-client/1.0");
    cfg.model = env_utils::get_var("LLM_MODEL", dotenv, "google/gemma-4-31B-it");
    if (cfg.api_key.empty()) {
        std::fprintf(stderr, "[fatal] LLM_API_KEY не задан\n");
        return 1;
    }

    if (interactive) {
        interactive_mode(ads, cfg);
        return 0;
    }

    try {
        std::string input((std::istreambuf_iterator<char>(std::cin)), std::istreambuf_iterator<char>());
        json request = json::parse(input);
        validate_request(request);
        json response = process_request(request, ads, cfg);
        std::cout << response.dump(2) << '\n';
    } catch (const std::exception& e) {
        std::fprintf(stderr, "[fatal] %s\n", e.what());
        return 1;
    }
    return 0;
}
