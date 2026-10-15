"""Schema/validation contract: extracted to review_schema, defects fixed."""
import unittest

from review_schema import (
    ValidationError,
    parse_json_strict,
    validate_review,
    validate_review_context,
    validate_skip,
)
from tests.helpers import CARD


class ReviewContextTest(unittest.TestCase):
    def test_valid_context_passes(self):
        review = validate_review_context(dict(CARD))
        self.assertEqual(review["review_id"], "job-001")

    def test_unknown_field_rejected(self):
        args = dict(CARD, prompt="should never be captured")
        with self.assertRaises(ValidationError):
            validate_review_context(args)

    def test_nonfinite_number_rejected(self):
        args = dict(CARD, resources={"tokens": float("inf")})
        with self.assertRaises(ValidationError):
            validate_review_context(args)

    def test_bool_value_rejected(self):
        args = dict(CARD, context={"model": True})
        with self.assertRaises(ValidationError):
            validate_review_context(args)


class FullReviewTest(unittest.TestCase):
    def test_direct_review_rejects_browser_only_revision(self):
        from tests.helpers import full_payload
        with self.assertRaises(ValidationError):
            validate_review(full_payload(revision=1))

    def test_valid_full_passes(self):
        from tests.helpers import full_payload
        review = validate_review(full_payload())
        self.assertEqual(review["ratings"]["overall"], 5)

    def test_bool_rating_rejected(self):
        from tests.helpers import full_payload
        with self.assertRaises(ValidationError):
            validate_review(full_payload(ratings={"overall": True, "quality": 4,
                                                  "speed": 3, "resource_efficiency": 4}))

    def test_gap_reasons_require_no_completion(self):
        from tests.helpers import full_payload
        with self.assertRaises(ValidationError):
            validate_review(full_payload(completion="yes", gap_reasons=["incomplete"]))


class SkipTest(unittest.TestCase):
    def test_exact_skip_ok(self):
        self.assertTrue(validate_skip({"skip": True}))

    def test_numeric_skip_rejected(self):
        with self.assertRaises(ValidationError):
            validate_skip({"skip": 1})

    def test_skip_with_extras_rejected(self):
        with self.assertRaises(ValidationError):
            validate_skip({"skip": True, "ratings": {}})


class StrictJsonTest(unittest.TestCase):
    def test_nan_rejected(self):
        with self.assertRaises(ValidationError):
            parse_json_strict(b'{"tokens": NaN}')

    def test_infinity_rejected(self):
        with self.assertRaises(ValidationError):
            parse_json_strict(b'{"tokens": Infinity}')


if __name__ == "__main__":
    unittest.main()
