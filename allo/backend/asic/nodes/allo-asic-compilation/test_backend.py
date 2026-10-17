"""Tests for HLS backend configuration and RTL artifact discovery."""

from pathlib import Path

from backend import configure_backend, find_rtl_directory, publish_rtl_artifacts


def test_configure_vitis_defaults():
    target, configs, metadata = configure_backend("vitis", 8.0, "")
    assert target == "vitis_hls"
    assert configs["frequency"] == 125.0
    assert configs["device"] == "u280"
    assert metadata["rtl_stage"] == "syn"


def test_configure_systemc_defaults():
    target, configs, metadata = configure_backend("systemc", 3.333, "")
    assert target == "systemc"
    assert configs["clock_period"] == 3.333
    assert configs["device"] == "nangate-45nm_beh"
    assert configs["preserve_hierarchy"] is True
    assert metadata == {
        "device": "nangate-45nm_beh",
        "rtl_stage": "rtl",
        "frontend": "systemc",
        "channel_protocol": "connections",
        "synthesis_engine": "catapult",
        "rtl_artifact": "concat_rtl.v",
    }


def test_find_systemc_rtl_directory_under_build(tmp_path: Path):
    rtl_dir = tmp_path / "build" / "Catapult_1" / "top.v1"
    rtl_dir.mkdir(parents=True)
    (rtl_dir / "rtl.v").write_text("module top; endmodule\n")
    assert find_rtl_directory("systemc", tmp_path) == rtl_dir


def test_publish_systemc_normalizes_rtl_filename(tmp_path: Path):
    rtl_dir = tmp_path / "project" / "build" / "Catapult_1" / "top.v1"
    rtl_dir.mkdir(parents=True)
    (rtl_dir / "rtl.v").write_text("module top; endmodule\n")

    output = tmp_path / "published"
    publish_rtl_artifacts("systemc", tmp_path / "project", output)

    assert (output / "concat_rtl.v").read_text() == "module top; endmodule\n"
    assert not (output / "rtl.v").exists()
