import unittest
from scoring import estimate_net_from_gross, classify_salary_type, salary_decision, score_vacancy

class ScoringTests(unittest.TestCase):
    def test_tax_below_first_band(self):
        self.assertAlmostEqual(estimate_net_from_gross(100000), 87000, places=2)

    def test_tax_second_band(self):
        # 250k/month = 3m/year; annual tax 312k + 90k = 402k.
        self.assertAlmostEqual(estimate_net_from_gross(250000), (3_000_000 - 402_000) / 12, places=2)

    def test_salary_unknown_needs_review(self):
        self.assertEqual(salary_decision(80000, 100000, "RUB", "unknown")[0], "review")

    def test_gross_net_filter(self):
        self.assertEqual(salary_decision(70000, 90000, "RUB", "gross")[0], "qualified")
        self.assertEqual(salary_decision(50000, 70000, "RUB", "net")[0], "rejected")

    def test_explicit_salary_type(self):
        self.assertEqual(classify_salary_type("зарплата 80 000 на руки"), "net")
        self.assertEqual(classify_salary_type("оклад 80 000 до вычета налогов"), "gross")

    def test_score_relevant_payroll_role(self):
        score, reasons = score_vacancy({
            "title": "Бухгалтер по расчету заработной платы",
            "description": "Начисление заработной платы, отпускные, больничные, 1С:БГУ, Excel, бюджетное учреждение, табели",
            "city": "Сочи",
        })
        self.assertGreaterEqual(score, 70)
        self.assertTrue(reasons)

if __name__ == "__main__":
    unittest.main()
