"""Экстрактор фактов: сырьё -> facts.json по контракту reels/CONTRACT.md.

Гарантия, которую модуль даёт сборщику: поле quote — всегда точная подстрока
исходника. Модель только предлагает кандидатов; подтверждает и записывает их
код. Не подтвердилось — факт выброшен.

    python -m reels.extract reels/input/sostav-86369.md -o reels/out/86369/facts.json
    python -m reels.extract reels/input/ -o reels/out

Только стандартная библиотека.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

KINDS = ("number", "claim", "quote", "entity", "action")

# числительные словами: «две недели» -> value 2
WORD_NUMBERS = {
    "один": "1", "одна": "1", "одного": "1", "одну": "1",
    "полтора": "1.5", "полторы": "1.5",
    "два": "2", "две": "2", "двух": "2", "оба": "2",
    "три": "3", "трёх": "3", "трех": "3",
    "четыре": "4", "четырёх": "4", "четырех": "4",
    "пять": "5", "пяти": "5", "шесть": "6", "шести": "6",
    "семь": "7", "семи": "7", "восемь": "8", "восьми": "8",
    "девять": "9", "девяти": "9", "десять": "10", "десяти": "10",
    "сто": "100", "сотня": "100", "тысяча": "1000", "тысячи": "1000",
}

PROMPT = """\
Ты извлекаешь факты из статьи для сценария короткого ролика.

Верни ТОЛЬКО JSON-массив, без markdown-обёртки и без пояснений. Каждый элемент:

{{"kind": "...", "text": "...", "value": "...", "unit": "...", "quote": "..."}}

Правила, нарушение любого делает факт негодным:

1. "quote" — фрагмент статьи ДОСЛОВНО, скопированный посимвольно. Не
   перефразируй, не сокращай, не исправляй. Обычно одно-два предложения.
2. "text" — краткий пересказ этого же фрагмента. В нём не может быть ничего,
   чего нет в "quote". Убирать можно, добавлять нельзя. Ни одного числа,
   имени или утверждения сверх процитированного.
3. "kind" — строго одно из: number, claim, quote, entity, action.
   number — факт с числом (в том числе числом словами: «две недели»).
   entity — конкретный человек, компания, продукт с тем, что он сделал.
   quote — прямая речь в кавычках.
   action — совет или шаг, который читатель может выполнить.
   claim — утверждение без числа.
4. "value" и "unit" заполняй ТОЛЬКО при kind = number, иначе оба null.
   value — само число цифрами ("2", "80", "15-20"), unit — единица
   ("недели", "уроков", "минут").

Нужно {want} фактов: самые конкретные — с числами, именами, кейсами. Возьми
и несколько kind = action, они пойдут в финал ролика.

Статья:

---
{article}
---
"""


def normalize(text):
    """Нормализованная строка + карта индексов обратно в оригинал."""
    chars, idx, prev_space = [], [], False
    for i, ch in enumerate(text):
        c = ch
        if c in "«»“”„‟":
            c = '"'
        elif c in "‘’‛":
            c = "'"
        elif c in "—–‒−":
            c = "-"
        elif c == "ё":
            c = "е"
        elif c == "Ё":
            c = "Е"
        elif c == " ":
            c = " "
        if c.isspace():
            if prev_space:
                continue
            c, prev_space = " ", True
        else:
            prev_space = False
        chars.append(c)
        idx.append(i)
    return "".join(chars), idx


def snap_to_source(quote, source, norm_source, idx_map):
    """Ищет цитату в исходнике и возвращает подстроку ИСХОДНИКА, не строку модели."""
    if not quote:
        return None
    if quote in source:  # быстрый путь: уже дословно
        return quote
    norm_quote, _ = normalize(quote)
    norm_quote = norm_quote.strip()
    if not norm_quote:
        return None
    pos = norm_source.find(norm_quote)
    if pos < 0:
        return None
    return source[idx_map[pos]:idx_map[pos + len(norm_quote) - 1] + 1]


def derive_value(quote):
    """Число из цитаты: сначала цифрами, потом словами."""
    m = re.search(r"\d+(?:[-–]\d+)?(?:[.,]\d+)?", quote)
    if m:
        return m.group(0).replace("–", "-")
    for word in re.findall(r"[а-яё]+", quote.lower()):
        if word in WORD_NUMBERS:
            return WORD_NUMBERS[word]
    return None


def score(fact):
    """Ранг: конкретное выше общего."""
    base = {"number": 4, "entity": 3, "quote": 2, "action": 2, "claim": 1}
    s = base.get(fact["kind"], 1)
    if re.search(r"\d", fact["quote"]):
        s += 2
    if len(fact["quote"]) <= 160:  # короткое лучше ложится в ролик
        s += 1
    return s


def numbers_of(fact):
    """Числа факта: цифрами и словами. «80 уроков за 15 минут» -> {'80','15'}."""
    found = set(re.findall(r"\d+", fact["quote"]))
    for word in re.findall(r"[а-яё]+", fact["quote"].lower()):
        if word in WORD_NUMBERS:
            found.add(WORD_NUMBERS[word])
    if fact.get("value"):
        found.update(re.findall(r"\d+", fact["value"]))
    return found


def content_words(fact):
    return {w for w in re.findall(r"[а-яёa-z]{4,}", fact["text"].lower())}


def spread_out(facts, cap):
    """Разводит пересекающиеся факты.

    Три факта про одни и те же 80 уроков и 15 минут — это один факт, сказанный
    трижды: сборщик поставит их подряд в «суть», и ролик будет топтаться на
    месте. Поэтому факт, чьи числа уже целиком прозвучали, уходит вниз списка,
    а не выбрасывается: объём сохраняем, а наверху — разные вещи.
    """
    picked, deferred = [], []
    used_numbers, seen_pairs = set(), []
    for fact in facts:
        nums = numbers_of(fact)
        words = content_words(fact)
        # все числа уже прозвучали — факт ничего не добавляет
        repeat = bool(nums) and nums <= used_numbers
        for prev_nums, prev_words in seen_pairs:
            if repeat:
                break
            # «за 15 минут» и «за 15-20 минут» — числа формально разные, но факт
            # один: ловим по доле общих чисел вместе с общей темой
            shared = len(nums & prev_nums) / len(nums) if nums else 0
            topic = len(words & prev_words) / max(len(words | prev_words), 1)
            repeat = (shared >= 0.5 and topic > 0.3) or topic > 0.6
        if repeat:
            deferred.append(fact)
            continue
        picked.append(fact)
        used_numbers |= nums
        seen_pairs.append((nums, words))
    return (picked + deferred)[:cap]


def parse_source(path):
    text = path.read_text(encoding="utf-8")
    title = next((ln[2:].strip() for ln in text.splitlines() if ln.startswith("# ")), path.stem)
    m = re.search(r"^Источник:\s*(\S+)", text, re.M)
    origin = m.group(1) if m else str(path)
    source_id = path.stem[len("sostav-"):] if path.stem.startswith("sostav-") else path.stem
    return text, {"id": source_id, "title": title, "origin": origin, "chars": len(text)}


def ask_model(article, want, timeout):
    result = subprocess.run(
        ["claude", "-p", PROMPT.format(article=article, want=want),
         "--tools", "", "--output-format", "text"],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "claude вернул ненулевой код").strip()[:300])
    out = result.stdout.strip()
    out = re.sub(r"^```(?:json)?\s*|\s*```$", "", out).strip()
    start, end = out.find("["), out.rfind("]")
    if start < 0 or end < 0:
        raise RuntimeError("в ответе модели нет JSON-массива")
    return json.loads(out[start:end + 1])


def facts_without_model(text):
    """Фолбэк без claude: только предложения с цифрами. Выдача беднее, но честная."""
    out = []
    for sent in re.split(r"(?<=[.!?])\s+", text):
        sent = sent.strip()
        if not re.search(r"\d", sent) or len(sent) > 300 or sent.startswith("#"):
            continue
        out.append({"kind": "number", "text": sent, "value": derive_value(sent),
                    "unit": None, "quote": sent})
    return out


def build_facts(raw_facts, text, cap, verbose):
    """Проверка кандидатов. Всё, что не подтвердилось исходником, выбрасывается."""
    norm_source, idx_map = normalize(text)
    confirmed = []
    for item in raw_facts:
        if not isinstance(item, dict):
            continue
        quote = snap_to_source(str(item.get("quote") or ""), text, norm_source, idx_map)
        if quote is None:
            continue  # цитаты нет в исходнике — факт выброшен

        fact_text = str(item.get("text") or "").strip() or quote
        # правило контракта: числа из text обязаны быть в quote
        if set(re.findall(r"\d+", fact_text)) - set(re.findall(r"\d+", quote)):
            fact_text = quote

        kind = item.get("kind")
        if kind not in KINDS:
            kind = "number" if re.search(r"\d", quote) else "claim"

        value = unit = None
        if kind == "number":
            value = str(item.get("value")).strip() if item.get("value") else derive_value(quote)
            if value is None:
                kind = "claim"  # числа не нашлось — это не number
            else:
                unit = str(item.get("unit")).strip() if item.get("unit") else None

        confirmed.append({"kind": kind, "text": fact_text, "value": value,
                          "unit": unit, "quote": quote})

    # дедуп по цитате
    seen, unique = set(), []
    for f in confirmed:
        if f["quote"] not in seen:
            seen.add(f["quote"])
            unique.append(f)

    unique.sort(key=score, reverse=True)
    kept = spread_out(unique, cap)
    # ролику нужен финал: если action не прошёл по рангу, вернём лучший
    if not any(f["kind"] == "action" for f in kept):
        action = next((f for f in unique if f["kind"] == "action"), None)
        if action and kept:
            kept[-1] = action

    for n, fact in enumerate(kept, 1):
        fact["id"] = f"f{n}"
    if verbose:
        print(f"  предложено {len(raw_facts)} / подтверждено {len(confirmed)} / "
              f"в файл {len(kept)}", file=sys.stderr)
    return [{"id": f["id"], "kind": f["kind"], "text": f["text"],
             "value": f["value"], "unit": f["unit"], "quote": f["quote"]} for f in kept]


def process(path, out_path, args):
    text, source = parse_source(path)
    if args.verbose:
        print(f"{path.name} -> {out_path}", file=sys.stderr)

    if args.no_llm:
        raw = facts_without_model(text)
    else:
        try:
            raw = ask_model(text, args.want, args.timeout)
        except (subprocess.TimeoutExpired, RuntimeError, json.JSONDecodeError) as exc:
            print(f"  модель не дала разбора ({exc}); падаю в режим без модели",
                  file=sys.stderr)
            raw = facts_without_model(text)

    payload = {"source": source, "facts": build_facts(raw, text, args.cap, args.verbose)}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    return len(payload["facts"])


def main():
    parser = argparse.ArgumentParser(description="сырьё -> facts.json")
    parser.add_argument("input", type=Path, help="файл статьи или папка со статьями")
    parser.add_argument("-o", "--out", type=Path, default=Path("reels/out"),
                        help="файл facts.json (для файла) или корень вывода (для папки)")
    parser.add_argument("--cap", type=int, default=20, help="потолок фактов на источник")
    parser.add_argument("--want", type=int, default=22, help="сколько просить у модели")
    parser.add_argument("--timeout", type=int, default=240, help="таймаут модели, секунд")
    parser.add_argument("--no-llm", action="store_true", help="без модели, только цифры")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"нет такого входа: {args.input}", file=sys.stderr)
        return 1
    if not args.no_llm and shutil.which("claude") is None:
        print("не найден бинарник claude; повтори с --no-llm", file=sys.stderr)
        return 2

    if args.input.is_dir():
        sources = sorted(p for p in args.input.iterdir()
                         if p.is_file() and p.suffix in (".md", ".txt"))
        if not sources:
            print(f"в папке {args.input} нет .md или .txt", file=sys.stderr)
            return 1
        targets = [(p, args.out / parse_source(p)[1]["id"] / "facts.json") for p in sources]
    else:
        out = args.out
        if out.suffix != ".json":  # папка вместо файла — достроим по контракту
            out = out / parse_source(args.input)[1]["id"] / "facts.json"
        targets = [(args.input, out)]

    total = 0
    for path, out_path in targets:
        total += process(path, out_path, args)
    print(f"готово: источников {len(targets)}, фактов {total}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
