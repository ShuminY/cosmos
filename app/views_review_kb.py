"""审图知识库管理页面 - 查看/编辑审图规则与人工案例知识库。"""
from __future__ import annotations
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import drawing_rag
from src.time_utils import format_beijing

RULES_WORKBOOK_NAME = "审图依据-审查规则库.xlsx"

# 规则中较长的文本字段用 text_area，其余用 text_input
_RULE_LONG_FIELDS = {"content", "method", "criteria", "example", "ai_logic", "suggestion"}


def _get_visible_projects():
    from views import visible_projects
    return visible_projects(st.session_state["user"]["id"])


def _project_selector() -> object | None:
    """顶部项目选择器，返回选中的 Project."""
    projects = _get_visible_projects()
    if not projects:
        st.info("您还没有任何项目。请先创建一个项目。")
        return None
    names = [p.name for p in projects]
    idx = st.selectbox(
        "选择项目",
        range(len(projects)),
        format_func=lambda i: names[i],
        key="review_kb_project_selector",
    )
    return projects[idx]


# ============ 规则管理 ============
def _render_rule_form(project_id: int, rule: dict | None, key_prefix: str):
    """规则新增/编辑表单。rule 为 None 时是新增."""
    is_edit = rule is not None
    with st.form(key=f"{key_prefix}_form", clear_on_submit=not is_edit):
        values = {}
        for attr, label in drawing_rag.RULE_FIELDS:
            default = rule.get(attr, "") if rule else ""
            if attr in _RULE_LONG_FIELDS:
                values[attr] = st.text_area(label, value=default, height=80, key=f"{key_prefix}_{attr}")
            else:
                values[attr] = st.text_input(label, value=default, key=f"{key_prefix}_{attr}")

        submitted = st.form_submit_button("💾 保存" if is_edit else "➕ 新增规则", type="primary")

    if submitted:
        if not values.get("rule_code", "").strip():
            st.warning("规则编号不能为空。")
            return
        with st.spinner("正在保存并向量化..."):
            if is_edit:
                drawing_rag.update_rule(rule["id"], values)
            else:
                drawing_rag.create_rule(project_id, values)
        st.success("已保存并完成向量化。")
        st.rerun()


def _render_rules_tab(project):
    rule_count = drawing_rag.count_rules(project.id)

    col_a, col_b, col_c = st.columns([1, 1, 2])
    col_a.metric("审图规则数", rule_count)
    with col_c:
        xlsx_path = Path("data") / "projects" / str(project.id) / RULES_WORKBOOK_NAME
        has_xlsx = xlsx_path.exists()
        st.caption("⚠️ 从 Excel 同步会覆盖当前项目全部规则（含手动新增/编辑）。")
        confirm = st.checkbox("确认覆盖式同步", key=f"confirm_sync_{project.id}")
        if st.button(
            "🔄 从 Excel 覆盖同步",
            key=f"sync_rules_kb_{project.id}",
            disabled=not (has_xlsx and confirm),
            help=None if has_xlsx else f"未找到规则库文件：{xlsx_path}",
        ):
            with st.spinner("正在从 Excel 导入规则并向量化..."):
                count, err = drawing_rag.import_rules_from_xlsx(project.id)
            if err:
                st.error(f"同步失败：{err}")
            else:
                st.success(f"已同步 {count} 条审图规则。")
                st.rerun()

    st.divider()

    with st.expander("➕ 新增规则", expanded=(rule_count == 0)):
        _render_rule_form(project.id, None, key_prefix=f"new_rule_{project.id}")

    st.divider()

    rules = drawing_rag.list_rules(project.id)
    if not rules:
        st.info("暂无规则。请点击上方「新增规则」或从 Excel 同步。")
        return

    search = st.text_input("🔍 过滤规则（按编号/分类/审查项）", key=f"rule_filter_{project.id}")
    if search:
        s = search.strip().lower()
        rules = [
            r for r in rules
            if s in (r.get("rule_code", "") + r.get("category_l1", "")
                     + r.get("category_l2", "") + r.get("check_item", "")).lower()
        ]

    st.caption(f"共 {len(rules)} 条")
    for rule in rules:
        title = f"[{rule['rule_code']}] {rule.get('category_l1', '')} / {rule.get('check_item', '')}"
        with st.expander(title, expanded=False):
            edit_key = f"editing_rule_{rule['id']}"
            if st.session_state.get(edit_key):
                _render_rule_form(project.id, rule, key_prefix=f"edit_rule_{rule['id']}")
                if st.button("取消编辑", key=f"cancel_edit_rule_{rule['id']}"):
                    st.session_state[edit_key] = False
                    st.rerun()
            else:
                for attr, label in drawing_rag.RULE_FIELDS:
                    value = rule.get(attr, "")
                    if value:
                        st.markdown(f"**{label}：** {value}")
                c1, c2, _ = st.columns([1, 1, 4])
                if c1.button("✏️ 编辑", key=f"edit_btn_rule_{rule['id']}"):
                    st.session_state[edit_key] = True
                    st.rerun()
                if c2.button("🗑️ 删除", key=f"del_btn_rule_{rule['id']}"):
                    drawing_rag.delete_rule(rule["id"])
                    st.success("已删除。")
                    st.rerun()


# ============ 案例管理 ============
_VERDICT_OPTIONS = ["manual", "confirmed"]
_VERDICT_CN = {"manual": "人工批注", "confirmed": "人工确认成立"}


def _render_case_form(case: dict, key_prefix: str):
    """案例编辑表单。"""
    with st.form(key=f"{key_prefix}_form"):
        rule_code = st.text_input("规则编号", value=case.get("rule_code", ""), key=f"{key_prefix}_rule_code")
        location = st.text_input("部位", value=case.get("location", ""), key=f"{key_prefix}_location")
        problem = st.text_area("问题描述", value=case.get("problem", ""), height=80, key=f"{key_prefix}_problem")
        suggestion = st.text_area("建议", value=case.get("suggestion", ""), height=80, key=f"{key_prefix}_suggestion")
        c1, c2 = st.columns(2)
        severity = c1.text_input("严重程度", value=case.get("severity", ""), key=f"{key_prefix}_severity")
        cur_verdict = case.get("verdict", "manual")
        verdict = c2.selectbox(
            "类型",
            _VERDICT_OPTIONS,
            index=_VERDICT_OPTIONS.index(cur_verdict) if cur_verdict in _VERDICT_OPTIONS else 0,
            format_func=lambda v: _VERDICT_CN.get(v, v),
            key=f"{key_prefix}_verdict",
        )
        text = st.text_area("整合文本（用于展示/向量化）", value=case.get("text", ""), height=100, key=f"{key_prefix}_text")
        submitted = st.form_submit_button("💾 保存", type="primary")

    if submitted:
        drawing_rag.update_case(case["id"], {
            "rule_code": rule_code, "location": location, "problem": problem,
            "suggestion": suggestion, "severity": severity, "verdict": verdict,
            "text": text,
        })
        st.success("已保存并重新向量化。")
        st.rerun()


def _render_cases_tab(project):
    case_count = drawing_rag.count_cases(project.id)
    st.metric("人工案例数", case_count)
    st.caption("人工案例由图纸审核时的批注/确认自动向量化入库，也可在此手动编辑或删除。")

    st.divider()

    cases = drawing_rag.list_cases(project.id)
    if not cases:
        st.info("暂无人工案例。在图纸审核结果中新增批注或点击「✅ 对」即可自动入库。")
        return

    search = st.text_input("🔍 过滤案例（按编号/部位/问题）", key=f"case_filter_{project.id}")
    if search:
        s = search.strip().lower()
        cases = [
            c for c in cases
            if s in (c.get("rule_code", "") + c.get("location", "")
                     + c.get("problem", "") + c.get("text", "")).lower()
        ]

    st.caption(f"共 {len(cases)} 条")
    for case in cases:
        verdict_cn = _VERDICT_CN.get(case.get("verdict", ""), case.get("verdict", ""))
        time_str = format_beijing(case["created_at"], "%Y-%m-%d %H:%M") if case.get("created_at") else ""
        loc = case.get("location") or "未指定部位"
        title = f"[{case.get('rule_code') or '无编号'}] {verdict_cn} · {loc}"
        with st.expander(title, expanded=False):
            edit_key = f"editing_case_{case['id']}"
            if st.session_state.get(edit_key):
                _render_case_form(case, key_prefix=f"edit_case_{case['id']}")
                if st.button("取消编辑", key=f"cancel_edit_case_{case['id']}"):
                    st.session_state[edit_key] = False
                    st.rerun()
            else:
                if case.get("problem"):
                    st.markdown(f"**问题：** {case['problem']}")
                if case.get("suggestion"):
                    st.markdown(f"**建议：** {case['suggestion']}")
                if case.get("severity"):
                    st.markdown(f"**严重程度：** {case['severity']}")
                if case.get("text"):
                    st.markdown(f"**整合文本：** {case['text']}")
                meta = []
                if case.get("author"):
                    meta.append(f"批注人：{case['author']}")
                if case.get("page_num"):
                    meta.append(f"页码：第 {case['page_num']} 页")
                if time_str:
                    meta.append(time_str)
                if meta:
                    st.caption(" · ".join(meta))
                c1, c2, _ = st.columns([1, 1, 4])
                if c1.button("✏️ 编辑", key=f"edit_btn_case_{case['id']}"):
                    st.session_state[edit_key] = True
                    st.rerun()
                if c2.button("🗑️ 删除", key=f"del_btn_case_{case['id']}"):
                    drawing_rag.delete_case(case["id"])
                    st.success("已删除。")
                    st.rerun()


# ============ 主入口 ============
def view_review_knowledge_base():
    """审图知识库管理主页面（规则 + 人工案例）."""
    st.title("📚 审图知识库")
    st.caption("管理审图规则库与人工审图案例；所有改动会自动向量化，供图纸审核 RAG 检索。")

    project = _project_selector()
    if not project:
        return

    tab_rules, tab_cases = st.tabs(["📏 审图规则", "🧑‍⚖️ 人工案例"])
    with tab_rules:
        _render_rules_tab(project)
    with tab_cases:
        _render_cases_tab(project)
