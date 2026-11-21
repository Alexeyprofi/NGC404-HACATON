#!/usr/bin/env python3
"""Консольный чат с помощником: пишете запрос обычным текстом, помощник отвечает.
История диалога хранится только здесь и передаётся в ./run при каждом сообщении.
Запуск: python chat.py      Команды: «новый» - начать заново, «выход» - закончить."""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "data", "catalog.json")
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


def show(text):
    print(text.replace("₽.", "руб.").replace("₽", "руб."))


def ask(messages):
    req = json.dumps({"messages": messages}, ensure_ascii=False).encode("utf-8")
    p = subprocess.run([sys.executable, os.path.join(HERE, "run"), "--catalog", CATALOG],
                       input=req, capture_output=True, timeout=130)
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace").strip().splitlines()
        return None, (err[-1] if err else "программа завершилась с ошибкой")
    return json.loads(p.stdout.decode("utf-8")), ""


def main():
    messages = []
    print("Помощник покупателя. Пишите запрос обычным текстом, например:")
    print("  Стол не шире метра, кресло и лампа. Таганрог. До 15000 рублей.")
    print("Команды: «новый» - начать заново, «выход» - закончить.\n")
    while True:
        try:
            text = input("Вы: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text.lower() in ("выход", "exit", "q"):
            break
        if text.lower() in ("новый", "сброс"):
            messages = []
            print("Начинаем заново.\n")
            continue
        messages.append({"role": "user", "text": text})
        resp, err = ask(messages)
        if resp is None:
            messages.pop()
            print("Ошибка: %s\nПопробуйте ещё раз.\n" % err)
            continue
        if resp["status"] == "clarify":
            reply = " ".join(q["text"] for q in resp["questions"])
            for q in resp["questions"]:
                show("Помощник: " + q["text"])
        elif resp["status"] == "no_match":
            reply = resp["message"]
            show("Помощник: " + reply)
        else:
            show("Помощник: нашёл %d вариант(а):" % len(resp["options"]))
            for n, o in enumerate(resp["options"], 1):
                show("\nВариант %d: %d руб., объявления: %s" % (n, o["total_rub"], ", ".join(o["listing_ids"])))
                show(o["explanation"])
            reply = "Предложены комплекты стоимостью: " + ", ".join(str(o["total_rub"]) for o in resp["options"]) + " руб."
        messages.append({"role": "assistant", "text": reply})
        print()


if __name__ == "__main__":
    main()
