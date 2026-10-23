import unittest

import pandas as pd

from dashboard_views import prepare_scope
from frn_analysis import add_frn_structure
from kis_issuance import parse_formula, unsupported_reason


class DashboardDataTests(unittest.TestCase):
    def test_official_rate_codes_separate_straight_and_fixed_to_float(self):
        frame = pd.DataFrame([
            {"rate_type_code": "2", "rate_type_name": "변동", "benchmark_code": None},
            {"rate_type_code": "3", "rate_type_name": "고정+변동", "benchmark_code": None},
            {"rate_type_code": None, "rate_type_name": None, "benchmark_code": "CD91"},
            {"rate_type_code": None, "rate_type_name": None, "benchmark_code": None},
        ])
        result = add_frn_structure(frame)
        self.assertEqual(result.frn_structure.tolist(), [
            "STRAIGHT_FRN", "FIXED_TO_FLOAT", "STRAIGHT_FRN", "OTHER_FLOATING"
        ])

    def test_market_scope_excludes_hybrid_by_default(self):
        candidates = pd.DataFrame([
            {"isin_code": "A", "issuer": "신한은행", "issue_dt": pd.Timestamp("2024-01-01"), "issue_amount": 100, "frn_structure": "STRAIGHT_FRN"},
            {"isin_code": "B", "issuer": "신한은행", "issue_dt": pd.Timestamp("2024-02-01"), "issue_amount": 200, "frn_structure": "FIXED_TO_FLOAT"},
        ])
        valid = pd.DataFrame([{"isin_code": "A"}])
        straight = prepare_scope(candidates, valid, ["신한은행"], pd.Timestamp("2024-01-01").date(), pd.Timestamp("2024-12-31").date())
        broad = prepare_scope(candidates, valid, ["신한은행"], pd.Timestamp("2024-01-01").date(), pd.Timestamp("2024-12-31").date(), True)
        self.assertEqual(straight.isin_code.tolist(), ["A"])
        self.assertEqual(len(broad), 2)
        self.assertTrue(straight.iloc[0].condition_confirmed)

    def test_formula_variants_and_exclusion_reasons_are_explicit(self):
        self.assertEqual(parse_formula("민평3사 AAA등급 CD1M+23bp"), ("CD1M_M3_AAA", 23.0))
        self.assertEqual(parse_formula("91물 CD+63bp"), ("CD91", 63.0))
        self.assertEqual(unsupported_reason("LOOKBACK 5bizdays WITHOUT OBSERVATION SHIFT+10bp"), "KOFR_OR_RFR")
        self.assertEqual(unsupported_reason("민평4사 국민은행 1년"), "OTHER_CREDIT_CURVE")
        self.assertEqual(unsupported_reason("CD1개월+20bp"), "CD_BASIS_UNSPECIFIED")

    def test_scope_explains_failure_and_outside_coverage(self):
        candidates = pd.DataFrame([
            {"isin_code": "A", "issuer": "신한은행", "issue_date": "20260105", "issue_dt": pd.Timestamp("2026-01-05"), "issue_amount": 100, "frn_structure": "STRAIGHT_FRN"},
            {"isin_code": "B", "issuer": "신한은행", "issue_date": "20230105", "issue_dt": pd.Timestamp("2023-01-05"), "issue_amount": 100, "frn_structure": "STRAIGHT_FRN"},
        ])
        reports = pd.DataFrame([{"status": "PARSED", "periods_json": '[["20260101", "20260109"]]'}])
        failures = pd.DataFrame([{"isin_code": "A", "reason": "CD_BASIS_UNSPECIFIED"}])
        scope = prepare_scope(candidates, pd.DataFrame(columns=["isin_code"]), ["신한은행"],
                              pd.Timestamp("2022-01-01").date(), pd.Timestamp("2026-12-31").date(),
                              source_reports=reports, source_failures=failures)
        reasons = scope.set_index("isin_code").missing_reason.to_dict()
        self.assertEqual(reasons["A"], "CD 기준 세부 산정방식 불명확")
        self.assertEqual(reasons["B"], "원문 보고서 수집 범위 밖")


if __name__ == '__main__':
    unittest.main()
