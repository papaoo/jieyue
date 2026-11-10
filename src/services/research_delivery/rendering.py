"""Deterministic readable historical drafts. Frozen risks never depend on model prose."""
import re


def _plain(value):
    return re.sub(r"([\\`*_{}\[\]<>#])", r"\\\1", str(value)).replace("\n", " ")


def render_draft(envelope, llm):
    lines = ["# 阶跃 · 历史研究草稿", "数据日期未核实；W1 未验收，不能正式发布。非投资建议。",
             f"来源：{_plain(envelope['source_run_id'])}；原始排序：{_plain(envelope['ranking_mode'])}",
             "阻断项：" + ", ".join(envelope["blockers"]),
             f"源错误：{envelope['source_errors']['count']}；AI：{_plain(llm['status'])}"]
    if llm.get("reason"):
        lines.append("AI 未采用原因：" + _plain(llm["reason"]))
    model_rows = (llm.get("explanation") or {}).get("candidates", [])
    for index, pick in enumerate(envelope["candidates"]):
        lines.append(f"\n## {_plain(pick['code'])} {_plain(pick['name'])}（来源名次 {pick['rank']}）")
        metrics = [f"{key}={value}" for key, value in pick["metrics"].items() if value is not None]
        lines.append("保存指标（非当日收据）：" + (", ".join(metrics) if metrics else "未知"))
        for label in ("why_selected", "why_now"):
            for reason in pick[label]:
                lines.append(f"保存解释/{label} [{_plain(reason.get('source', 'unknown'))}/{_plain(reason.get('quality', 'unknown'))}]：{_plain(reason.get('text', ''))}")
        lines.append("原始风险（冻结；旧模型文本仅为推断）：" + ("；".join(_plain(risk["text"]) for risk in pick["risks"]) or "风险信息未知"))
        if pick["missing"]:
            lines.append("缺口：" + ", ".join(pick["missing"]))
        if index < len(model_rows):
            for reason in model_rows[index]["reasons"]:
                label = "证据原文/保存指标" if reason["kind"] == "fact" else "推断（待人工核实）"
                lines.append(f"AI/{label}：{_plain(reason['text'])} [指标：{','.join(reason['metric_refs'])}；证据：{','.join(reason['evidence_ids'])}]")
    lines.append("\n## 原始整体风险")
    lines.extend(_plain(risk["text"]) for risk in envelope["risks"])
    if envelope["missing"]:
        lines.append("整体缺口：" + ", ".join(envelope["missing"]))
    lines.append("\n## 存储证据（日期和归属分别披露）")
    for entry in envelope["evidence"]:
        label = _plain(entry["title"])
        link = f"[{label}]({entry['url']})" if entry["url"] else label + "（无安全公开链接）"
        lines.append(f"{_plain(entry['id'])} {link}；{entry['issuer_status']}；published={_plain(entry['published_at'])}；fetched={_plain(entry['fetched_at'])}；{entry['date_status']}：{_plain(entry['summary'])}")
    if not envelope["evidence"]:
        lines.append("暂无可匹配的已存储发行人证据；未执行补采。")
    return "\n\n".join(lines)
