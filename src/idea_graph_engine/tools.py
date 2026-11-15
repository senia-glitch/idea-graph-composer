"""Инструменты, которые движок даёт агентам.

Помощник: добавить карточки в граф (``graph_apply``), задать человеку вопрос с вариантами (``ask``), закончить (``finish``).
Исследователь: прочитать задание (``turn_info``) и отправить находки (``report``). Больше у агентов ничего нет.
"""

from __future__ import annotations

HELPER_RULES = """
# Как ты работаешь

Ты помощник бизнес-аналитика. Человек главный: ты только предлагаешь, он принимает, отклоняет или правит.
Задача приходит во вводных хода (если их нет в сообщении — вызови `turn_info`).

Инструменты:
- `graph_apply` — добавить карточки в граф: `ops` — список операций
  `{"op":"add_card","kind":"…","title":"…","body":"…","attrs":{…}}`. Другие операции запрещены. Родитель проставляется сам.
  Если вернулся отказ — исправь по тексту отказа и повтори.
- `ask` — задать человеку ОДИН вопрос с готовыми вариантами. Только в спорной ситуации, когда без его выбора нельзя
  решить между разными вариантами; если можно выбрать разумно сам — не спрашивай. После `ask` сразу заканчивай ход:
  ответ придёт в следующем ходе.
- `finish` — работа закончена; `summary` — одна-две строки, что сделано.

Правила: пиши по-русски, коротко и конкретно; не выдумывай факты; не повторяй то, что уже есть или было отклонено;
вопросов обычным текстом не задавай; длинных пояснений в конце не пиши.
""".strip()

RESEARCHER_RULES = """
# Как ты работаешь

Ты исследователь. Вызови `turn_info`: там задание. Ищи в сети (открывай сайты), затем отправь результат
ровно одним вызовом `report`. Не пиши результат обычным текстом: считается только `report`.
Включай только то, что нашёл и можешь подтвердить ссылкой. Пиши по-русски, коротко и конкретно.
""".strip()

_TURN_INFO = {"name": "turn_info", "description": "Вводные текущего хода. Вызови, если их нет в сообщении.",
              "inputSchema": {"type": "object", "properties": {}}}

HELPER_TOOLS = [
    _TURN_INFO,
    {
        "name": "graph_apply",
        "description": "Добавить карточки в граф: ops — список {op:'add_card', kind, title, body, attrs}. "
                       "Пакет применяется целиком или не применяется.",
        "inputSchema": {"type": "object", "properties": {"ops": {"type": "array", "items": {"type": "object"}}},
                        "required": ["ops"]},
    },
    {
        "name": "ask",
        "description": "Задать человеку один вопрос с готовыми вариантами (только в спорной ситуации). "
                       "Человек может выбрать несколько вариантов, написать своё или отклонить все. После вызова заканчивай ход.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Один конкретный вопрос"},
                "options": {"type": "array", "minItems": 2, "maxItems": 4, "items": {"type": "object", "properties": {
                    "title": {"type": "string", "description": "Готовое решение, а не тема"},
                    "consequence": {"type": "string", "description": "Одна строка: что из этого следует"}},
                    "required": ["title"]}},
                "recommended": {"type": "array", "items": {"type": "integer"}, "description": "Номера рекомендуемых вариантов (с 1)"},
                "reason": {"type": "string", "description": "Почему рекомендуешь — одна строка"},
            },
            "required": ["question", "options"],
        },
    },
    {
        "name": "finish",
        "description": "Сообщить, что работа закончена.",
        "inputSchema": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
    },
]

RESEARCH_TOOLS = [
    _TURN_INFO,
    {
        "name": "report",
        "description": "Отправить находки. Вызывается ровно один раз в конце работы.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "findings": {"type": "array", "items": {"type": "object", "properties": {
                    "kind": {"type": "string", "description": "Вид находки (как назван в задании)"},
                    "title": {"type": "string", "description": "Короткое название одной строкой"},
                    "description": {"type": "string"},
                    "data": {"type": "object", "description": "Поля находки, если задание их просит"},
                    "sources": {"type": "array", "items": {"type": "string"}, "description": "Адреса страниц, откуда взято"}},
                    "required": ["title", "sources"]}},
                "summary": {"type": "string"},
            },
            "required": ["findings"],
        },
    },
]
