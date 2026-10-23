"""은행별 원만기별 최초 발행 가산금리 시계열."""
import json
from datetime import date
from pathlib import Path
import altair as alt
import pandas as pd
import streamlit as st
from config import DB_PATH
from db import get_connection, init_db
from frn_analysis import (COMMERCIAL_BANKS, FRN_STRUCTURE_LABELS, load_bank_frn,
                          MATURITY_LABELS, spread_percentile)
from issuance_terms import load_issue_terms, load_source_coverage, BENCHMARK_LABELS
from frn_parser import parse_frn_quote
from reference_rates import import_reference_rate_csv
from dashboard_views import prepare_scope, render_market, render_quality

st.set_page_config(page_title='은행 FRN 트래킹',page_icon='🏦',layout='wide')
st.title('은행 FRN 트래킹')
st.caption('발행시장 규모와 발행 당시 CD+가산금리를 분리해 봅니다. 이후 리셋된 표면금리는 최초 조건을 덮어쓰지 않습니다.')

@st.cache_resource
def initialize():
    init_db()

@st.cache_data(max_entries=3, show_spinner='FRN 데이터를 불러오는 중입니다.')
def load_dashboard_data(db_signature):
    """DB 파일이 교체될 때만 원자료를 다시 읽는다."""
    del db_signature
    conn=get_connection()
    try:
        terms=load_issue_terms(conn)
        candidates=load_bank_frn(conn)
        rates=pd.DataFrame([dict(r) for r in conn.execute("SELECT rate_date,rate_value,source FROM reference_rate_daily WHERE benchmark_code='CD91' ORDER BY rate_date")])
        source_reports,source_failures,conflicts=load_source_coverage(conn)
    finally:
        conn.close()
    return terms,candidates,rates,source_reports,source_failures,conflicts

initialize()
db_path=Path(DB_PATH)
db_stat=db_path.stat()
terms,candidates,rates,source_reports,source_failures,conflicts=load_dashboard_data((db_stat.st_mtime_ns,db_stat.st_size))
valid=terms[terms.confidence_status=='CONFIRMED'].copy() if not terms.empty else terms
view=st.segmented_control('화면',['발행시장 규모','가산금리 추이','데이터 품질'],default='발행시장 규모',key='dashboard_view',width='stretch')
candidate_dates=candidates.issue_dt.dropna() if not candidates.empty else pd.Series(dtype='datetime64[ns]')
default_start=max(date(2022,1,1),candidate_dates.min().date()) if not candidate_dates.empty else date(2022,1,1)
default_end=min(date.today(),candidate_dates.max().date()) if not candidate_dates.empty else date.today()
st.sidebar.header('분석 범위')
selected_banks=st.sidebar.multiselect('시중은행 5개',COMMERCIAL_BANKS,default=COMMERCIAL_BANKS,key='market_banks')
market_dates=st.sidebar.date_input('발행일 범위',value=(default_start,default_end),key='market_dates')
include_hybrid=st.sidebar.toggle('고정→변동 전환 포함',value=False,help='기본값은 발행일부터 변동금리인 직선형 FRN입니다.')
st.sidebar.divider()
st.sidebar.subheader('수집 현황')
st.sidebar.metric('은행 FRN 후보',f'{len(candidates):,}건')
st.sidebar.metric('최초 발행조건 확보',f'{len(valid):,}건')
st.sidebar.caption('ISIN·발행일·만기·금액을 대조한 발행조건입니다. 출처 간 조건 충돌은 비교에서 제외합니다.')
st.sidebar.caption('CD 1개월 민평3사·민평4사·CD91은 각각 다른 비교 기준입니다.')

if len(market_dates)!=2:
    st.info('시작일과 종료일을 모두 선택해 주세요.')
    st.stop()
if not selected_banks:
    st.info('분석할 은행을 하나 이상 선택해 주세요.')
    st.stop()
market_start,market_end=market_dates
scope=prepare_scope(candidates,valid,selected_banks,market_start,market_end,include_hybrid,
                    source_reports,source_failures,conflicts)

if view=='발행시장 규모':
    render_market(scope)
    st.caption('현재 집계 기준: '+('직선형 FRN + 고정→변동 전환' if include_hybrid else FRN_STRUCTURE_LABELS['STRAIGHT_FRN']))
    st.stop()
if view=='데이터 품질':
    render_quality(scope,selected_banks,Path(__file__).parent/'data'/'issuance_collection_report.json',rates,source_reports)
    st.stop()

if valid.empty:
    st.info('최초 발행조건 수집이 필요합니다. kis_issuance.py로 공개 발행현황을 대조해 적재할 수 있습니다.')
else:
    period=valid[(valid.issue_dt.dt.date>=market_start)&(valid.issue_dt.dt.date<=market_end)&valid.issuer.isin(selected_banks)].copy()
    if period.empty:
        st.warning('선택 범위에서 확인된 최초 발행조건이 없습니다. 데이터 품질 화면에서 미확보 현황을 확인해 주세요.')
        st.stop()
    issuers=sorted(period.issuer.unique())
    codes=sorted(period.benchmark_code.unique())
    st.session_state.setdefault('issue_issuer','하나은행' if '하나은행' in issuers else issuers[0])
    if st.session_state.issue_issuer not in issuers:
        st.session_state.issue_issuer=issuers[0]
    st.session_state.setdefault('issue_tenor','1년')
    st.session_state.setdefault('issue_benchmark','CD1M_M4_AAA' if 'CD1M_M4_AAA' in codes else codes[0])
    if st.session_state.issue_benchmark not in codes:
        st.session_state.issue_benchmark=codes[0]
    st.session_state.setdefault('target_spread',59.0)
    st.session_state.setdefault('issue_dates',(market_start,market_end))
    st.session_state.setdefault('compare_tenors',['1년','1.5년'])
    if st.button('신한은행 · 2022년부터 1년/1.5년 비교'):
        st.session_state.issue_issuer='신한은행'
        st.session_state.issue_benchmark='CD1M_M4_AAA'
        st.session_state.issue_tenor='1년'
        st.session_state.issue_dates=(date(2022,1,1),date.today())
        st.session_state.compare_tenors=['1년','1.5년']
    quote=st.text_input('빠른 조건 입력',placeholder='하나은행 1.5년 1CD+59bp')
    if st.button('조회 조건 적용',disabled=not quote):
        parsed=parse_frn_quote(quote)
        if parsed['issuer'] not in issuers or not parsed['maturity_bucket']:
            st.warning('확보된 발행사명과 만기를 확인해 주세요.')
        else:
            st.session_state.issue_issuer=parsed['issuer']
            st.session_state.issue_tenor=parsed['maturity_bucket']
            if parsed['spread_bps'] is not None:
                st.session_state.target_spread=parsed['spread_bps']
            if parsed['benchmark_code'] in codes:
                st.session_state.issue_benchmark=parsed['benchmark_code']
            elif parsed['benchmark_code']=='CD1M':
                st.info('CD 1개월의 민평3사·4사 기준은 아래 선택에서 확인해 주세요.')
            else:
                st.warning('기준금리 산정방식은 아래에서 직접 선택해 주세요.')
    c1,c2,c3=st.columns(3)
    issuer=c1.selectbox('발행 은행',issuers,key='issue_issuer')
    bucket=c2.selectbox('발행 당시 원만기',MATURITY_LABELS,key='issue_tenor')
    benchmark=c3.selectbox('기준금리 산정방식',codes,key='issue_benchmark',format_func=lambda c:BENCHMARK_LABELS.get(c,c))
    start,end=market_start,market_end
    st.subheader(f'{issuer} · 원만기별 발행 가산금리 비교')
    compare_tenors=st.multiselect('비교할 원만기',MATURITY_LABELS,key='compare_tenors')
    comparison=period[(period.issuer==issuer)&(period.benchmark_code==benchmark)&period.maturity_bucket.astype(str).isin(compare_tenors)].copy()
    bank_candidates=candidates[(candidates.issuer==issuer)&(candidates.issue_date>=start.strftime('%Y%m%d'))&(candidates.issue_date<=end.strftime('%Y%m%d'))]
    with st.container(horizontal=True):
        for tenor in compare_tenors:
            sample=comparison[comparison.maturity_bucket.astype(str)==tenor]
            candidate_count=len(bank_candidates[bank_candidates.maturity_bucket.astype(str)==tenor])
            with st.container(border=True):
                st.metric(f'{tenor} · 선택 기준 원문',f'{len(sample)}건')
                st.caption(f'동일 은행·만기 전체 FRN 후보 {candidate_count}건')
    st.caption(f'{start:%Y-%m-%d} ~ {end:%Y-%m-%d} · {BENCHMARK_LABELS.get(benchmark,benchmark)} · 원문 확인 표본과 전체 후보를 서로 다른 값으로 표시합니다.')
    empty_tenors=[t for t in compare_tenors if not (comparison.maturity_bucket.astype(str)==t).any()]
    if empty_tenors:
        st.info(f'{", ".join(empty_tenors)}: 확인된 발행조건이 없습니다. 발행이 없었다는 뜻은 아닙니다.')
    if not comparison.empty:
        comparison['원만기']=comparison.maturity_bucket.astype(str)
        comparison['발행액(억원)']=pd.to_numeric(comparison.issue_amount,errors='coerce')/1e8
        comparison_chart=alt.Chart(comparison).mark_circle(size=85).encode(
            x=alt.X('issue_dt:T',title='발행일',scale=alt.Scale(domain=[start.isoformat(),end.isoformat()]),axis=alt.Axis(format='%Y',tickCount=6)),
            y=alt.Y('spread_bps:Q',title='최초 가산금리(bp)',scale=alt.Scale(zero=False)),
            color=alt.Color('원만기:N',scale=alt.Scale(domain=compare_tenors,scheme='tableau10')),
            size=alt.Size('발행액(억원):Q',title='발행액(억원)',scale=alt.Scale(range=[45,260])),
            tooltip=['issue_date:N','원만기:N','spread_bps:Q','발행액(억원):Q','isin_code:N','raw_terms:N']
        ).properties(height=280,title=f'{issuer} · 최초 발행 가산금리')
        st.altair_chart(comparison_chart,width='stretch',alt=f'{issuer}의 선택 원만기별 최초 발행 가산금리. 미확보 기간은 빈 구간으로 표시합니다.')
        years=range(start.year,end.year+1)
        yearly=comparison.groupby(comparison.issue_dt.dt.year).size()
        st.caption('확보 표본: '+' · '.join(f'{year}년 {int(yearly.get(year,0))}건' for year in years))
        st.caption('점만 표시합니다. 빈 구간의 금리나 미확보 만기의 선을 추정하지 않습니다.')
    else:
        st.warning('선택 조건의 발행 가산금리가 미확보 상태입니다. 0bp로 표시하지 않습니다.')
    st.divider()
    same=period[(period.maturity_bucket.astype(str)==bucket)&(period.benchmark_code==benchmark)]
    peers=same[same.issuer==issuer].sort_values(['issue_date','isin_code']).copy()
    target=st.number_input('비교할 신규 발행 가산금리(bp)',step=1.0,key='target_spread')
    metrics=st.columns(4)
    metrics[0].metric('동일 조건 발행',f'{len(peers):,}건')
    metrics[1].metric('가산금리 중앙값',f'{peers.spread_bps.median():.1f}bp' if len(peers) else '-')
    metrics[2].metric('최근 발행 가산금리',f'{peers.iloc[-1].spread_bps:g}bp' if len(peers) else '-')
    pct=spread_percentile(peers.spread_bps,target) if len(peers)>=5 else None
    metrics[3].metric('입력값 과거 백분위',f'{pct:.1f}%' if pct is not None else '표본 5건 미만')
    st.subheader(f'{issuer} · {bucket} · 최초 발행 가산금리 추이')
    st.caption('각 점은 개별 발행 종목입니다. 발행 당시 가산금리를 보존하며 이후 리셋 표면금리로 갱신하지 않습니다.')
    if peers.empty:
        st.info('선택 조건의 확보된 발행조건이 없습니다. 원만기·기간·기준금리를 변경해 주세요.')
    else:
        peers['rolling_median']=peers.spread_bps.rolling(5,min_periods=1).median()
        points=alt.Chart(peers).mark_circle(size=80).encode(
            x=alt.X('issue_dt:T',title='발행일'),
            y=alt.Y('spread_bps:Q',title='최초 가산금리(bp)',scale=alt.Scale(zero=False)),
            tooltip=[alt.Tooltip('issue_dt:T',title='발행일'),alt.Tooltip('spread_bps:Q',title='가산금리(bp)'),
                     alt.Tooltip('bond_name:N',title='종목'),alt.Tooltip('isin_code:N',title='ISIN'),
                     alt.Tooltip('raw_terms:N',title='발행조건 원문')])
        trend=alt.Chart(peers).mark_line(color='#EF8A22').encode(x='issue_dt:T',y='rolling_median:Q')
        st.altair_chart(points+trend,width='stretch',alt='동일 은행과 원만기의 최초 발행 가산금리와 최근 5건 중앙값')
        st.caption('주황선: 발행일·종목번호 순 최근 5건 중앙값. 같은 날짜의 종목 간 순서는 종목번호 기준입니다.')
        details=peers[['issue_date','issuer','maturity_date','original_tenor_months','spread_bps','raw_terms','issue_amount_억원','isin_code','source_ref']].rename(columns={
            'issue_date':'발행일','issuer':'은행','maturity_date':'만기일','original_tenor_months':'원만기(개월)',
            'spread_bps':'최초 가산금리(bp)','raw_terms':'발행조건 원문','issue_amount_억원':'발행액(억원)','isin_code':'ISIN','source_ref':'출처'})
        st.dataframe(details.sort_values('발행일',ascending=False),hide_index=True,width='stretch',
                     column_config={'출처':st.column_config.LinkColumn('원문 보고서')},alt='최초 발행조건과 종목별 원문 출처')
        st.download_button('선택 발행조건 CSV 다운로드',details.to_csv(index=False).encode('utf-8-sig'),file_name='frn_issue_spreads.csv',mime='text/csv')
    st.subheader('같은 기간·원만기·기준금리의 타행 비교')
    banks=sorted(same.issuer.unique())
    chosen_banks=st.multiselect('비교 은행',banks,default=banks)
    cross=same[same.issuer.isin(chosen_banks)]
    if not cross.empty:
        chart=alt.Chart(cross).mark_circle(size=65).encode(
            x=alt.X('issue_dt:T',title='발행일'),y=alt.Y('spread_bps:Q',title='최초 가산금리(bp)',scale=alt.Scale(zero=False)),
            color=alt.Color('issuer:N',title='은행'),tooltip=['issuer:N','issue_date:N','spread_bps:Q','isin_code:N'])
        st.altair_chart(chart,width='stretch',alt='동일 원만기와 기준금리의 은행별 최초 발행 가산금리')
        summary=cross.groupby('issuer').spread_bps.agg(['count','median','min','max'])
        summary['입력값-중앙값(bp)']=target-summary['median']
        st.dataframe(summary.rename(columns={'count':'발행건수','median':'중앙값(bp)','min':'최저(bp)','max':'최고(bp)'}),width='stretch')

with st.expander('수집 범위와 미확보 종목 확인'):
    st.caption('확보된 발행조건은 전체 FRN 후보의 일부입니다. 빈 날짜를 발행 없음으로 단정하지 않습니다.')
    path=Path(__file__).parent/'data'/'issuance_collection_report.json'
    if path.exists():
        report=json.loads(path.read_text(encoding='utf-8'))
        failed=sum('error' in r for r in report['reports'])
        st.caption(f"최근 수집: {report['completed_at']} · 보고서 {len(report['reports'])}건 · 오류 {failed}건")
        st.dataframe(pd.DataFrame([{'보고서일':r['report_date'],'발행기간':str(r.get('periods',[])),'출처':r['url'],'오류':r.get('error','')} for r in report['reports']]),hide_index=True,width='stretch')
    if not candidates.empty:
        covered=set(valid.isin_code) if not valid.empty else set()
        missing=candidates[~candidates.isin_code.isin(covered)]
        st.caption(f'최초 발행조건 미확보 후보: {len(missing):,}건')
        st.dataframe(missing[['issue_date','issuer','bond_name','isin_code','maturity_bucket']],hide_index=True,width='stretch')
    if not terms.empty and (terms.confidence_status=='CONFLICT').any():
        st.warning('출처 간 발행조건이 충돌한 종목은 비교에서 제외했습니다.')

with st.expander('CD91 일별 금리 · 역산용 기준자료'):
    st.caption('CD91은 민평 CD 1개월과 다른 시계열입니다. 최초 적용금리와 실제 fixing 규칙이 확인된 CD91 종목의 역산에 사용합니다.')
    if not rates.empty:
        rates['date']=pd.to_datetime(rates.rate_date,format='%Y%m%d')
        st.caption(f"CD91 {len(rates):,}건 · {rates.date.min():%Y-%m-%d} ~ {rates.date.max():%Y-%m-%d}")
        st.line_chart(rates.set_index('date')[['rate_value']].rename(columns={'rate_value':'CD91(%)'}))
    upload=st.file_uploader('다른 기준금리 CSV 가져오기',type='csv')
    if st.button('기준금리 저장',disabled=upload is None):
        conn=get_connection()
        try:
            count=import_reference_rate_csv(conn,upload)
            st.success(f'{count}건 저장 완료')
        except Exception as exc:
            st.error(str(exc))
        finally:
            conn.close()
