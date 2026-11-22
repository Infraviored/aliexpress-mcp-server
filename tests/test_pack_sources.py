import deals


def test_pairs_and_teile_are_pack_words():
    assert deals.pack_size("Male Female 90 Angle / 5P / 3Pairs") == 3
    assert deals.pack_size("5P / 1 Pair") == 1
    assert deals.pack_size("10 teile/los") == 10
    assert deals.pack_size("5 Paar") == 5


def test_title_pack_reads_a_single_stated_size():
    assert deals.title_pack("10PCS TSAL4400 TSAL6100 EMITTER IR") == 10
    assert deals.title_pack("10 Stück TEFT4300 D=3mm") == 10


def test_title_pack_ignores_choices_ranges_and_conflicts():
    assert deals.title_pack("1/3/5 Pair Magnetic Pogo Pin") == 1
    assert deals.title_pack("1-10 stücke HX711 Modul") == 1
    assert deals.title_pack("5PCS or 10PCS chip") == 1
    assert deals.title_pack("ESP32 board") == 1
