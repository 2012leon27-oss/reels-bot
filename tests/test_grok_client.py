import json
import unittest

from grok_client import AIResponseError, parse_structure_response, render_markdown


class StructureContractTests(unittest.TestCase):
    def test_parses_fenced_json_and_normalizes_actions(self):
        payload = {
            "title": "Запуск продукта",
            "summary": "Нужно подготовить запуск.",
            "structured_text": "## Цель\nПодготовить запуск без потери качества.",
            "decisions": ["Запускать поэтапно"],
            "actions": [
                {"task": "Собрать план", "owner": "Анна", "deadline": "пятница"},
                "Проверить аналитику",
                {"owner": "Иван"},
            ],
            "ideas": ["Начать с пилота"],
            "open_questions": ["Какой бюджет?"],
            "tags": ["запуск", "план"],
        }
        result = parse_structure_response(f"```json\n{json.dumps(payload)}\n```")

        self.assertEqual(result["title"], "Запуск продукта")
        self.assertEqual(len(result["actions"]), 2)
        self.assertEqual(result["actions"][1]["owner"], "")

    def test_rejects_empty_document(self):
        with self.assertRaises(AIResponseError):
            parse_structure_response('{"title": "Пусто"}')

    def test_markdown_export_contains_result_and_verbatim_source(self):
        structured = {
            "title": "План",
            "summary": "Коротко.",
            "structured_text": "## Контекст\nПолный контекст.",
            "decisions": [],
            "actions": [],
            "ideas": [],
            "open_questions": [],
            "tags": ["план"],
        }
        source = "Исходная запись со всеми деталями."
        markdown = render_markdown(structured, source)

        self.assertIn("# План", markdown)
        self.assertIn("## Оригинальная запись", markdown)
        self.assertIn(source, markdown)


if __name__ == "__main__":
    unittest.main()
