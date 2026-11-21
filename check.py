#!/usr/bin/env python3
"""Прогоняет тесты из my_tests/tests.json через ./run и сам проверяет ответы по каталогу.

Запуск (из папки проекта):   python check.py            - все тесты
                             python check.py strict_less quantity   - только указанные
Итог по каждому тесту: PASS (всё верно), FAIL (ошибка), REVIEW (проверьте глазами).
"""
import bisect
import itertools
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OPS = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b,
       ">=": lambda a, b: a >= b, "==": lambda a, b: a == b}
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


def load_catalog(path):
    with open(path, encoding="utf-8-sig") as f:
        return {str(x["id"]): x for x in json.load(f)}


def all_totals(test, catalog):
    """Независимый перебор: суммы всех допустимых комплектов по условиям теста."""
    pools = []
    for cat in test["cats"]:
        pool = []
        for it in catalog.values():
            if it["category"] != cat or it["city_id"] != test["city"]:
                continue
            ok = True
            for c, attr, op, val in test.get("checks", []):
                if c != cat:
                    continue
                v = (it.get("attributes") or {}).get(attr)
                if v is None or not OPS[op](v, val):
                    ok = False
            if ok:
                pool.append(it["price_rub"])
        pools.append(pool)
    return sorted(s for s in (sum(x) for x in itertools.product(*pools)) if s <= test["budget"])


def verify(test, resp, catalog, stdout_text):
    bad = []
    status = resp.get("status")
    if status not in test["status"]:
        bad.append("статус %r, ожидался один из %s" % (status, test["status"]))
        return bad
    low = stdout_text.lower()
    for w in test.get("not_text", []):
        if w.lower() in low:
            bad.append("в ответе есть запрещённый текст %r (вставка из объявления?)" % w)
    for w in test.get("must_text", []):
        if status == "ok" and w.lower() not in low:
            bad.append("в ответе нет ожидаемой фразы %r" % w)
    if status == "clarify":
        fields = [q.get("field") for q in resp.get("questions", [])]
        if not fields:
            bad.append("нет вопросов")
        if "fields" in test and sorted(fields) != sorted(test["fields"]):
            bad.append("поля вопросов %s, ожидались %s" % (sorted(fields), sorted(test["fields"])))
    elif status == "no_match":
        if not resp.get("message"):
            bad.append("пустое message")
        if "options" in resp or "listing_ids" in stdout_text:
            bad.append("в no_match не должно быть комплектов и id товаров")
        for i in catalog:
            if len(i) >= 4 and i in resp.get("message", ""):
                bad.append("в message попал id товара %s" % i)
                break
    elif status == "ok":
        opts = resp.get("options", [])
        if not 1 <= len(opts) <= 3:
            bad.append("комплектов %d, нужно от 1 до 3" % len(opts))
        seen = set()
        for n, o in enumerate(opts, 1):
            ids = o.get("listing_ids", [])
            tag = "комплект %d: " % n
            if any(str(i) not in catalog for i in ids):
                bad.append(tag + "есть id, которого нет в каталоге"); continue
            items = [catalog[str(i)] for i in ids]
            if len(set(map(str, ids))) != len(ids):
                bad.append(tag + "повторяются id")
            cats = [it["category"] for it in items]
            if len(set(cats)) != len(cats):
                bad.append(tag + "две вещи одной категории")
            if "cats" in test and sorted(cats) != sorted(test["cats"]):
                bad.append(tag + "категории %s, ожидались %s" % (sorted(cats), sorted(test["cats"])))
            cities = {it["city_id"] for it in items}
            if len(cities) != 1:
                bad.append(tag + "товары из разных городов %s" % sorted(cities))
            if "city" in test and cities != {test["city"]}:
                bad.append(tag + "город %s, ожидался %s" % (sorted(cities), test["city"]))
            total = sum(it["price_rub"] for it in items)
            if o.get("total_rub") != total:
                bad.append(tag + "total_rub=%s, а по каталогу сумма %s" % (o.get("total_rub"), total))
            if "budget" in test and total > test["budget"]:
                bad.append(tag + "сумма %s больше бюджета %s" % (total, test["budget"]))
            for cat, attr, op, val in test.get("checks", []):
                for it in items:
                    if it["category"] != cat:
                        continue
                    v = (it.get("attributes") or {}).get(attr)
                    if v is None or not OPS[op](v, val):
                        bad.append(tag + "%s %s=%r не удовлетворяет условию %s %r" % (it["id"], attr, v, op, val))
            for i in test.get("not_ids", []):
                if i in map(str, ids):
                    bad.append(tag + "в комплекте не должно быть %s" % i)
            if not o.get("explanation"):
                bad.append(tag + "пустое explanation")
            key = tuple(sorted(map(str, ids)))
            if key in seen:
                bad.append(tag + "дубль другого комплекта")
            seen.add(key)
        exp = test.get("expect_total")
        if exp and opts:
            tots = all_totals(test, catalog)
            got = [o.get("total_rub") for o in opts]
            if not tots:
                bad.append("в тесте нет допустимых комплектов по каталогу (проверьте тест)")
            elif exp == "min" and got[0] != tots[0]:
                bad.append("первый комплект %s, а самый дешёвый возможный %s" % (got[0], tots[0]))
            elif exp == "max" and got[0] != tots[-1]:
                bad.append("первый комплект %s, а самый дорогой возможный в бюджете %s" % (got[0], tots[-1]))
            elif exp == "spread" and len(opts) >= 3 and (tots[0] not in got or tots[-1] not in got):
                bad.append("в вариантах нет самого дешёвого (%s) или самого дорогого (%s): %s" % (tots[0], tots[-1], got))
            elif exp == "middle" and len(tots) > 2:
                pos = bisect.bisect_left(tots, got[0]) / (len(tots) - 1)
                if not 0.3 <= pos <= 0.7:
                    bad.append("первый комплект %s не средний по цене (позиция %.0f%% от дешёвого к дорогому)" % (got[0], pos * 100))
    return bad


def main():
    names = [a for a in sys.argv[1:] if not a.startswith("-")]
    with open(os.path.join(HERE, "my_tests", "tests.json"), encoding="utf-8") as f:
        tests = json.load(f)
    if names:
        tests = [t for t in tests if t["name"] in names]
    default_catalog = os.path.join(HERE, "data", "catalog.json")
    pause = float(os.environ.get("CHECK_PAUSE", "6"))  # лимит API: 20 вызовов в минуту
    counts = {"PASS": 0, "FAIL": 0, "REVIEW": 0}
    for n, test in enumerate(tests):
        cat_path = os.path.join(HERE, test["catalog"]) if "catalog" in test else default_catalog
        catalog = load_catalog(cat_path)
        req = json.dumps({"messages": test["messages"]}, ensure_ascii=False).encode("utf-8")
        t0 = time.time()
        try:
            p = subprocess.run([sys.executable, os.path.join(HERE, "run"), "--catalog", cat_path],
                               input=req, capture_output=True, timeout=125)
            out, err, code = p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace"), p.returncode
        except subprocess.TimeoutExpired:
            out, err, code = "", "", -1
        took = time.time() - t0
        cond = next((l for l in err.splitlines() if l.startswith("Условия:")), "")
        problems = []
        resp = {}
        if code != 0:
            problems.append("программа завершилась с кодом %s. %s" % (code, (err.strip().splitlines() or [""])[-1]))
        else:
            try:
                resp = json.loads(out)
                problems = verify(test, resp, catalog, out)
            except json.JSONDecodeError:
                problems.append("stdout - не один JSON-объект")
        if took > 100:
            problems.append("слишком долго: %.0f с (лимит на весь запуск 120 с)" % took)
        slow = 20 < took <= 100
        retries = [l for l in err.splitlines() if l.startswith("Модель:")]
        verdict = "FAIL" if problems else ("REVIEW" if "review" in test else "PASS")
        counts[verdict] += 1
        print("[%s] %-20s %-9s %4.1f с  %s" % (verdict, test["name"], resp.get("status", "-"), took, test["why"]))
        if slow:
            print("       МЕДЛЕННО: ответ модели шёл дольше 20 с (на корректность не влияет)")
        for r in retries[:3]:
            print("       " + r[:200])
        for pr in problems:
            print("       ОШИБКА:", pr)
        if verdict in ("FAIL", "REVIEW") and cond:
            print("       " + cond[:300])
        if verdict == "REVIEW":
            print("       ПРОВЕРЬТЕ:", test["review"])
            print("       ОТВЕТ:", out.strip()[:400])
        if n < len(tests) - 1:
            time.sleep(pause)
    print("\nИтого: PASS %(PASS)d, FAIL %(FAIL)d, REVIEW %(REVIEW)d" % counts)
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
