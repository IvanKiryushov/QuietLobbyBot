import pytest
import sys
import os

# Добавляем src в PYTHONPATH для импорта
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

from bad_words import contains_swear_words

class TestAntiSwear:
    def test_basic_swear_words(self):
        assert contains_swear_words("ты хуй") == True
        assert contains_swear_words("пиздец пришел") == True
        assert contains_swear_words("да пошел ты нахуй") == True
        assert contains_swear_words("ебать ты лох") == True
        assert contains_swear_words("блядь, забыл") == True

    def test_new_reported_words(self):
        # Слова, которые прислал пользователь из скриншота теста
        assert contains_swear_words("Урод") == True
        assert contains_swear_words("Ублюдок") == True
        assert contains_swear_words("Xuecoc") == True
        assert contains_swear_words("Пидр") == True
        assert contains_swear_words("Бля") == True
        assert contains_swear_words("Сука") == True

    def test_translit_and_variations(self):
        assert contains_swear_words("pizda") == True
        assert contains_swear_words("pizdec") == True
        assert contains_swear_words("blyad") == True
        assert contains_swear_words("blyat") == True
        assert contains_swear_words("huesos") == True
        assert contains_swear_words("ubludok") == True
        assert contains_swear_words("suka") == True

    def test_obfuscated_swear_words(self):
        assert contains_swear_words("х*й") == True
        assert contains_swear_words("п1здец") == True
        assert contains_swear_words("еб@ть") == True
        assert contains_swear_words("бл*дь") == True
        assert contains_swear_words("п.и.з.д.а") == True
        assert contains_swear_words("х_у_й") == True

    def test_false_positives(self):
        assert contains_swear_words("я люблю хлеб") == False
        assert contains_swear_words("не стоит меня оскорблять") == False
        assert contains_swear_words("нужно страховать") == False
        assert contains_swear_words("он потреблять любит") == False
        assert contains_swear_words("хороший ребенок") == False
        assert contains_swear_words("уродился красивым") == False
        assert contains_swear_words("он себя хорошо ведет") == False

    def test_clean_text(self):
        assert contains_swear_words("привет, как дела?") == False
        assert contains_swear_words("всё отлично") == False
        assert contains_swear_words("как дела, бро?") == False

