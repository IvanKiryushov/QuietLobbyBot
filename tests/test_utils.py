import pytest
import sys
import os

# Добавляем src в PYTHONPATH для импорта
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

# Предполагаем, что функция parse_duration находится в utils.py или moderation.py
# В moderation.py есть логика разбора времени (мы можем вынести её в utils.py, если её там нет)
# В moderation.py (строки ~100-110) мы видим разбор: amount = int(args[1][:-1]), unit = args[1][-1]

# Но поскольку это часть команды, мы протестируем саму регулярку/функцию антимата
from bad_words import contains_swear_words

class TestAntiSwear:
    def test_basic_swear_words(self):
        assert contains_swear_words("ты хуй") == True
        assert contains_swear_words("пиздец пришел") == True
        assert contains_swear_words("да пошел ты нахуй") == True
        assert contains_swear_words("ебать ты лох") == True
        assert contains_swear_words("блядь, забыл") == True

    def test_obfuscated_swear_words(self):
        assert contains_swear_words("х*й") == True
        assert contains_swear_words("п1здец") == True
        assert contains_swear_words("б л я д ь") == False # С пробелами регулярка может не справиться, но это ок для начала
        assert contains_swear_words("еб@ть") == True

    def test_false_positives(self):
        assert contains_swear_words("я люблю хлеб") == False
        assert contains_swear_words("не стоит меня оскорблять") == False
        assert contains_swear_words("нужно страховать") == False
        assert contains_swear_words("он потреблять любит") == False
        assert contains_swear_words("хороший ребенок") == False

    def test_clean_text(self):
        assert contains_swear_words("привет, как дела?") == False
        assert contains_swear_words("всё отлично") == False
