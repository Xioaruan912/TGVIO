from __future__ import annotations

import unittest

from tgvio_player.domain.library_filters import (
    InvalidFilters,
    LibraryFilters,
    parse_filters,
    parse_rules,
)


class LibraryFiltersTests(unittest.TestCase):
    def test_unknown_filter_key_is_rejected(self) -> None:
        with self.assertRaises(InvalidFilters):
            parse_filters({"colour": "red"})

    def test_empty_and_malformed_smart_rules_are_an_empty_selection(self) -> None:
        # A smart collection stores its rules as JSON it did not write itself, so a
        # bad blob is an empty collection, never a 500 and never "everything".
        self.assertEqual(parse_rules(None), LibraryFilters.empty())
        self.assertEqual(parse_rules(""), LibraryFilters.empty())
        self.assertEqual(parse_rules("{"), LibraryFilters.empty())
        self.assertEqual(parse_rules('{"nope": 1}'), LibraryFilters.empty())
        self.assertEqual(parse_rules("[1, 2]"), LibraryFilters.empty())

    def test_date_range_is_absolute_seconds(self) -> None:
        filters = parse_filters({"date_from": "1790000000", "date_to": "1799999999"})
        self.assertEqual((filters.date_from, filters.date_to), (1790000000, 1799999999))

    def test_random_sort_requires_a_seed(self) -> None:
        with self.assertRaises(InvalidFilters):
            parse_filters({"sort": "random"})
        self.assertEqual(parse_filters({"sort": "random", "seed": "7"}).seed, 7)

    def test_unknown_sort_and_out_of_range_numbers_are_rejected(self) -> None:
        with self.assertRaises(InvalidFilters):
            parse_filters({"sort": "cheapest"})
        with self.assertRaises(InvalidFilters):
            parse_filters({"min_seconds": "abc"})
        with self.assertRaises(InvalidFilters):
            parse_filters({"min_seconds": "-5"})

    def test_valid_smart_rules_round_trip_through_json(self) -> None:
        # Without this the malformed-rules test above would pass on an implementation
        # that always returns the empty selection.
        filters = parse_filters({"min_seconds": "30", "sort": "longest", "has_cover": "true"})
        self.assertEqual(parse_rules(filters.to_json()), filters)

    def test_empty_selection_is_the_documented_default(self) -> None:
        self.assertEqual(parse_filters({}), LibraryFilters.empty())
        self.assertEqual(LibraryFilters.empty().sort, "newest")
        self.assertIsNone(LibraryFilters.empty().seed)


if __name__ == "__main__":
    unittest.main()
