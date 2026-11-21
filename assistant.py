#!/usr/bin/env python3
"""ИИ-помощник покупателя (хакатон ЮФУ, Avito Start).

Разделение труда:
  * Модель Gemma (extract_with_llm) ТОЛЬКО понимает человеческий язык: превращает диалог
    в структурированные условия (категории, город, бюджет, ограничения, пожелания о цене).
  * Обычный код проверяет эти условия, сам фильтрует каталог, считает цены, собирает и
    ранжирует комплекты и пишет ответ по контракту.
  * Заголовки и описания объявлений в модель не передаются и в ответ не копируются
    (защита от инструкций внутри объявлений). Цена, город и характеристики берутся
    только из структурных полей каталога.
"""
import argparse
import copy
import itertools
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

TOTAL_TIMEOUT_SEC = int(os.environ.get("RUN_TOTAL_TIMEOUT", "100"))  # по контракту на весь запуск 120 секунд
HERE = os.path.dirname(os.path.realpath(__file__))

# Запасные значения на случай, если data/cities.json или data/categories.json не найдены.
DEFAULT_CITIES = {"taganrog": "Таганрог", "rostov-na-donu": "Ростов-на-Дону", "azov": "Азов"}
DEFAULT_CATEGORIES = {
    "table": {"name": "стол", "attrs": {"width_cm": {"kind": "number", "label": "ширина", "unit": "см"}}},
    "chair": {"name": "кресло", "attrs": {"adjustable_height": {"kind": "bool", "label": "регулировка высоты", "unit": ""}}},
    "lamp": {"name": "лампа", "attrs": {"color_temperature_k": {"kind": "number", "label": "цветовая температура", "unit": "К"}}},
}
# Человеческие названия и подсказки для модели по известным характеристикам.
KNOWN_ATTRS = {
    "width_cm": ("ширина", "см", "ширина стола в сантиметрах; 1 метр = 100 см"),
    "adjustable_height": ("регулировка высоты", "", "true - регулировка высоты нужна, false - её не должно быть"),
    "color_temperature_k": ("цветовая температура", "К", "кельвины; слова вроде «тёплый свет» без числа в число не превращай"),
}
UNIT_RU = {"cm": "см", "K": "К"}

REF = {"cities": dict(DEFAULT_CITIES), "categories": copy.deepcopy(DEFAULT_CATEGORIES)}

OPS = {
    "<=": lambda a, b: a <= b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    ">": lambda a, b: a > b,
    "==": lambda a, b: a == b,
}
OP_TEXT = {"<=": "не более", ">=": "не менее", "<": "меньше", ">": "больше", "==": "ровно"}

PROMPT_TEMPLATE = """Ты разбираешь запрос покупателя. Тебе дают диалог покупателя с помощником.
Верни ТОЛЬКО один JSON-объект, без пояснений и без Markdown.

Поля объекта:
- "categories": список id категорий, которые покупатель хочет купить, без повторов. Допустимые категории: @CATS@.
- "unsupported_categories": список названий других товаров, которые покупатель просит, в именительном падеже и единственном числе (например "стул", "шкаф"). Если таких нет - [].
- "city_id": один из id: @CITIES@. Если город не назван - null.
- "unsupported_city": название города в именительном падеже (например "Москва"), если назван какой-то другой город, иначе null.
- "budget_rub": целое число рублей - общий бюджет на весь комплект. Если не назван - null. «15 тысяч» и «15к» = 15000.
- "constraints": список ограничений на характеристики. Элемент: {"category": ..., "attribute": ..., "op": ..., "value": ...}.
  Допустимые характеристики:
@ATTRS@
  op: "<=" (до, не более, не шире), ">=" (от, не менее), "<" (меньше, уже), ">" (больше, шире), "==" (ровно).
  Для логических характеристик: op "==" и value true или false.
  Интервал «от A до B» - это два ограничения: ">=" A и "<=" B.
- "ambiguous_fields": список из значений "budget_rub", "city_id", "categories" - полей, для которых из сообщений нельзя однозначно выбрать одно значение. Например: названо несколько городов («Таганрог или Азов»); вместо одного общего бюджета названы лимиты на отдельные товары; названы противоречивые бюджеты. Для такого поля поставь null (для categories - пустой список). Если неоднозначностей нет - [].
- "price_preference": "cheapest" (самые дешёвые, подешевле, недорого), "middle" (средние по цене, что-нибудь среднее) или "expensive" (самые дорогие, подороже, премиум) - пожелание о цене комплекта в целом, иначе null. Это пожелание, а не бюджет: слова «подешевле» и «подороже» НЕ меняют budget_rub и остальные условия.
- "category_price_preference": объект {id категории: "cheaper" или "pricier"} для пожеланий вроде «кресло подороже, стол подешевле». Иначе {}.
- "soft_preferences": список пожеланий, которые нельзя выразить характеристиками выше: цвет, материал, удобство, состояние, стиль и т. п. Элемент: {"text": короткая формулировка из 1-3 слов, "keywords": 1-3 корня слов строчными буквами для поиска по тексту объявлений}. Например {"text":"белый","keywords":["бел"]}. Если таких пожеланий нет - [].
- "quantities": объект {id категории: число} ТОЛЬКО для категорий, где покупатель просит больше одного товара. Иначе {}.

Правила:
1. Учитывай весь диалог. Если покупатель позже явно изменил условие, бери новое значение, остальные условия сохраняй.
2. Короткий ответ покупателя на вопрос помощника («15 тысяч», «Ростов») относится к этому вопросу.
3. Ничего не выдумывай и не подставляй значения по умолчанию: если условие не названо, ставь null, [] или {}.
4. Пожелание о цене и пожелания из soft_preferences, названные ранее, сохраняй, пока покупатель их не отменил.

Пример ответа: {"categories":["table","chair","lamp"],"unsupported_categories":[],"city_id":"taganrog","unsupported_city":null,"budget_rub":15000,"constraints":[{"category":"table","attribute":"width_cm","op":"<=","value":100}],"ambiguous_fields":[],"price_preference":null,"category_price_preference":{},"soft_preferences":[],"quantities":{}}"""


class LLMError(Exception):
    pass


def log(msg):
    try:
        sys.stderr.buffer.write((str(msg) + "\n").encode("utf-8", "replace"))
        sys.stderr.buffer.flush()
    except Exception:
        pass


# ---------------------------------------------------------------- .env и справочники

def load_env():
    """Читает файл .env рядом с программой. Уже заданные переменные не трогает."""
    path = os.path.join(HERE, ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:]
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def load_reference(base=None):
    """Города и категории читаются из data/cities.json и data/categories.json (рядом с программой).
    Если файлов нет или они повреждены, используются встроенные значения."""
    base = base or os.path.join(HERE, "data")
    cities = dict(DEFAULT_CITIES)
    cats = copy.deepcopy(DEFAULT_CATEGORIES)
    path = os.path.join(base, "cities.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8-sig") as f:
                raw = json.load(f)
            found = {str(x["id"]): str(x["name"]) for x in raw
                     if isinstance(x, dict) and x.get("id") and x.get("name")}
            if found:
                cities = found
        except Exception as e:  # noqa
            log("cities.json не прочитан, беру встроенный список: %s" % e)
    path = os.path.join(base, "categories.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8-sig") as f:
                raw = json.load(f)
            found = {}
            for x in raw:
                attrs = {}
                for key, spec in (x.get("attributes") or {}).items():
                    typ = spec.get("type")
                    kind = "bool" if typ == "boolean" else "number" if typ in ("integer", "number") else None
                    if kind is None:
                        continue
                    if key in KNOWN_ATTRS:
                        label, unit = KNOWN_ATTRS[key][0], KNOWN_ATTRS[key][1]
                    else:
                        unit = UNIT_RU.get(spec.get("unit"), spec.get("unit") or "")
                        label = key
                    attrs[key] = {"kind": kind, "label": label, "unit": unit}
                found[str(x["id"])] = {"name": str(x.get("name") or x["id"]).lower(), "attrs": attrs}
            if found:
                cats = found
        except Exception as e:  # noqa
            log("categories.json не прочитан, беру встроенный список: %s" % e)
    REF["cities"], REF["categories"] = cities, cats


def build_system_prompt():
    cats = ", ".join('"%s" (%s)' % (cid, c["name"]) for cid, c in REF["categories"].items())
    cities = ", ".join('"%s" (%s)' % (cid, name) for cid, name in REF["cities"].items())
    lines = []
    for cid, c in REF["categories"].items():
        for key, a in c["attrs"].items():
            typ = "true/false" if a["kind"] == "bool" else "число" + (", " + a["unit"] if a["unit"] else "")
            hint = KNOWN_ATTRS.get(key, ("", "", ""))[2]
            lines.append('    - category "%s" (%s), attribute "%s": %s%s' % (cid, c["name"], key, typ, "; " + hint if hint else ""))
    return PROMPT_TEMPLATE.replace("@CATS@", cats).replace("@CITIES@", cities).replace("@ATTRS@", "\n".join(lines))


# ---------------------------------------------------------------- модель

def call_llm(messages, deadline):
    base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    key = os.environ.get("LLM_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "google/gemma-4-31B-it")
    agent = os.environ.get("LLM_USER_AGENT", "buyer-assistant/1.0")
    if not base or not key:
        raise LLMError("Не заданы LLM_BASE_URL / LLM_API_KEY (см. файл .env)")
    body = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": 500,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    headers = {
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "User-Agent": agent,
    }
    last_err = "неизвестная ошибка"
    for attempt in range(1, 4):
        timeout = min(85, deadline - time.time())
        if timeout < 5:
            break
        req = urllib.request.Request(base + "/chat/completions", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            try:
                text = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                text = ""
            last_err = "HTTP %s %s" % (e.code, text)
            if e.code in (400, 401, 403, 413, 415):
                raise LLMError("Модель отклонила запрос: " + last_err)
            wait = 3 * attempt
            try:
                wait = max(wait, int(e.headers.get("Retry-After", 0)))
            except Exception:
                pass
            log("Модель: %s, повтор через %s с" % (last_err, wait))
            time.sleep(max(0, min(wait, deadline - time.time() - 6)))
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError,
                json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
            last_err = "%s: %s" % (type(e).__name__, e)
            log("Модель: " + last_err)
            time.sleep(min(2, max(0, deadline - time.time() - 6)))
    raise LLMError("Модель недоступна: " + last_err)


def parse_json_object(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("в ответе модели нет JSON")
    obj = json.loads(text[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("ответ модели - не объект")
    return obj


def messages_to_transcript(messages):
    lines = []
    for m in messages:
        who = "Покупатель" if m["role"] == "user" else "Помощник"
        lines.append("%s: %s" % (who, m["text"]))
    return "Диалог:\n" + "\n".join(lines) + "\n\nВерни JSON с условиями."


def extract_with_llm(messages, deadline):
    llm_messages = [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": messages_to_transcript(messages)},
    ]
    last = None
    first = None
    for attempt in range(2):
        content = call_llm(llm_messages, deadline)
        try:
            ex = normalize_extraction(parse_json_object(content))
        except (ValueError, json.JSONDecodeError) as e:
            last = e
            log("Не удалось разобрать ответ модели: %s | %r" % (e, content[:200]))
            continue
        if ex["dropped_constraints"] and attempt == 0:
            log("Часть ограничений не распознана, повторяю запрос к модели")
            first = ex
            continue
        return ex
    if first is not None:
        return first
    raise LLMError("Модель вернула некорректный JSON: %s" % last)


# ---------------------------------------------------------------- нормализация

def to_int(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v == int(v) else None
    if isinstance(v, str):
        digits = "".join(ch for ch in v if ch.isdigit())
        return int(digits) if digits else None
    return None


def to_number(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            return float(v.replace(",", ".").replace(" ", ""))
        except ValueError:
            return None
    return None


def to_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    return None


def clean_text(s, limit):
    """Только буквы, цифры, пробелы и дефис: безопасный короткий текст для вывода."""
    s = "".join(ch for ch in str(s) if ch.isalnum() or ch in " -")
    return " ".join(s.split())[:limit]


def normalize_extraction(raw):
    cats_ref, cities_ref = REF["categories"], REF["cities"]
    cats = []
    for c in raw.get("categories") or []:
        if isinstance(c, str) and c in cats_ref and c not in cats:
            cats.append(c)
    unsupported = [str(x) for x in (raw.get("unsupported_categories") or []) if x]

    city = raw.get("city_id")
    unsupported_city = raw.get("unsupported_city")
    if isinstance(city, str) and city not in cities_ref:
        unsupported_city = unsupported_city or city
        city = None
    if not isinstance(city, str):
        city = None

    budget = to_int(raw.get("budget_rub"))
    if budget is not None and budget < 0:
        budget = None

    ambiguous = []
    if isinstance(raw.get("ambiguous_fields"), list):
        ambiguous = [f for f in raw["ambiguous_fields"] if f in ("budget_rub", "city_id", "categories")]
    if "city_id" in ambiguous:
        city, unsupported_city = None, None
    if "budget_rub" in ambiguous:
        budget = None
    if "categories" in ambiguous:
        cats = []

    constraints = []
    dropped = 0
    for c in raw.get("constraints") or []:
        if not isinstance(c, dict):
            dropped += 1
            continue
        cat, attr, op = c.get("category"), c.get("attribute"), c.get("op")
        spec = cats_ref.get(cat, {}).get("attrs", {}).get(attr) if isinstance(cat, str) and isinstance(attr, str) else None
        if spec is None or op not in OPS:
            dropped += 1
            continue
        if spec["kind"] == "bool":
            value = to_bool(c.get("value"))
            if value is None or op != "==":
                dropped += 1
                continue
        else:
            value = to_number(c.get("value"))
            if value is None:
                dropped += 1
                continue
        constraints.append({"category": cat, "attribute": attr, "op": op, "value": value})

    price_pref = raw.get("price_preference")
    if price_pref not in ("cheapest", "middle", "expensive"):
        price_pref = None

    cat_pref = {}
    if isinstance(raw.get("category_price_preference"), dict):
        for k, v in raw["category_price_preference"].items():
            if k in cats and v in ("cheaper", "pricier"):
                cat_pref[k] = v

    soft = []
    if isinstance(raw.get("soft_preferences"), list):
        for p in raw["soft_preferences"][:5]:
            if not isinstance(p, dict):
                continue
            text = clean_text(p.get("text", ""), 40)
            if not text:
                continue
            kws = []
            for k in (p.get("keywords") or [])[:4] if isinstance(p.get("keywords"), list) else []:
                k = clean_text(k, 20).lower()
                if len(k) >= 3:
                    kws.append(k)
            soft.append({"text": text, "keywords": kws})

    quantities = {}
    if isinstance(raw.get("quantities"), dict):
        for k, v in raw["quantities"].items():
            n = to_int(v)
            if k in cats and n is not None and 2 <= n <= 20:
                quantities[k] = n

    return {
        "categories": cats,
        "unsupported_categories": unsupported,
        "city_id": city,
        "unsupported_city": unsupported_city if isinstance(unsupported_city, str) and unsupported_city else None,
        "budget_rub": budget,
        "constraints": constraints,
        "dropped_constraints": dropped,
        "ambiguous": ambiguous,
        "price_preference": price_pref,
        "category_price_preference": cat_pref,
        "soft_preferences": soft,
        "quantities": quantities,
    }


# ---------------------------------------------------------------- каталог, фильтры, комбинации

def load_catalog(path):
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    items = []
    for it in data:
        if not isinstance(it, dict):
            continue
        price = it.get("price_rub")
        if (it.get("id") is None or it.get("category") not in REF["categories"] or isinstance(price, bool)
                or not isinstance(price, int) or price <= 0 or not isinstance(it.get("city_id"), str)):
            continue
        if not isinstance(it.get("attributes"), dict):
            it["attributes"] = {}
        items.append(it)
    return items


def satisfies(item, constraints):
    attrs = item.get("attributes") or {}
    for c in constraints:
        if c["attribute"] not in attrs:
            return False  # значение неизвестно - не подтверждает соответствие
        v = attrs[c["attribute"]]
        kind = REF["categories"][c["category"]]["attrs"][c["attribute"]]["kind"]
        if kind == "bool":
            if type(v) is not bool or not OPS[c["op"]](v, c["value"]):
                return False
        else:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not OPS[c["op"]](v, c["value"]):
                return False
    return True


def constraints_for(constraints, cat):
    return [c for c in constraints if c["category"] == cat]


def build_pools(catalog, cats, city, constraints):
    pools = {}
    for cat in cats:
        pool = [it for it in catalog
                if it["category"] == cat and it["city_id"] == city
                and satisfies(it, constraints_for(constraints, cat))]
        pool.sort(key=lambda it: (it["price_rub"], str(it["id"])))
        pools[cat] = pool
    return pools


def enumerate_combos(pools, cats, budget):
    """Все комплекты (по одному товару на категорию), сумма которых не больше бюджета."""
    mins = [pools[c][0]["price_rub"] for c in cats]
    suffix = [0] * (len(cats) + 1)
    for i in range(len(cats) - 1, -1, -1):
        suffix[i] = suffix[i + 1] + mins[i]
    combos = []

    def rec(i, chosen, total):
        if i == len(cats):
            combos.append((total, tuple(chosen)))
            return
        for it in pools[cats[i]]:
            t = total + it["price_rub"]
            if t + suffix[i + 1] > budget:
                break  # пул отсортирован по цене
            chosen.append(it)
            rec(i + 1, chosen, t)
            chosen.pop()

    rec(0, [], 0)
    return combos


# ---------------------------------------------------------------- ранжирование

def match_prefs(item, prefs):
    """Номера пожеланий, ключевые слова которых встретились в тексте объявления.
    Это влияет только на порядок выбора среди уже допустимых комплектов."""
    text = (str(item.get("title", "")) + " " + str(item.get("description", ""))).lower()
    return {i for i, p in enumerate(prefs) if any(k in text for k in p["keywords"])}


def diverse_pick(rows, limit):
    """Берёт лучший по порядку комплект, затем лучшие из тех, что заметно отличаются от выбранных."""
    chosen = [0]
    k = len(rows[0][2])

    def dist(i, j):
        return sum(1 for a, b in zip(rows[i][2], rows[j][2]) if a != b)

    for need in (min(2, k), 1):
        for i in range(len(rows)):
            if len(chosen) >= limit:
                break
            if i in chosen:
                continue
            if min(dist(i, j) for j in chosen) >= need:
                chosen.append(i)
    return chosen


def select_options(combos, pools, ex, limit=3):
    """Возвращает список (total, items, label, matched) - до трёх допустимых комплектов."""
    prefs = ex.get("soft_preferences", [])
    cache = {}
    rows = []
    for total, items in combos:
        matched = set()
        if prefs:
            for it in items:
                key = str(it["id"])
                if key not in cache:
                    cache[key] = match_prefs(it, prefs)
                matched |= cache[key]
        rows.append((total, items, tuple(str(i["id"]) for i in items), frozenset(matched)))
    filtered = False
    if prefs:
        best = max(len(r[3]) for r in rows)
        if best > 0:
            rows = [r for r in rows if len(r[3]) == best]
            filtered = True
    rows.sort(key=lambda r: (r[0], r[2]))
    n = len(rows)
    scope = "среди комплектов, где найдено пожелание" if filtered else "среди подходящих комплектов"

    cpp = ex.get("category_price_preference") or {}
    pref = ex.get("price_preference")
    if n == 1:
        idx = [0]
        labels = {0: "Это единственный подходящий комплект."}
    elif cpp:
        pct = {}
        for cat, pool in pools.items():
            denom = max(len(pool) - 1, 1)
            pct[cat] = {str(it["id"]): i / denom for i, it in enumerate(pool)}

        def score(r):
            s = 0.0
            for it in r[1]:
                p = cpp.get(it["category"])
                if p:
                    s += pct[it["category"]][str(it["id"])] * (1 if p == "pricier" else -1)
            return s

        rows.sort(key=lambda r: (-score(r), r[0], r[2]))
        idx = diverse_pick(rows, limit)
        words = ", ".join("%s %s" % (REF["categories"][c]["name"], "подороже" if v == "pricier" else "подешевле")
                          for c, v in cpp.items())
        labels = {i: "Подобран под ваше пожелание по цене (%s), в рамках бюджета." % words for i in idx}
    elif pref in ("cheapest", "expensive", "middle"):
        if pref == "expensive":
            rows.sort(key=lambda r: (-r[0], r[2]))
            text = "Один из самых дорогих комплектов %s в рамках бюджета." % scope
        elif pref == "middle":
            med = rows[n // 2][0]
            rows.sort(key=lambda r: (abs(r[0] - med), r[0], r[2]))
            text = "Комплект около средней цены %s." % scope
        else:
            text = "Один из самых дешёвых комплектов %s." % scope
        idx = diverse_pick(rows, limit)
        labels = {i: text for i in idx}
    else:
        idx = sorted({0, n // 2, n - 1})
        labels = {}
        for i in idx:
            if i == 0:
                labels[i] = "Самый дешёвый комплект %s." % scope
            elif i == n - 1:
                labels[i] = "Самый дорогой комплект %s в рамках бюджета." % scope
            else:
                labels[i] = "Комплект средней цены %s." % scope
    return [(rows[i][0], rows[i][1], labels.get(i, ""), rows[i][3]) for i in idx]


# ---------------------------------------------------------------- тексты

def money(n):
    return "{:,}".format(int(n)).replace(",", " ") + " ₽"


def safe_id(item):
    """Название объявления в текст не берём (в нём может быть вставка-манипуляция): только id."""
    s = str(item["id"])
    return s if len(s) <= 40 and all(ch.isalnum() or ch in "-_." for ch in s) else "без номера"


def fmt_value(attr_spec, v):
    if attr_spec["kind"] == "bool":
        return "есть" if v else "нет"
    v = int(v) if isinstance(v, float) and v == int(v) else v
    return "%s %s" % (v, attr_spec["unit"]) if attr_spec["unit"] else str(v)


def constraint_text(c):
    spec = REF["categories"][c["category"]]["attrs"][c["attribute"]]
    if spec["kind"] == "bool":
        return "%s: %s" % (spec["label"], "нужна" if c["value"] else "не нужна")
    return "%s %s %s" % (spec["label"], OP_TEXT[c["op"]], fmt_value(spec, c["value"]))


def item_facts(item, constraints):
    cat = item["category"]
    facts = []
    for key, spec in REF["categories"][cat]["attrs"].items():
        v = item["attributes"].get(key)
        if v is None or (spec["kind"] == "bool") != (type(v) is bool):
            continue
        fact = "%s: %s" % (spec["label"], fmt_value(spec, v))
        req = [c for c in constraints if c["category"] == cat and c["attribute"] == key]
        if req:
            fact += " (требование: %s)" % "; ".join(constraint_text(c) for c in req)
        facts.append(fact)
    return facts


def explain(total, items, city, budget, label, matched, ex):
    parts = []
    for it in items:
        facts = item_facts(it, ex["constraints"])
        p = "%s (объявление %s) - %s" % (REF["categories"][it["category"]]["name"], safe_id(it), money(it["price_rub"]))
        if facts:
            p += ", " + ", ".join(facts)
        parts.append(p)
    parts[0] = parts[0][:1].upper() + parts[0][1:]
    text = "Город: %s. %s. Итого %s при бюджете %s." % (REF["cities"][city], "; ".join(parts), money(total), money(budget))
    if label:
        text += " " + label
    prefs = ex.get("soft_preferences", [])
    if prefs:
        notes = []
        for i, p in enumerate(prefs):
            if i in matched:
                where = ", ".join(REF["categories"][it["category"]]["name"] for it in items if i in match_prefs(it, prefs))
                notes.append("«%s» - слово найдено в тексте объявления (%s), но в характеристиках каталога это не подтверждено" % (p["text"], where))
            else:
                notes.append("«%s» - проверить по характеристикам каталога нельзя" % p["text"])
        text += " Пожелания: " + "; ".join(notes) + "."
    for cat, q in (ex.get("quantities") or {}).items():
        text += (" Вы просили %d шт. категории «%s», но в комплект входит по одному товару каждой категории,"
                 " остальное придётся подобрать отдельно." % (q, REF["categories"][cat]["name"]))
    return text


def no_match_message(catalog, cats, city, budget, constraints, pools):
    city_name = REF["cities"][city]
    for cat in cats:
        in_city = [it for it in catalog if it["category"] == cat and it["city_id"] == city]
        name = REF["categories"][cat]["name"]
        if not in_city:
            return ("В городе %s нет объявлений в категории «%s», поэтому полный комплект собрать нельзя. "
                    "Можно выбрать другой город или убрать эту категорию." % (city_name, name))
        if not pools[cat]:
            cons = constraints_for(constraints, cat)
            cond = ", ".join(constraint_text(c) for c in cons)
            unknown = sum(1 for it in in_city if any(c["attribute"] not in it["attributes"] for c in cons))
            msg = "Среди объявлений «%s» в городе %s нет подходящих по условию (%s)." % (name, city_name, cond)
            if unknown:
                msg += " У %d объявл. эта характеристика не указана, поэтому их нельзя подтвердить." % unknown
            return msg + " Можно ослабить это условие или выбрать другой город."
    cheapest = sum(pools[c][0]["price_rub"] for c in cats)
    return ("Подходящие товары есть, но самый дешёвый комплект стоит %s, а ваш бюджет - %s. "
            "Можно увеличить бюджет (минимум до %s), убрать одну из категорий или ослабить условия."
            % (money(cheapest), money(budget), money(cheapest)))


# ---------------------------------------------------------------- решение

def decide(ex, catalog):
    cats_ref, cities_ref = REF["categories"], REF["cities"]
    questions = []
    amb = ex.get("ambiguous", [])
    if ex["budget_rub"] is None:
        if "budget_rub" in amb:
            text = "Уточните, пожалуйста, один общий бюджет на весь комплект в рублях."
        else:
            text = "Какой у вас бюджет на весь комплект?"
        questions.append({"field": "budget_rub", "text": text})
    if ex["city_id"] is None:
        names = ", ".join(cities_ref.values())
        if "city_id" in amb:
            text = "Комплект собирается в одном городе, а вы назвали несколько. В каком искать: %s?" % names
        elif ex["unsupported_city"]:
            text = "Города «%s» нет в каталоге. В каком из городов искать: %s?" % (ex["unsupported_city"], names)
        else:
            text = "В каком городе вы хотите купить товары? Доступны: %s." % names
        questions.append({"field": "city_id", "text": text})
    if not ex["categories"] or ex["unsupported_categories"]:
        names = ", ".join(c["name"] for c in cats_ref.values())
        if "categories" in amb and not ex["unsupported_categories"]:
            text = "Уточните, пожалуйста, какие товары вам нужны. Доступны: %s." % names
        elif ex["unsupported_categories"]:
            text = "Категорий «%s» нет в каталоге. Какие товары вам нужны? Доступны: %s." % (
                "», «".join(ex["unsupported_categories"]), names)
        else:
            text = "Какие товары вам нужны? Доступны: %s." % names
        questions.append({"field": "categories", "text": text})
    if questions:
        return {"status": "clarify", "questions": questions}

    cats = [c for c in cats_ref if c in ex["categories"]]
    city, budget, cons = ex["city_id"], ex["budget_rub"], ex["constraints"]
    pools = build_pools(catalog, cats, city, cons)
    if any(not pools[c] for c in cats):
        return {"status": "no_match", "message": no_match_message(catalog, cats, city, budget, cons, pools)}
    combos = enumerate_combos(pools, cats, budget)
    if not combos:
        return {"status": "no_match", "message": no_match_message(catalog, cats, city, budget, cons, pools)}
    options = []
    for total, items, label, matched in select_options(combos, pools, ex):
        options.append({
            "listing_ids": [it["id"] for it in items],
            "total_rub": sum(it["price_rub"] for it in items),
            "explanation": explain(total, items, city, budget, label, matched, ex),
        })
    return {"status": "ok", "options": options}


def check_request(req):
    msgs = req.get("messages") if isinstance(req, dict) else None
    if not isinstance(msgs, list) or not msgs:
        raise ValueError("во входном JSON нет непустого массива messages")
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant") or not isinstance(m.get("text"), str):
            raise ValueError("сообщение должно иметь поля role (user/assistant) и text")
        if not m["text"].strip():
            raise ValueError("пустой текст сообщения: контракт требует непустую строку")
    if msgs[-1]["role"] != "user":
        raise ValueError("последнее сообщение должно принадлежать user")
    return msgs


def start_watchdog(seconds):
    """Страховка: что бы ни зависло (сеть, DNS), через seconds секунд программа завершится сама."""
    def fire():
        log("Превышено общее время запуска, завершаюсь")
        os._exit(3)
    timer = threading.Timer(seconds, fire)
    timer.daemon = True
    timer.start()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", required=True)
    args = ap.parse_args(argv)
    deadline = time.time() + TOTAL_TIMEOUT_SEC
    start_watchdog(TOTAL_TIMEOUT_SEC + 8)
    try:
        load_env()
        load_reference()
        messages = check_request(json.loads(sys.stdin.buffer.read().decode("utf-8-sig")))
        catalog = load_catalog(args.catalog)
    except Exception as e:  # noqa
        log("Ошибка входных данных: %s" % e)
        return 2
    try:
        extracted = extract_with_llm(messages, deadline)
    except LLMError as e:
        log("Ошибка модели: %s" % e)
        return 3
    log("Условия: " + json.dumps(extracted, ensure_ascii=False))
    response = decide(extracted, catalog)
    sys.stdout.buffer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0
