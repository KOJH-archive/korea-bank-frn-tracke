"""FRN 대시보드의 시장 규모와 데이터 품질 화면."""
import json
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st


REASON_LABELS = {
    "CONFLICT": "출처 간 조건 충돌",
    "FIXED_TO_FLOAT": "고정→변동 전환형(직선형 제외)",
    "KOFR_OR_RFR": "KOFR·RFR 산식(현재 비교 기준 밖)",
    "CP_BENCHMARK": "CP 기준금리(현재 비교 기준 밖)",
    "CD_BASIS_UNSPECIFIED": "CD 기준 세부 산정방식 불명확",
    "OTHER_CREDIT_CURVE": "다른 민평 신용곡선",
    "UNPARSED_ROW": "PDF 행 구조 인식 실패",
    "UNSUPPORTED_FORMULA": "지원하지 않는 산식",
}


def prepare_scope(candidates, valid_terms, banks, start, end, include_hybrid=False,
                  source_reports=None, source_failures=None, conflicts=None):
    """선택 범위의 FRN 후보에 원문 발행조건 확보 여부를 붙인다."""
    structures = ["STRAIGHT_FRN", "FIXED_TO_FLOAT"] if include_hybrid else ["STRAIGHT_FRN"]
    scope = candidates[
        candidates.issuer.isin(banks)
        & (candidates.issue_dt.dt.date >= start)
        & (candidates.issue_dt.dt.date <= end)
        & candidates.frn_structure.isin(structures)
    ].copy()
    confirmed_ids = set(valid_terms.isin_code) if not valid_terms.empty else set()
    scope["condition_confirmed"] = scope.isin_code.isin(confirmed_ids)
    scope["missing_reason"] = "원문 보고서 수집 범위 밖"
    if source_reports is not None and not source_reports.empty:
        periods = []
        for raw in source_reports[source_reports.status == "PARSED"].periods_json:
            try:
                periods.extend(json.loads(raw))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        covered = scope.issue_date.map(
            lambda d: any(begin <= str(d) <= finish for begin, finish in periods)
        )
        scope.loc[covered, "missing_reason"] = "보고서 대상기간 내 원문 미매칭"
    if source_failures is not None and not source_failures.empty:
        reasons = (source_failures.dropna(subset=["isin_code"])
                   .drop_duplicates("isin_code", keep="last")
                   .set_index("isin_code")["reason"])
        mapped = scope.isin_code.map(reasons)
        scope.loc[mapped.notna(), "missing_reason"] = mapped.dropna().map(REASON_LABELS)
    if conflicts:
        scope.loc[scope.isin_code.isin(conflicts), "missing_reason"] = REASON_LABELS["CONFLICT"]
    scope.loc[scope.condition_confirmed, "missing_reason"] = "원문 조건 확인"
    scope["year"] = scope.issue_dt.dt.year
    scope["issue_amount"] = pd.to_numeric(scope.issue_amount, errors="coerce").fillna(0)
    return scope


def render_market(scope):
    st.header("발행시장 규모")
    st.caption("금융위 종목정보의 공식 금리유형을 기준으로 집계합니다. 기본값은 발행일부터 변동금리인 직선형 FRN입니다.")
    if scope.empty:
        st.warning("선택 범위에 해당하는 FRN 후보가 없습니다.")
        return

    with st.container(horizontal=True):
        st.metric("총 발행액", f"{scope.issue_amount.sum()/1e12:,.3f}조원", border=True)
        st.metric("발행 종목", f"{len(scope):,}건", border=True)
        st.metric("직선형 FRN", f"{(scope.frn_structure == 'STRAIGHT_FRN').sum():,}건", border=True)
        st.metric("최근 발행일", f"{scope.issue_dt.max():%Y-%m-%d}", border=True)

    annual_bank = (
        scope.groupby(["year", "issuer"], observed=True)
        .agg(발행액=("issue_amount", "sum"), 발행건수=("isin_code", "nunique"))
        .reset_index()
    )
    annual_bank["발행액(조원)"] = annual_bank.발행액 / 1e12
    annual_bank["연도"] = annual_bank.year.astype(str)
    chart = alt.Chart(annual_bank).mark_bar().encode(
        x=alt.X("연도:N", title="발행연도", sort="ascending"),
        y=alt.Y("발행액(조원):Q", title="발행액(조원)"),
        color=alt.Color("issuer:N", title="은행"),
        tooltip=[
            alt.Tooltip("연도:N"), alt.Tooltip("issuer:N", title="은행"),
            alt.Tooltip("발행액(조원):Q", format=",.3f"),
            alt.Tooltip("발행건수:Q", format=",d"),
        ],
    ).properties(height=380, title="연도별 시중은행 FRN 발행액")
    st.altair_chart(chart, width="stretch", alt="시중은행의 연도별 FRN 발행액을 은행별로 쌓은 막대그래프")

    annual_total = (
        annual_bank.groupby("year")
        .agg(**{"발행액(조원)": ("발행액(조원)", "sum"), "발행건수": ("발행건수", "sum")})
        .reset_index().rename(columns={"year": "연도"})
    )
    st.dataframe(
        annual_total, hide_index=True, width="stretch",
        column_config={"발행액(조원)": st.column_config.NumberColumn(format="%.3f조")},
        alt="연도별 FRN 발행액과 발행건수",
    )

    st.subheader("은행별 누계")
    bank_total = (
        scope.groupby("issuer")
        .agg(발행건수=("isin_code", "nunique"), 발행액=("issue_amount", "sum"))
        .reset_index().rename(columns={"issuer": "은행"}).sort_values("발행액", ascending=False)
    )
    bank_total["발행액(조원)"] = bank_total.pop("발행액") / 1e12
    st.dataframe(
        bank_total, hide_index=True, width="stretch",
        column_config={"발행액(조원)": st.column_config.NumberColumn(format="%.3f조")},
        alt="은행별 FRN 누적 발행액과 건수",
    )


def render_quality(scope, selected_banks, report_path, rates, source_reports=None):
    st.header("데이터 품질")
    st.caption("FRN 후보 모집단과 원문 발행조건 확보 표본을 연도·은행별로 비교합니다.")
    if scope.empty:
        st.warning("선택 범위에 해당하는 FRN 후보가 없습니다.")
        return

    confirmed = scope[scope.condition_confirmed]
    candidate_amount = scope.issue_amount.sum()
    confirmed_amount = confirmed.issue_amount.sum()
    with st.container(horizontal=True):
        st.metric("전체 FRN 후보", f"{len(scope):,}건", border=True)
        st.metric("원문 조건 확인", f"{len(confirmed):,}건", border=True)
        st.metric("건수 확보율", f"{100*len(confirmed)/len(scope):.1f}%", border=True)
        st.metric("발행액 확보율", f"{100*confirmed_amount/candidate_amount if candidate_amount else 0:.1f}%", border=True)

    yearly = scope.groupby("year").agg(
        후보건수=("isin_code", "nunique"), 후보발행액=("issue_amount", "sum"),
        조건확인건수=("condition_confirmed", "sum"),
    ).reset_index()
    confirmed_amounts = confirmed.groupby("year").issue_amount.sum()
    yearly["조건확인발행액"] = yearly.year.map(confirmed_amounts).fillna(0)
    yearly["건수확보율"] = 100 * yearly.조건확인건수 / yearly.후보건수
    yearly["발행액확보율"] = 100 * yearly.조건확인발행액 / yearly.후보발행액
    yearly["상태"] = pd.cut(
        yearly.건수확보율, [-0.1, 49.999, 79.999, 100.1],
        labels=["미흡", "부분 확보", "양호"],
    ).astype(str)

    long = yearly.melt(
        id_vars=["year"], value_vars=["건수확보율", "발행액확보율"],
        var_name="기준", value_name="확보율",
    )
    long["연도"] = long.year.astype(str)
    chart = alt.Chart(long).mark_line(point=True).encode(
        x=alt.X("연도:N", title="발행연도", sort="ascending"),
        y=alt.Y("확보율:Q", title="확보율(%)", scale=alt.Scale(domain=[0, 100])),
        color=alt.Color("기준:N"),
        tooltip=["연도:N", "기준:N", alt.Tooltip("확보율:Q", format=".1f")],
    ).properties(height=300, title="연도별 최초 발행조건 확보율")
    st.altair_chart(chart, width="stretch", alt="연도별 FRN 후보 대비 원문 발행조건 건수와 발행액 확보율")

    table = yearly.rename(columns={"year": "연도"}).copy()
    table["후보발행액(조원)"] = table.pop("후보발행액") / 1e12
    table["조건확인발행액(조원)"] = table.pop("조건확인발행액") / 1e12
    st.dataframe(
        table, hide_index=True, width="stretch",
        column_config={
            "건수확보율": st.column_config.ProgressColumn(format="%.1f%%", min_value=0, max_value=100),
            "발행액확보율": st.column_config.ProgressColumn(format="%.1f%%", min_value=0, max_value=100),
            "후보발행액(조원)": st.column_config.NumberColumn(format="%.3f조"),
            "조건확인발행액(조원)": st.column_config.NumberColumn(format="%.3f조"),
        },
        alt="연도별 FRN 후보와 최초 발행조건 확보율",
    )
    zero_years = table[(table.후보건수 > 0) & (table.조건확인건수 == 0)].연도.tolist()
    if zero_years:
        st.warning(f'{", ".join(map(str, zero_years))}년은 FRN 후보가 있지만 원문 발행조건 확보가 0건입니다. 발행이 없었던 것이 아닙니다.')

    bank_year = scope.groupby(["year", "issuer"]).agg(
        후보건수=("isin_code", "nunique"), 조건확인건수=("condition_confirmed", "sum")
    ).reset_index()
    bank_year["확보율(%)"] = 100 * bank_year.조건확인건수 / bank_year.후보건수
    matrix = bank_year.pivot(index="year", columns="issuer", values="확보율(%)").reindex(columns=selected_banks)
    st.subheader("은행·연도별 건수 확보율")
    st.dataframe(
        matrix, width="stretch",
        column_config={bank: st.column_config.ProgressColumn(format="%.1f%%", min_value=0, max_value=100) for bank in matrix.columns},
        alt="은행과 연도별 최초 발행조건 건수 확보율",
    )

    details = st.expander("미확보 종목", on_change="rerun")
    if details.open:
        with details:
            missing = scope[~scope.condition_confirmed].copy()
            missing["미확보 사유"] = missing["missing_reason"]
            st.caption(f"현재 선택 범위 미확보 후보: {len(missing):,}건")
            reason_counts = missing["미확보 사유"].value_counts().rename_axis("사유").reset_index(name="종목수")
            st.dataframe(reason_counts, hide_index=True, width="stretch", alt="최초 발행조건 미확보 사유별 종목수")
            st.dataframe(
                missing[["issue_date", "issuer", "bond_name", "isin_code", "maturity_bucket", "frn_structure_label", "미확보 사유"]],
                hide_index=True, width="stretch", alt="최초 발행조건이 아직 확보되지 않은 FRN 후보",
            )

    sources = st.expander("원문 보고서 수집 현황", on_change="rerun")
    if sources.open:
        with sources:
            if source_reports is not None and not source_reports.empty:
                failed = (source_reports.status == "FAILED").sum()
                st.caption(f"DB 수집이력: 보고서 {len(source_reports):,}건 · 오류 {failed:,}건 · 파서 v{int(source_reports.parser_version.max())}")
                rows = source_reports[["report_date", "periods_json", "rows_parsed", "rows_skipped", "source_url", "error"]].rename(columns={
                    "report_date": "보고서일", "periods_json": "발행기간", "rows_parsed": "인식행",
                    "rows_skipped": "제외행", "source_url": "출처", "error": "오류"})
                st.dataframe(rows, hide_index=True, width="stretch",
                             column_config={"출처": st.column_config.LinkColumn("출처")},
                             alt="DB에 저장된 KIS 원문 보고서별 수집 기간과 오류")
            elif report_path.exists():
                report = json.loads(report_path.read_text(encoding="utf-8"))
                failed = sum("error" in row for row in report["reports"])
                st.caption(f"최근 수집: {report['completed_at']} · 보고서 {len(report['reports'])}건 · 오류 {failed}건")
                rows = pd.DataFrame([{
                    "보고서일": row["report_date"], "발행기간": str(row.get("periods", [])),
                    "출처": row["url"], "오류": row.get("error", ""),
                } for row in report["reports"]])
                st.dataframe(rows, hide_index=True, width="stretch", alt="KIS 원문 보고서별 수집 기간과 오류")
            else:
                st.info("원문 보고서 수집 이력이 없습니다.")

    reference = st.expander("CD91 일별 금리 현황", on_change="rerun")
    if reference.open:
        with reference:
            st.caption("CD91은 민평 CD 1개월과 다른 시계열이며 서로 대체하여 역산하지 않습니다.")
            if not rates.empty:
                frame = rates.copy()
                frame["date"] = pd.to_datetime(frame.rate_date, format="%Y%m%d")
                st.caption(f"CD91 {len(frame):,}건 · {frame.date.min():%Y-%m-%d} ~ {frame.date.max():%Y-%m-%d}")
                st.line_chart(frame.set_index("date")[["rate_value"]].rename(columns={"rate_value": "CD91(%)"}), alt="CD91 일별 금리 시계열")
