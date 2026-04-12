# OnionPress onionname word lists
# Each language module exports ADJECTIVES and NOUNS lists
# All words are ASCII lowercase (romanized for non-Latin scripts)

SUPPORTED_LANGUAGES = {
    "en_US": "en",
    "fr_FR": "fr",
    "es_ES": "es",
    "de_DE": "de",
    "nl_NL": "nl",
    "pt_BR": "pt",
    "ja": "ja",
    "zh_CN": "zh",
    "ar": "ar",
}


def get_wordlist(lang_code):
    """Load adjectives and nouns for a language code.
    Falls back to English if language not found."""
    module_name = SUPPORTED_LANGUAGES.get(lang_code, "en")
    try:
        mod = __import__(f"wordlists.{module_name}", fromlist=["ADJECTIVES", "NOUNS"])
        return mod.ADJECTIVES, mod.NOUNS
    except ImportError:
        from wordlists import en
        return en.ADJECTIVES, en.NOUNS
