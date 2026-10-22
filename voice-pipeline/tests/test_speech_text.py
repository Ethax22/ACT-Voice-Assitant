"""Acronyms reach the voice joined up, not as spaced letters (spaced letters were spoken with gaps)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from speech_text_filter import to_speech


def say(text):
    return asyncio.run(to_speech(text))


def test_degree_names_and_acronyms_have_no_spaced_letters():
    assert say("We offer B.E. and M.E.") == "We offer BE and ME."
    assert say("B. E. and M. E. programs") == "BE and ME programs"
    assert say("B.Tech, M.Tech and Ph.D.") == "BTech, MTech and PhD."
    for acronym in ("CSE", "ECE", "NBA", "TNEA"):
        assert say(f"Apply for {acronym} today") == f"Apply for {acronym} today"   # left as written
    assert say("AI&DS and EEE") == "AIDS and triple E"


def test_a_plain_word_me_is_not_touched():
    assert say("Please help me, ME is a degree") == "Please help me, ME is a degree"


def test_years_and_ranges_are_spoken_as_numbers_not_digits():
    assert say("in 2024") == "in twenty twenty-four"
    assert say("2024-25 season-ல") == "twenty twenty-four to twenty-five season-ல"
    assert say("2001-ல") == "two thousand one-ல"
    assert say("1999") == "nineteen ninety-nine"


def test_ratings_percentages_and_counts():
    assert say("3.9 out of 5") == "three point nine out of five"
    assert say("88% students") == "eighty-eight percent students"
    assert say("71,670 books") == "seventy-one thousand six hundred seventy books"
    assert say("175-185 companies") == "one hundred seventy-five to one hundred eighty-five companies"


def test_internal_assessment_test_is_spoken_in_full():
    assert say("IA TEST-1 marks.") == "Internal Assessment Test one marks."
    assert say("your IA Test-1 marks") == "your Internal Assessment Test one marks"
    assert say("IAT-2 results") == "Internal Assessment Test two results"
    assert say("I.A.T 3") == "Internal Assessment Test three"
    assert say("the IAT marks") == "the Internal Assessment Test marks"
    assert say("Internal Assessment Test 1 marks.") == "Internal Assessment Test one marks."
    assert say("IATA rules and the VIA test 1") == "IATA rules and the VIA test one"


def test_phone_numbers_and_codes_stay_as_digits():
    assert say("call 044-67409444") == "call 044-67409444"
    assert say("counselling code 1316") == "counselling code 1316"
    assert say("number 94450") == "number 94450"
    assert say("24/7 and the 1st year") == "24/7 and the 1st year"


def test_agni_is_said_with_the_stress_on_ag():
    assert say("Welcome to Agni College of Technology, this is Agni AI.") == \
        "Welcome to <<ˈ|ʌ|g|n|i>> College of Technology, this is <<ˈ|ʌ|g|n|i>> AI."
    assert say("Agni-ல படிக்கலாம்") == "<<ˈ|ʌ|g|n|i>>-ல படிக்கலாம்"
    assert say("Agnihotri") == "Agnihotri"          # a longer word is left alone


def test_words_built_on_agni():
    assert say("Agnians from Mechanical won.") == "<<ˈ|ʌ|g|n|i|ə|n|z>> from Mechanical won."
    assert say("Agnifest'26, Agniverse and the AGNIHACKATHON") == \
        "<<ˈ|ʌ|g|n|i>> Fest'twenty-six, <<ˈ|ʌ|g|n|i>> Verse and the <<ˈ|ʌ|g|n|i>> Hackathon"


def test_agnishwar_and_garuda_depend_on_the_sentence_language():
    assert say("Our Vice Chairman is Mr. Agnishwar Jayaprakash, the founder of Garuda Aerospace.") == \
        "Our Vice Chairman is Mr. <<ˈ|a|g|n|i|ʃ|w|ə|r>> Jayaprakash, the founder of <<ˈ|g|a|r|u|d|ɑː>> Aerospace."
    assert say("எங்க Vice Chairman Mr. Agnishwar Jayaprakash, Garuda Aerospace-ஓட founder.") == \
        "எங்க Vice Chairman Mr. <<ˈ|ʌ|g|n|i|ʃ|w|ə|r>> Jayaprakash, Garuda Aerospace-ஓட founder."
