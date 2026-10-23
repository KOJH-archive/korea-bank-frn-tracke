"""로컬 실제 DB 기반 회귀 테스트. 원자료 범위를 0bp나 발행 없음으로 오인하지 않는다."""
from datetime import date
from pathlib import Path
import unittest

from streamlit.testing.v1 import AppTest


class IssueDashboardTests(unittest.TestCase):
    def test_market_quality_and_shinhan_spread_views(self):
        app=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py'),default_timeout=30).run()
        self.assertFalse(app.exception)
        self.assertTrue(any(x.label=='총 발행액' for x in app.metric))

        app.segmented_control(key='dashboard_view').set_value('데이터 품질').run()
        self.assertFalse(app.exception)
        self.assertTrue(any(x.label=='건수 확보율' for x in app.metric))
        self.assertTrue(any('발행이 없었던 것이 아닙니다' in x.value for x in app.warning))

        app.segmented_control(key='dashboard_view').set_value('가산금리 추이').run()
        self.assertFalse(app.exception)
        app.button[0].click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox(key='issue_issuer').value,'신한은행')
        self.assertEqual(app.date_input[0].value[0],date(2022,1,1))
        self.assertEqual(app.multiselect(key='compare_tenors').value,['1년','1.5년'])
        self.assertTrue(any('발행이 없었다는 뜻은 아닙니다' in x.value for x in app.info))
        self.assertTrue(any('2022년 0건' in x.value for x in app.caption))
        app.selectbox(key='issue_tenor').select('1.5년').run()
        self.assertFalse(app.exception)
        self.assertTrue(any(x.label=='동일 조건 발행' and x.value=='0건' for x in app.metric))


if __name__=='__main__':
    unittest.main()
