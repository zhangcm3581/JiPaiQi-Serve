#!/usr/bin/env python3
"""Run isolated acceptance/regression tests and render a standalone HTML report."""

import argparse
import hashlib
import json
import platform
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
TITLES = {
    "test_tenants_have_independent_versions_and_preserve_zeros": "租户版本隔离与前导零",
    "test_seven_hands_leave_exact_multiset_and_lock_without_advancing": "七份汇总守恒与结果锁定",
    "test_only_thirteen_cards_are_accepted": "严格校验13张手牌",
    "test_duplicate_rank_suit_is_preserved_but_third_copy_rejected": "双副牌同牌容量限制",
    "test_deck_overflow_rolls_back_last_submission": "超量上报事务回滚",
    "test_idempotent_ack_survives_restart_and_canonical_hand_order": "重启后去重与手牌顺序无关",
    "test_end_closes_incomplete_round_once_and_rejects_old_hand": "缺牌结束及迟到数据拒绝",
    "test_new_version_cannot_receive_old_or_unbound_data": "禁止旧牌换版本及未绑定上传",
    "test_unregistered_or_unjoined_device_cannot_end_round": "未登记或未绑定不能结束",
    "test_timeout_starts_at_first_valid_hand_and_is_fixed_across_restart": "首份计时、180秒超时与重启恢复",
    "test_manual_versions_are_forward_only_and_disable_is_persistent": "手动版本推进与租户停用",
    "test_device_limit_and_history_survives_reregistration": "设备上限与历史快照",
    "test_concurrent_retries_and_end_advance_once": "并发重复上传与结束",
    "test_concurrent_seventh_hand_and_end_never_modify_closed_history": "收齐与结束并发一致性",
    "test_version_exhaustion_closes_without_wrapping_or_blocking_other_tenants": "最大版本耗尽不回绕",
    "test_daily_summary_uses_shanghai_midnight": "北京时间今日指标",
    "test_pages_and_real_crud": "页面与租户管理接口",
    "test_strict_http_fields": "HTTP字段类型严格校验",
    "test_http_limits_and_origin": "HTTP大小与来源限制",
    "test_invalid_admin_message_does_not_break_subscription": "管理端错误消息可恢复",
    "test_binary_frames_close_cleanly": "二进制帧正确关闭",
    "test_live_seven_clients_result_and_history": "ASGI七端结果与管理端快照",
    "test_ws_reconnect_presence_and_strict_messages": "重连、在线状态与身份校验",
    "test_replaced_connection_and_unknown_tenant": "连接替换与未知租户",
    "test_expired_round_closed_before_message_after_restart": "重启先清理过期轮次",
    "test_http_version_boundary_returns_validation_error": "HTTP版本范围回归",
    "test_ws_version_boundary_rejects_without_disconnecting": "WS版本范围及连接存活回归",
    "test_success_backs_up_data_updates_code_and_restarts": "更新成功与数据库备份",
    "test_failed_update_restores_code_dependencies_and_service": "更新失败恢复代码及依赖",
    "test_no_changes_does_not_restart_service": "无更新不重启",
    "test_local_changes_are_not_overwritten": "更新保留本地修改",
    "test_diverged_branch_is_rejected_before_service_stop": "Git分叉拒绝自动更新",
    "test_backup_failure_restarts_original_service_without_changing_code": "备份失败恢复原服务",
    "test_update_lock_prevents_a_second_update": "更新锁防止并发部署",
}


def fingerprint():
    digest = hashlib.sha256()
    files = []
    for folder in ("app", "tests", "scripts"):
        for path in sorted((ROOT / folder).rglob("*")):
            if (
                path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix in (".py", ".sh", ".html", ".css", ".js")
            ):
                files.append(path)
    for path in files:
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(b"\0" + path.read_bytes())
    return digest.hexdigest(), len(files)


def cases_from(path):
    cases = []
    for node in ET.parse(path).getroot().iter("testcase"):
        props = {
            p.attrib["name"]: p.attrib.get("value", "")
            for p in node.findall("./properties/property")
        }
        name = node.attrib["name"]
        base = name.split("[")[0]
        suffix = name[len(base) :]
        failure = node.find("failure")
        error = node.find("error")
        bad = failure if failure is not None else error
        status = (
            "failed"
            if bad is not None
            else "skipped"
            if node.find("skipped") is not None
            else "passed"
        )
        category = props.get("category") or (
            "部署更新"
            if "update_script" in node.attrib.get("classname", "")
            else "版本边界回归"
            if "boundaries" in node.attrib.get("classname", "")
            else "协议集成回归"
            if "test_api" in node.attrib.get("classname", "")
            else "状态与数据回归"
        )
        cases.append(
            {
                "id": node.attrib.get("classname", "") + "::" + name,
                "title": props.get("title", TITLES.get(base, base)) + suffix,
                "category": category,
                "status": status,
                "seconds": float(node.attrib.get("time", 0)),
                "steps": props.get(
                    "steps", "运行对应回归用例，使用隔离测试数据检查断言。"
                ),
                "expected": props.get(
                    "expected", "符合用例中的数据、状态与错误码断言；无未捕获异常。"
                ),
                "evidence": json.loads(props.get("evidence", "[]")),
                "sent": int(props.get("wire_sent", "0")),
                "received": int(props.get("wire_received", "0")),
                "network": "wire_sent" in props,
                "failure": ""
                if bad is None
                else bad.text or bad.attrib.get("message", ""),
            }
        )
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="reports/test-report.html")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    xml = output.with_suffix(".xml")
    before, file_count = fingerprint()
    began = time.monotonic()
    checks = []
    for title, command in [
        (
            "Python静态检查",
            [sys.executable, "-m", "ruff", "check", "app", "tests", "scripts"],
        ),
        (
            "Python格式检查",
            [
                sys.executable,
                "-m",
                "ruff",
                "format",
                "--check",
                "app",
                "tests",
                "scripts",
            ],
        ),
        ("更新脚本语法", ["bash", "-n", "scripts/update.sh"]),
        ("完整自动测试", [sys.executable, "-m", "pytest", "-q", f"--junitxml={xml}"]),
    ]:
        print(f"正在执行：{title}", flush=True)
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
        checks.append(
            {
                "title": title,
                "command": " ".join(command),
                "code": result.returncode,
                "output": result.stdout + result.stderr,
            }
        )
        print(result.stdout, end="", flush=True)
        if result.stderr:
            print(result.stderr, end="", flush=True)
    after, _ = fingerprint()
    cases = cases_from(xml) if xml.exists() else []
    cases.sort(key=lambda c: (not c["network"], c["category"], c["title"]))
    baseline = ROOT / "reports/before-fixes.xml"
    baseline_cases = cases_from(baseline) if baseline.exists() else []
    baseline_failed = sum(c["status"] == "failed" for c in baseline_cases)
    totals = {
        key: sum(c["status"] == key for c in cases)
        for key in ("passed", "failed", "skipped")
    }
    passed = (
        bool(cases)
        and all(c["code"] == 0 for c in checks)
        and before == after
        and not totals["failed"]
        and not totals["skipped"]
    )
    metrics = []
    for case in cases:
        for item in case["evidence"]:
            for key, value in item.items():
                if key in ("result_latency_ms", "max_result_latency_ms"):
                    metrics.append({"title": case["title"], "value": value})
    data = {
        "generated_at": datetime.now(ZoneInfo("Asia/Shanghai")).strftime(
            "%Y-%m-%d %H:%M:%S UTC+08:00"
        ),
        "environment": f"{platform.system()} {platform.release()} · Python {platform.python_version()} · SQLite {sqlite3.sqlite_version}",
        "dependencies": {
            name: version(name)
            for name in ("fastapi", "uvicorn", "websockets", "pytest")
        },
        "source_hash": after,
        "source_unchanged": before == after,
        "file_count": file_count,
        "seconds": round(time.monotonic() - began, 2),
        "passed": passed,
        "totals": totals,
        "cases": cases,
        "checks": checks,
        "metrics": metrics,
        "network_count": sum(c["network"] for c in cases),
        "sent": sum(c["sent"] for c in cases),
        "received": sum(c["received"] for c in cases),
        "baseline_failed": baseline_failed,
        "categories": sorted({c["category"] for c in cases}),
        "boundary_passed": sum(
            c["category"] == "版本边界回归" and c["status"] == "passed" for c in cases
        ),
    }
    output.with_suffix(".json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2)
    )
    assets = ROOT / "scripts/report"
    env = Environment(
        loader=FileSystemLoader(assets), autoescape=select_autoescape(["html"])
    )
    output.write_text(
        env.get_template("report.html").render(
            **data,
            css=(assets / "report.css").read_text(),
            js=(assets / "report.js").read_text(),
        )
    )
    print(f"\n报告：{output}\n结果：{totals}", flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
