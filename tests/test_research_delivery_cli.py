"""LOCAL CLI tests: all stock names and facts are synthetic offline fixtures."""
import copy
import importlib
import json
import os
from datetime import date, datetime
from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest

from src.services.screening.config import Config
from src.services.screening.strategy import list_strategies
from tests.test_screening_compute_only import forbid_remote


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "research_delivery_preview.py"
CORE = ("name", "price", "amount", "total_mv", "pe_ratio", "pb_ratio",
        "turnover_rate", "volume_ratio", "change_pct")


def bundle():
    return dict(schema_version=1, trade_date="2026-10-09", calendar_verified=True,
                universe_verified=True, universe=["600001", "600002"],
                snapshot=dict(columns=["code", *CORE, "business_date"], index=[7, 7],
                              data=[[code, "合成离线测试股票", 10., 1e9, 1e10, 8., 1., 2., 1.2, 1., "2026-10-09"]
                                    for code in ("600001", "600002")],
                              attrs=dict(snapshot_source="synthetic-offline", evidence_id="synthetic-batch",
                                         units_profile="cn_equity_cny_v1", pe_ratio_basis="ttm",
                                         field_trade_date_columns={field: "business_date" for field in CORE})),
                daily_histories={})


def cli():
    return importlib.import_module("scripts.research_delivery_preview")


def run_bundle(tmp_path, capsys, value=None, strategy="dual_low", extra=()):
    path = tmp_path / "冻结 输入.json"
    path.write_text(json.dumps(bundle() if value is None else value, ensure_ascii=False), encoding="utf-8")
    status = cli().main(["preview", "--bundle", str(path), "--strategy", strategy, *extra])
    output = capsys.readouterr()
    return status, json.loads(output.out), output


def test_catalog_uses_actual_ten_strategies_without_env(monkeypatch, capsys):
    forbid_remote(monkeypatch)
    assert cli().main(["catalog"]) == 0
    actual = json.loads(capsys.readouterr().out)
    expected = list_strategies(Config().strategies_dir)
    assert len(actual) == len(expected) == 10
    assert actual == [dict(name=info.name, display_name=info.display_name, version=info.version,
                           requires_daily_features=info.requires_daily_features,
                           required_daily_fields=info.required_daily_fields) for info in expected]


def test_real_dual_low_is_preview_only_with_numeric_facts(monkeypatch, tmp_path, capsys):
    forbidden = forbid_remote(monkeypatch)
    import socket
    monkeypatch.setattr(socket, "create_connection", forbidden)
    import src.services.screening.config as config_module
    monkeypatch.setattr(config_module, "_load_env_file", forbidden)
    monkeypatch.setenv("STRATEGIES_DIR", "secret-not-a-config-source")
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "secret-webhook-must-not-surface")
    status, result, output = run_bundle(tmp_path, capsys)
    assert status == 0 and result["readiness"]["state"] == "degraded"
    assert result["trade_date"] == "2026-10-09"
    assert result["preview_only"] is True and result["publish_allowed"] is False
    assert result["scope"]["universe"] == {"count": 2, "codes": ["600001", "600002"]}
    assert result["scope"]["technical_planned"] == {"count": 0, "codes": []}
    assert len(result["result"]["picks"]) == 2
    assert result["readiness"]["valid_count"] == result["readiness"]["required_count"]
    assert result["result"]["llm_ranked"] is False and result["result"]["saved_path"] == ""
    for pick in result["result"]["picks"]:
        assert pick["name"] == "合成离线测试股票" and pick["price"] == 10.
        assert pick["amount"] == 1e9 and pick["pe_ratio"] == 8.
        assert pick["post_analysis_status"]["scorecard"] == "completed"
    assert "secret-" not in output.out + output.err
    assert not output.err


def test_json_boolean_numeric_cell_is_preserved_and_blocks_preview(monkeypatch, tmp_path, capsys):
    forbid_remote(monkeypatch)
    value = bundle()
    price_column = value["snapshot"]["columns"].index("price")
    value["snapshot"]["data"][0][price_column] = True
    path = tmp_path / "布尔数值 冻结.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    frame = cli().load_bundle(path)["snapshot"]
    assert frame["price"].iloc[0] is True
    assert frame["price"].iloc[1] == 10.
    status, result, _ = run_bundle(tmp_path, capsys, value)
    assert status == 2 and result["readiness"]["state"] == "blocked"
    assert "snapshot:600001:price:invalid" in result["readiness"]["blockers"]
    assert result["result"] is None
    assert result["preview_only"] is True and result["publish_allowed"] is False


@pytest.mark.parametrize("damage", ["stale", "proof", "date", "calendar", "universe"])
def test_missing_or_stale_evidence_blocks(monkeypatch, tmp_path, capsys, damage):
    forbid_remote(monkeypatch)
    value = bundle()
    if damage == "stale":
        value["snapshot"]["data"][1][-1] = "2026-10-08"
    elif damage == "proof":
        del value["snapshot"]["attrs"]["evidence_id"]
    elif damage == "date":
        del value["snapshot"]["attrs"]["field_trade_date_columns"]["price"]
    else:
        value[f"{damage}_verified"] = False
    status, result, _ = run_bundle(tmp_path, capsys, value)
    assert status == 2 and result["readiness"]["state"] == "blocked"
    assert result["readiness"]["blockers"] and result["result"] is None
    assert result["preview_only"] is True and result["publish_allowed"] is False


def test_valid_zero_candidates_remains_success(tmp_path, capsys):
    value = bundle()
    for row in value["snapshot"]["data"]:
        row[3] = 0.
    status, result, _ = run_bundle(tmp_path, capsys, value)
    assert status == 0 and result["result"]["picks"] == []
    assert result["scope"]["snapshot_pass"] == {"count": 0, "codes": []}


@pytest.mark.parametrize("damage", ["top_key", "frame_key", "attrs_key", "duplicate_columns", "row_length",
                                    "index_length", "index_object", "nested_cell", "columns_type", "empty_column",
                                    "data_type", "attrs_type", "date_map_type", "history_type", "history_code",
                                    "history_frame", "universe_type", "universe_duplicate", "universe_code",
                                    "calendar_string", "universe_integer", "version_bool", "version_unknown",
                                    "date_non_iso", "date_invalid", "missing_top"])
def test_schema_rejects_unsupported_or_coerced_input(tmp_path, capsys, damage):
    value = bundle()
    frame = value["snapshot"]
    if damage == "top_key":
        value["webhook"] = "secret-do-not-print"
    elif damage == "frame_key":
        frame["import"] = "secret-do-not-print"
    elif damage == "attrs_key":
        frame["attrs"]["token"] = "secret-do-not-print"
    elif damage == "duplicate_columns":
        frame["columns"][1] = "code"
    elif damage == "row_length":
        frame["data"][0].pop()
    elif damage == "index_length":
        frame["index"].pop()
    elif damage == "index_object":
        frame["index"][0] = {"token": "secret-do-not-print"}
    elif damage == "nested_cell":
        frame["data"][0][1] = {"token": "secret-do-not-print"}
    elif damage == "columns_type":
        frame["columns"] = "code"
    elif damage == "empty_column":
        frame["columns"][0] = ""
    elif damage == "data_type":
        frame["data"] = {}
    elif damage == "attrs_type":
        frame["attrs"] = []
    elif damage == "date_map_type":
        frame["attrs"]["field_trade_date_columns"] = {"price": []}
    elif damage == "history_type":
        value["daily_histories"] = []
    elif damage == "history_code":
        value["daily_histories"] = {"1": copy.deepcopy(frame)}
    elif damage == "history_frame":
        value["daily_histories"] = {"600001": {"pickle": "secret-do-not-print"}}
    elif damage == "universe_type":
        value["universe"] = "600001"
    elif damage == "universe_duplicate":
        value["universe"] = ["600001", "600001"]
    elif damage == "universe_code":
        value["universe"] = [600001]
    elif damage == "calendar_string":
        value["calendar_verified"] = "true"
    elif damage == "universe_integer":
        value["universe_verified"] = 1
    elif damage.startswith("version_"):
        value["schema_version"] = True if damage == "version_bool" else 2
    elif damage.startswith("date_"):
        value["trade_date"] = "20261009" if damage == "date_non_iso" else "2026-02-30"
    else:
        del value["universe"]
    status, result, output = run_bundle(tmp_path, capsys, value)
    assert status == 2 and result == {"error": "bundle_invalid_schema", "preview_only": True, "publish_allowed": False}
    assert "secret" not in output.out + output.err


@pytest.mark.parametrize("content", [b'{"token":"secret",', b'\xff', b'{"value":NaN}', b'{"value":Infinity}',
                                      b'{"schema_version":1,"schema_version":1}'])
def test_corrupt_json_is_safe_error(tmp_path, capsys, content):
    path = tmp_path / "corrupt.json"
    path.write_bytes(content)
    assert cli().main(["preview", "--bundle", str(path), "--strategy", "dual_low"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "bundle_invalid_json"


def test_oversize_file_rejected_before_read(monkeypatch, tmp_path, capsys):
    path = tmp_path / "large.json"
    with path.open("wb") as stream:
        stream.truncate(cli().MAX_BUNDLE_BYTES + 1)
    original = Path.open
    def no_read(self, *args, **kwargs):
        if self == path:
            pytest.fail("oversize file opened for reading")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", no_read)
    assert cli().main(["preview", "--bundle", str(path), "--strategy", "dual_low"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "bundle_too_large"


@pytest.mark.parametrize("limit", ["0", "-1", "1.5"])
def test_invalid_technical_limit_is_parser_error(tmp_path, capsys, limit):
    with pytest.raises(SystemExit) as raised:
        cli().main(["preview", "--bundle", str(tmp_path / "absent"), "--strategy", "dual_low", "--technical-limit", limit])
    assert raised.value.code == 2
    assert not capsys.readouterr().out


def test_unknown_strategy_safe_error(tmp_path, capsys):
    status, result, output = run_bundle(tmp_path, capsys, strategy="secret-strategy")
    assert status == 2 and result["error"] == "preview_invalid_request"
    assert "secret" not in output.out + output.err


def test_strict_serializer_dates_numpy_and_unknown_values():
    normalize = cli().json_value
    value = dict(day=date(2026, 10, 9), time=datetime(2026, 10, 9, 9, 30),
                 integer=np.int64(2), floating=np.float32(1.25), flag=np.bool_(False))
    assert normalize(value) == dict(day="2026-10-09", time="2026-10-09T09:30:00", integer=2, floating=1.25, flag=False)
    for bad in (object(), float("nan"), np.float64(float("inf")), {1: "not-a-json-key"}):
        with pytest.raises((TypeError, ValueError)):
            normalize(bad)


def test_numpy_extended_float_is_finite_json_scalar():
    assert cli().json_value(np.longdouble("1.25")) == 1.25


def test_serialization_failure_never_prints_partial_or_secret(monkeypatch, tmp_path, capsys):
    from src.services.research_delivery.contracts import ReadinessResult
    from src.services.research_delivery.screening_preview import StrategyPreview
    from src.services.screening.models import ScreenResult
    result = ScreenResult("dual_low", "cn")
    result.picks = [object()]
    monkeypatch.setattr(cli(), "preview_strategy", lambda *a, **k: StrategyPreview(ReadinessResult("ready", 0, 0, 0, (), ()), None, result))
    status, output, _ = run_bundle(tmp_path, capsys)
    assert status == 2 and output["error"] == "preview_failed"


@pytest.mark.parametrize("args", [["catalog"], ["--help"], ["preview", "--help"],
                                   ["preview", "--bundle", "不存在 secret.json", "--strategy", "dual_low"]])
def test_actual_subprocess_other_cwd_utf8_and_no_env_secret(tmp_path, args):
    location = tmp_path / "中文 空间"
    location.mkdir()
    env = dict(os.environ, FEISHU_WEBHOOK_URL="secret-webhook", STRATEGIES_DIR="secret-directory")
    completed = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=location, env=env,
                               capture_output=True, encoding="utf-8", timeout=20)
    assert completed.returncode == (2 if "--bundle" in args else 0)
    assert "secret" not in completed.stdout + completed.stderr
    if args == ["catalog"]:
        assert len(json.loads(completed.stdout)) == 10
    elif "--bundle" in args:
        assert json.loads(completed.stdout)["error"] == "bundle_unreadable"
    else:
        assert "LOCAL" in completed.stdout


def test_actual_subprocess_dual_low_bundle(tmp_path):
    path = tmp_path / "合成 本地.json"
    path.write_text(json.dumps(bundle(), ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run([sys.executable, str(SCRIPT), "preview", "--bundle", str(path), "--strategy", "dual_low"],
                               cwd=tmp_path, capture_output=True, encoding="utf-8", timeout=20)
    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert result["preview_only"] is True and result["publish_allowed"] is False
    assert result["readiness"]["state"] == "degraded" and len(result["result"]["picks"]) == 2
