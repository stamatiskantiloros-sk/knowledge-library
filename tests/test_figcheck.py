"""A second model's figure reading against the first: tables row by row, other figures by numbers."""
from knowlib.figcheck import compare, numbers

TABLE = "| Stock no. | Usage | Cost |\n|---|---|---|\n| A/703 | 700 | 20.00 |\n| D/012 | 450 | 2.75 |\n| A/135 | 1,000 | 0.90 |"


def test_the_same_table_written_differently_agrees():
    other = TABLE.replace("1,000", "1000").replace("20.00", "20")
    assert compare(TABLE, other) == []


def test_a_number_in_the_wrong_row_is_caught_though_the_numbers_are_the_same():
    swapped = TABLE.replace("| 700 | 20.00 |", "| 450 | 20.00 |").replace("| 450 | 2.75 |", "| 700 | 2.75 |")
    assert numbers(TABLE) == numbers(swapped)          # OCR's bag of numbers would pass it
    got = compare(TABLE, swapped)
    assert any("a/703" in d and "700" in d and "450" in d for d in got) and len(got) == 2


def test_a_diagram_is_compared_by_its_numbers():
    a = "5,000 Delivery 20% Failure 1,000 Switch 530"
    assert compare(a, "5000 Delivery 20% Failure 1000 Switch 530") == []
    assert compare(a, "5000 Delivery 20% Failure 1000 Switch 580") != []
