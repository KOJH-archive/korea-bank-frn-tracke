import sqlite3
import unittest

from issuance_terms import init_issue_schema, save_issue_observation
from kis_issuance import parse_formula


class IssuanceTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.conn.executescript("""
        CREATE TABLE issuer_mapping(issuer_id TEXT PRIMARY KEY,issuer_name TEXT);
        CREATE TABLE bond_master(isin_code TEXT PRIMARY KEY,issuer_id TEXT,issue_date TEXT,maturity_date TEXT,issue_amount INTEGER);
        INSERT INTO issuer_mapping VALUES('BANK','하나은행');
        INSERT INTO bond_master VALUES('TEST_ISIN','BANK','20260105','20270105',100000000000);
        """)
        init_issue_schema(self.conn)
        self.row=dict(isin_code='TEST_ISIN',issue_date='20260105',maturity_date='20270105',
            issue_amount=100000000000,benchmark_code='CD1M_M4_AAA',spread_bps=25.0,
            raw_terms='민평4사 AAA CD 1개월+25bp',source_ref='test://report/page1',source_type='TEST')

    def tearDown(self):
        self.conn.close()

    def test_same_source_is_idempotent_and_reset_cannot_overwrite(self):
        self.assertEqual(save_issue_observation(self.conn,self.row),'CONFIRMED')
        save_issue_observation(self.conn,self.row)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM frn_issue_observations').fetchone()[0],1)
        other={**self.row,'spread_bps':45,'raw_terms':'민평4사 AAA CD 1개월+45bp','source_ref':'test://report/page2'}
        self.assertEqual(save_issue_observation(self.conn,other),'CONFLICT')
        self.assertEqual(self.conn.execute('SELECT spread_bps,confidence_status FROM frn_issue_terms').fetchone(),(25.0,'CONFLICT'))

    def test_date_and_amount_must_match(self):
        self.assertEqual(save_issue_observation(self.conn,{**self.row,'issue_date':'20260106'}),'DATE_MISMATCH')
        self.assertEqual(save_issue_observation(self.conn,{**self.row,'issue_amount':10000000000}),'AMOUNT_MISMATCH')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM frn_issue_terms').fetchone()[0],0)

    def test_benchmark_sources_not_interchangeable(self):
        self.assertEqual(parse_formula('민평3사 AAA CD 1개월+25bp'),('CD1M_M3_AAA',25))
        self.assertEqual(parse_formula('민평4사 AAA CD 1개월-2.5bp'),('CD1M_M4_AAA',-2.5))
        self.assertEqual(parse_formula('91일물 CD+55bp'),('CD91',55))
        self.assertEqual(parse_formula('민평 4사 AAA CD 1개월+0.35%'),('CD1M_M4_AAA',35))
        self.assertEqual(parse_formula('민평4사 AAA등급 CD 1개월+13bp'),('CD1M_M4_AAA',13))
        self.assertEqual(parse_formula('CD91일+0.45%'),('CD91',45))
        self.assertIsNone(parse_formula('LOOKBACK 5bizdays WITHOUT OBSERVATION SHIFT+40bp'))
        self.assertIsNone(parse_formula('A1 91일물 CP+88bp'))


if __name__=='__main__':
    unittest.main()
