import random
import unittest

from evidence.intervals import assert_normalized, coverage, day_parts, duration, intersect, merge, subtract


class IntervalTests(unittest.TestCase):
    def test_merge_adjacency_and_nested(self):
        self.assertEqual(merge([(1, 4), (2, 3), (4, 6), (9, 9)]), [(1, 6)])
        self.assertEqual(duration([(1, 4), (2, 3), (4, 6)]), 5)
        self.assertTrue(assert_normalized(merge([(8, 10), (1, 3)])))

    def test_subtraction_multiple_holes(self):
        self.assertEqual(subtract([(0, 10)], [(1, 3), (2, 4), (7, 12)]), [(0, 1), (4, 7)])
        self.assertEqual(subtract([(0, 10)], [(-1, 11)]), [])
        self.assertEqual(subtract([(0, 10)], [(10, 20)]), [(0, 10)])
        self.assertEqual(intersect([(0, 10)], [(10, 20)]), [])

    def test_randomized_set_oracle(self):
        rng = random.Random(114)
        for _ in range(300):
            first = [tuple(sorted((rng.randrange(-30, 30), rng.randrange(-30, 30)))) for _ in range(8)]
            second = [tuple(sorted((rng.randrange(-30, 30), rng.randrange(-30, 30)))) for _ in range(8)]
            a = {v for left, right in first for v in range(left, right)}
            b = {v for left, right in second for v in range(left, right)}
            self.assertEqual(duration(first), len(a))
            self.assertEqual(duration(intersect(first, second)), len(a & b))
            self.assertEqual(duration(subtract(first, second)), len(a - b))
            self.assertTrue(assert_normalized(subtract(first, second)))

    def test_day_boundary_before_epoch(self):
        result = day_parts([(-2, 2)])
        self.assertEqual(result, {"1969-12-31": 2, "1970-01-01": 2})
        self.assertEqual(sum(result.values()), 4)
        self.assertEqual(day_parts([]), {})

    def test_coverage_gaps(self):
        result = coverage([(1, 3), (6, 11)], (0, 10))
        self.assertEqual(result["seconds"], 6)
        self.assertEqual(result["gaps"], [(0, 1), (3, 6)])
        self.assertEqual(result["fraction"], 0.6)


if __name__ == "__main__":
    unittest.main()
