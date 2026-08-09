import key_combo


def test_tokenize_combo_basic():
    assert key_combo.tokenize_combo("ctrl+c") == ["ctrl", "c"]
    assert key_combo.tokenize_combo("Ctrl+Shift+V") == ["ctrl", "shift", "v"]


def test_tokenize_combo_invalid():
    assert key_combo.tokenize_combo("") is None
    assert key_combo.tokenize_combo(None) is None
    assert key_combo.tokenize_combo(123) is None
    assert key_combo.tokenize_combo("ctrl++v") is None  # token vuoto
    assert key_combo.tokenize_combo("+") is None


def test_is_valid_combo_true_for_known_tokens():
    assert key_combo.is_valid_combo("ctrl+c")
    assert key_combo.is_valid_combo("ctrl+shift+z")
    assert key_combo.is_valid_combo("f5")
    assert key_combo.is_valid_combo("alt+tab")


def test_is_valid_combo_false_for_unknown_tokens():
    assert not key_combo.is_valid_combo("ctrl+pippo")
    assert not key_combo.is_valid_combo("")
    assert not key_combo.is_valid_combo(None)
    assert not key_combo.is_valid_combo("f13")  # non esiste nella mappa


def test_parse_key_combo_linux_valid():
    codes = key_combo.parse_key_combo_linux("ctrl+v")
    assert codes == [29, 47]


def test_parse_key_combo_linux_invalid():
    assert key_combo.parse_key_combo_linux("ctrl+pippo") is None
    assert key_combo.parse_key_combo_linux("") is None


def test_parse_key_combo_linux_all_letters_and_digits_mapped():
    import string

    for ch in string.ascii_lowercase + string.digits:
        assert key_combo.LINUX_KEYCODES.get(ch) is not None, ch


def test_every_token_name_has_a_linux_keycode():
    # TOKEN_NAMES e' la validazione indipendente dal SO usata da daemon.py:
    # ogni token che accetta deve essere eseguibile almeno dal backend Linux
    missing = key_combo.TOKEN_NAMES - set(key_combo.LINUX_KEYCODES)
    assert not missing


def test_tokenize_combo_normalizes_common_llm_aliases():
    # "page_up"/"page_down" (con underscore, come in pynput/JS) sono un
    # sinonimo molto plausibile che un LLM genera al posto del nostro
    # "pageup"/"pagedown": vanno normalizzati, non rifiutati
    assert key_combo.tokenize_combo("ctrl+page_up") == ["ctrl", "pageup"]
    assert key_combo.tokenize_combo("page_down") == ["pagedown"]
    assert key_combo.tokenize_combo("pgup") == ["pageup"]
    assert key_combo.tokenize_combo("pgdn") == ["pagedown"]


def test_is_valid_combo_accepts_page_up_down_aliases():
    assert key_combo.is_valid_combo("page_up")
    assert key_combo.is_valid_combo("page_down")
    assert key_combo.is_valid_combo("ctrl+page_up")


def test_parse_key_combo_linux_accepts_page_up_down_aliases():
    assert key_combo.parse_key_combo_linux("page_up") == [104]
    assert key_combo.parse_key_combo_linux("page_down") == [109]


def test_tokenize_combo_normalizes_arrow_aliases():
    # "arrow_left"/"arrow_right"/... (come in JS "ArrowLeft") invece dei
    # nostri "left"/"right"
    assert key_combo.tokenize_combo("arrow_left") == ["left"]
    assert key_combo.tokenize_combo("arrow_right") == ["right"]
    assert key_combo.tokenize_combo("arrow_up") == ["up"]
    assert key_combo.tokenize_combo("arrow_down") == ["down"]


def test_is_valid_combo_accepts_arrow_aliases():
    assert key_combo.is_valid_combo("arrow_left")
    assert key_combo.is_valid_combo("ctrl+arrow_right")


def test_is_valid_combo_accepts_plus_minus():
    # "+"/"-" non possono essere token letterali (il separatore e' "+"),
    # quindi si usa la forma a parola "plus"/"minus" (come le stringhe
    # acceleratore di GTK); "-" come alias di "minus" e' comunque tollerato
    assert key_combo.is_valid_combo("ctrl+plus")
    assert key_combo.is_valid_combo("ctrl+minus")
    assert key_combo.is_valid_combo("ctrl+-")
    assert key_combo.tokenize_combo("ctrl+-") == ["ctrl", "minus"]


def test_parse_key_combo_linux_plus_minus():
    assert key_combo.parse_key_combo_linux("ctrl+plus") == [29, 13]
    assert key_combo.parse_key_combo_linux("ctrl+minus") == [29, 12]
