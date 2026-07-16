"""图纸审核 RAG 核心。

- 审图规则以数据库为唯一数据源，可从项目下的 Excel 规则库导入并向量化。
- 人工审图案例（人工确认/批注）自动向量化入库，参与后续审核检索。
- 复用 src/kb.py 的轻量 hash embedding 与 cosine 相似度，零新增依赖。
"""
from __future__ import annotations
from pathlib import Path

import numpy as np
from sqlalchemy import select

from .db import session, ReviewRule, ReviewCase
from .kb import embed_texts, embedding_to_json, json_to_embedding, cosine_similarity
from .time_utils import now_utc

ROOT = Path(__file__).resolve().parent.parent

# 与 views_drawings 保持一致的规则库文件名
RULES_WORKBOOK_NAME = "审图依据-审查规则库.xlsx"
RULES_SHEET_NAME = "审图规则库"

# xlsx 表头 -> ReviewRule 字段
_RULE_COLUMN_MAP = {
    "规则编号": "rule_code",
    "一级分类(审查大类)": "category_l1",
    "二级分类": "category_l2",
    "审查项(审什么)": "check_item",
    "审查内容说明": "content",
    "如何审核(方法/步骤)": "method",
    "判定标准(命中即提疑)": "criteria",
    "涉及专业": "disciplines",
    "涉及图纸": "drawings",
    "典型问题示例(真实)": "example",
    "AI判定逻辑": "ai_logic",
    "严重程度": "severity",
    "处理建议": "suggestion",
}


# ============ 向量化文本构造 ============
def _rule_embedding_text(rule: dict | ReviewRule) -> str:
    """把规则里最能表达语义的字段拼成待向量化文本."""
    def g(key_attr):
        if isinstance(rule, dict):
            return rule.get(key_attr, "") or ""
        return getattr(rule, key_attr, "") or ""

    if isinstance(rule, dict):
        parts = [
            rule.get("category_l1", ""), rule.get("category_l2", ""),
            rule.get("check_item", ""), rule.get("content", ""),
            rule.get("criteria", ""), rule.get("ai_logic", ""),
            rule.get("disciplines", ""), rule.get("drawings", ""),
        ]
    else:
        parts = [
            rule.category_l1, rule.category_l2, rule.check_item, rule.content,
            rule.criteria, rule.ai_logic, rule.disciplines, rule.drawings,
        ]
    return " ".join(str(p) for p in parts if p)


def _case_embedding_text(location: str, problem: str, suggestion: str,
                         rule_code: str, severity: str) -> str:
    parts = [rule_code, severity, location, problem, suggestion]
    return " ".join(str(p) for p in parts if p)


# ============ 规则导入 (Excel -> DB + 向量化) ============
def import_rules_from_xlsx(project_id: int, xlsx_path: Path | None = None) -> tuple[int, str | None]:
    """从项目 Excel 规则库导入规则到数据库并向量化。

    覆盖式导入：先停用该项目旧规则再写入新规则。
    返回 (导入条数, 错误信息)。
    """
    from app.views_drawings import _read_xlsx_sheet_rows  # 复用现有解析

    if xlsx_path is None:
        xlsx_path = ROOT / "data" / "projects" / str(project_id) / RULES_WORKBOOK_NAME
    if not Path(xlsx_path).exists():
        return 0, f"未找到审图规则库：{xlsx_path}"

    try:
        rows = _read_xlsx_sheet_rows(Path(xlsx_path), RULES_SHEET_NAME)
    except Exception as e:
        return 0, f"读取审图规则库失败：{e}"

    header_idx = next((i for i, row in enumerate(rows) if "规则编号" in row), None)
    if header_idx is None:
        return 0, "审图规则库中未找到表头“规则编号”"

    headers = rows[header_idx]
    parsed: list[dict] = []
    for row in rows[header_idx + 1:]:
        if not any(row):
            continue
        raw = {headers[i]: (row[i] if i < len(row) else "") for i in range(len(headers)) if headers[i]}
        if not raw.get("规则编号"):
            continue
        mapped = {field: raw.get(col, "") for col, field in _RULE_COLUMN_MAP.items()}
        parsed.append(mapped)

    if not parsed:
        return 0, "规则库中没有可导入的规则行"

    # 向量化（session 外，避免长事务）
    embeddings = embed_texts([_rule_embedding_text(r) for r in parsed])

    with session() as s:
        old = s.execute(
            select(ReviewRule).where(ReviewRule.project_id == project_id)
        ).scalars().all()
        for r in old:
            s.delete(r)

        for i, r in enumerate(parsed):
            s.add(ReviewRule(
                project_id=project_id,
                rule_code=r.get("rule_code", ""),
                category_l1=r.get("category_l1"),
                category_l2=r.get("category_l2"),
                check_item=r.get("check_item"),
                content=r.get("content"),
                method=r.get("method"),
                criteria=r.get("criteria"),
                disciplines=r.get("disciplines"),
                drawings=r.get("drawings"),
                example=r.get("example"),
                ai_logic=r.get("ai_logic"),
                severity=r.get("severity"),
                suggestion=r.get("suggestion"),
                embedding=embedding_to_json(embeddings[i]),
                source=RULES_WORKBOOK_NAME,
                is_active=True,
            ))
        s.commit()

    return len(parsed), None


def count_rules(project_id: int) -> int:
    with session() as s:
        return len(s.execute(
            select(ReviewRule.id).where(
                ReviewRule.project_id == project_id,
                ReviewRule.is_active.is_(True),
            )
        ).all())


def count_cases(project_id: int) -> int:
    with session() as s:
        return len(s.execute(
            select(ReviewCase.id).where(ReviewCase.project_id == project_id)
        ).all())


# ============ 规则 CRUD（管理页面用） ============
# ReviewRule 字段 -> 展示用中文名（顺序即表单/展示顺序）
RULE_FIELDS = [
    ("rule_code", "规则编号"),
    ("category_l1", "一级分类(审查大类)"),
    ("category_l2", "二级分类"),
    ("check_item", "审查项(审什么)"),
    ("content", "审查内容说明"),
    ("method", "如何审核(方法/步骤)"),
    ("criteria", "判定标准(命中即提疑)"),
    ("disciplines", "涉及专业"),
    ("drawings", "涉及图纸"),
    ("example", "典型问题示例(真实)"),
    ("ai_logic", "AI判定逻辑"),
    ("severity", "严重程度"),
    ("suggestion", "处理建议"),
]


def _rule_to_dict(rule: ReviewRule) -> dict:
    d = {attr: getattr(rule, attr) or "" for attr, _ in RULE_FIELDS}
    d["id"] = rule.id
    d["is_active"] = rule.is_active
    d["source"] = rule.source or ""
    return d


def list_rules(project_id: int, include_inactive: bool = False) -> list[dict]:
    """列出项目全部规则（管理页面用），按规则编号排序."""
    with session() as s:
        stmt = select(ReviewRule).where(ReviewRule.project_id == project_id)
        if not include_inactive:
            stmt = stmt.where(ReviewRule.is_active.is_(True))
        rows = s.execute(stmt.order_by(ReviewRule.rule_code)).scalars().all()
        return [_rule_to_dict(r) for r in rows]


def get_rule(rule_id: int) -> dict | None:
    with session() as s:
        rule = s.get(ReviewRule, rule_id)
        return _rule_to_dict(rule) if rule else None


def create_rule(project_id: int, fields: dict) -> int:
    """新增一条规则并向量化入库，返回 rule id."""
    emb = embed_texts([_rule_embedding_text(fields)])[0]
    with session() as s:
        rule = ReviewRule(
            project_id=project_id,
            embedding=embedding_to_json(emb),
            source="manual",
            is_active=True,
            **{attr: fields.get(attr) for attr, _ in RULE_FIELDS},
        )
        s.add(rule)
        s.flush()
        rule_id = rule.id
        s.commit()
        return rule_id


def update_rule(rule_id: int, fields: dict) -> bool:
    """更新规则字段并重新向量化，返回是否成功."""
    with session() as s:
        rule = s.get(ReviewRule, rule_id)
        if not rule:
            return False
        for attr, _ in RULE_FIELDS:
            if attr in fields:
                setattr(rule, attr, fields[attr])
        emb = embed_texts([_rule_embedding_text(rule)])[0]
        rule.embedding = embedding_to_json(emb)
        rule.updated_at = now_utc()
        s.commit()
        return True


def delete_rule(rule_id: int) -> bool:
    with session() as s:
        rule = s.get(ReviewRule, rule_id)
        if not rule:
            return False
        s.delete(rule)
        s.commit()
        return True


# ============ 案例 CRUD（管理页面用） ============
def _case_to_dict(case: ReviewCase) -> dict:
    return {
        "id": case.id,
        "source_document_id": case.source_document_id,
        "page_num": case.page_num,
        "rule_code": case.rule_code or "",
        "location": case.location or "",
        "problem": case.problem or "",
        "suggestion": case.suggestion or "",
        "severity": case.severity or "",
        "verdict": case.verdict or "",
        "author": case.author or "",
        "text": case.text or "",
        "created_at": case.created_at,
    }


def list_cases(project_id: int) -> list[dict]:
    """列出项目全部人工案例（管理页面用），按创建时间倒序."""
    with session() as s:
        rows = s.execute(
            select(ReviewCase).where(ReviewCase.project_id == project_id)
            .order_by(ReviewCase.created_at.desc())
        ).scalars().all()
        return [_case_to_dict(c) for c in rows]


def update_case(case_id: int, fields: dict) -> bool:
    """更新案例字段并重新向量化，返回是否成功."""
    editable = ["rule_code", "location", "problem", "suggestion", "severity", "verdict", "text"]
    with session() as s:
        case = s.get(ReviewCase, case_id)
        if not case:
            return False
        for attr in editable:
            if attr in fields:
                setattr(case, attr, fields[attr])
        embed_source = _case_embedding_text(
            case.location or "", case.problem or case.text or "",
            case.suggestion or "", case.rule_code or "", case.severity or "",
        ) or (case.text or "")
        case.embedding = embedding_to_json(embed_texts([embed_source])[0])
        s.commit()
        return True


def delete_case(case_id: int) -> bool:
    with session() as s:
        case = s.get(ReviewCase, case_id)
        if not case:
            return False
        s.delete(case)
        s.commit()
        return True


# ============ 检索 ============
def _search(rows, embeddings_attr: str, query: str, top_k: int) -> list:
    """通用向量检索，返回按相似度降序的 (对象, score) 列表."""
    if not rows:
        return []
    query_emb = embed_texts([query])[0]
    expected_dim = len(query_emb)

    valid_rows = []
    valid_embs = []
    for row in rows:
        raw = getattr(row, embeddings_attr, None)
        if not raw:
            continue
        emb = json_to_embedding(raw)
        if len(emb) == expected_dim:
            valid_rows.append(row)
            valid_embs.append(emb)

    if not valid_rows:
        return []

    scores = cosine_similarity(query_emb, np.array(valid_embs))
    order = np.argsort(-scores)[:top_k]
    return [(valid_rows[i], float(scores[i])) for i in order]


def search_rules(project_id: int, query: str, top_k: int = 15) -> list[dict]:
    """检索最相关的审图规则，返回可用于提示词的 dict 列表."""
    with session() as s:
        rows = s.execute(
            select(ReviewRule).where(
                ReviewRule.project_id == project_id,
                ReviewRule.is_active.is_(True),
            )
        ).scalars().all()
        hits = _search(rows, "embedding", query, top_k)
        results = []
        for rule, score in hits:
            results.append({
                "规则编号": rule.rule_code or "",
                "一级分类(审查大类)": rule.category_l1 or "",
                "二级分类": rule.category_l2 or "",
                "审查项(审什么)": rule.check_item or "",
                "审查内容说明": rule.content or "",
                "如何审核(方法/步骤)": rule.method or "",
                "判定标准(命中即提疑)": rule.criteria or "",
                "涉及专业": rule.disciplines or "",
                "涉及图纸": rule.drawings or "",
                "AI判定逻辑": rule.ai_logic or "",
                "严重程度": rule.severity or "",
                "处理建议": rule.suggestion or "",
                "score": score,
            })
        return results


def search_cases(project_id: int, query: str, top_k: int = 6) -> list[dict]:
    """检索最相关的人工审图案例."""
    with session() as s:
        rows = s.execute(
            select(ReviewCase).where(ReviewCase.project_id == project_id)
        ).scalars().all()
        hits = _search(rows, "embedding", query, top_k)
        results = []
        for case, score in hits:
            results.append({
                "rule_code": case.rule_code or "",
                "location": case.location or "",
                "problem": case.problem or "",
                "suggestion": case.suggestion or "",
                "severity": case.severity or "",
                "verdict": case.verdict or "",
                "author": case.author or "",
                "text": case.text or "",
                "score": score,
            })
        return results


def get_all_rules(project_id: int) -> list[dict]:
    """取全部规则（RAG 检索为空时回退用）."""
    with session() as s:
        rows = s.execute(
            select(ReviewRule).where(
                ReviewRule.project_id == project_id,
                ReviewRule.is_active.is_(True),
            ).order_by(ReviewRule.rule_code)
        ).scalars().all()
        return [{
            "规则编号": r.rule_code or "",
            "一级分类(审查大类)": r.category_l1 or "",
            "二级分类": r.category_l2 or "",
            "审查项(审什么)": r.check_item or "",
            "审查内容说明": r.content or "",
            "如何审核(方法/步骤)": r.method or "",
            "判定标准(命中即提疑)": r.criteria or "",
            "涉及专业": r.disciplines or "",
            "涉及图纸": r.drawings or "",
            "AI判定逻辑": r.ai_logic or "",
            "严重程度": r.severity or "",
            "处理建议": r.suggestion or "",
        } for r in rows]


# ============ 人工案例入库（自动向量化） ============
def add_review_case(project_id: int, text: str,
                    source_document_id: int | None = None,
                    page_num: int | None = None,
                    rule_code: str | None = None,
                    location: str | None = None,
                    problem: str | None = None,
                    suggestion: str | None = None,
                    severity: str | None = None,
                    verdict: str = "manual",
                    author: str | None = None) -> int | None:
    """新增一条人工审图案例并自动向量化入库，返回 case id."""
    text = (text or "").strip()
    if not text:
        return None

    embed_source = _case_embedding_text(
        location or "", problem or text, suggestion or "",
        rule_code or "", severity or "",
    ) or text
    emb = embed_texts([embed_source])[0]

    with session() as s:
        case = ReviewCase(
            project_id=project_id,
            source_document_id=source_document_id,
            page_num=page_num,
            rule_code=rule_code,
            location=location,
            problem=problem,
            suggestion=suggestion,
            severity=severity,
            verdict=verdict,
            author=author,
            text=text,
            embedding=embedding_to_json(emb),
            created_at=now_utc(),
        )
        s.add(case)
        s.flush()
        case_id = case.id
        s.commit()
        return case_id


# ============ Markdown 渲染（拼进提示词） ============
def rules_to_markdown(rules: list[dict]) -> str:
    """将检索出的规则转换为提示词用 Markdown."""
    if not rules:
        return "（未检索到匹配规则，请基于通用审图经验并谨慎判定。）"
    lines = []
    for rule in rules:
        score = rule.get("score")
        score_tag = f"（相关度{int(score * 100)}%）" if isinstance(score, (int, float)) else ""
        lines.append(
            "- "
            f"[{rule.get('规则编号', '')}] {score_tag}"
            f"{rule.get('一级分类(审查大类)', '')} / {rule.get('二级分类', '')} / "
            f"{rule.get('审查项(审什么)', '')}\n"
            f"  - 审查内容：{rule.get('审查内容说明', '')}\n"
            f"  - 如何审核：{rule.get('如何审核(方法/步骤)', '')}\n"
            f"  - 判定标准：{rule.get('判定标准(命中即提疑)', '')}\n"
            f"  - 涉及专业/图纸：{rule.get('涉及专业', '')}；{rule.get('涉及图纸', '')}\n"
            f"  - AI判定逻辑：{rule.get('AI判定逻辑', '')}\n"
            f"  - 严重程度：{rule.get('严重程度', '')}；处理建议：{rule.get('处理建议', '')}"
        )
    return "\n".join(lines)


def cases_to_markdown(cases: list[dict]) -> str:
    """将检索出的人工案例转换为提示词用 Markdown."""
    if not cases:
        return "（暂无相关人工审图案例。）"
    lines = []
    for case in cases:
        score = case.get("score")
        score_tag = f"（相关度{int(score * 100)}%）" if isinstance(score, (int, float)) else ""
        rule_code = case.get("rule_code") or "无编号"
        severity = case.get("severity") or ""
        location = case.get("location") or ""
        problem = case.get("problem") or case.get("text") or ""
        suggestion = case.get("suggestion") or ""
        verdict = case.get("verdict") or ""
        verdict_cn = {"confirmed": "人工确认成立", "manual": "人工批注"}.get(verdict, verdict)
        line = f"- [{rule_code}] {score_tag}{severity} {verdict_cn}"
        if location:
            line += f"\n  - 部位：{location}"
        if problem:
            line += f"\n  - 问题：{problem}"
        if suggestion:
            line += f"\n  - 建议：{suggestion}"
        lines.append(line)
    return "\n".join(lines)
