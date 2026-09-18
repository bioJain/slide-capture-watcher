import pytest


@pytest.fixture(scope="module")
def cli(core):
    import slide_capture_watcher

    return slide_capture_watcher


def test_defaults_build_matching_config_and_options(cli, core):
    args = cli.build_arg_parser().parse_args(["--title", "Zoom"])
    cfg = cli.build_config(args)
    opts = cli.build_watch_options(args)

    assert cfg == core.Config()
    assert opts.title == "Zoom"
    assert opts.mode == "window"
    assert opts.interval == 1.0
    assert opts.max_missing == 5
    assert opts.debug_diff is False
    assert str(opts.outdir) == "captures"


def test_roi_exclude_grid_arguments(cli):
    args = cli.build_arg_parser().parse_args([
        "--title", "Zoom",
        "--roi", "0.1,0.1,0.8,0.8",
        "--exclude", "0.7,0,0.3,0.3",
        "--exclude", "0,0.9,1,0.1",
        "--grid", "off",
        "--mode", "region",
        "--debug-diff",
    ])
    cfg = cli.build_config(args)
    opts = cli.build_watch_options(args)

    assert cfg.roi == (0.1, 0.1, 0.8, 0.8)
    assert cfg.exclude == [(0.7, 0.0, 0.3, 0.3), (0.0, 0.9, 1.0, 0.1)]
    assert (cfg.grid_rows, cfg.grid_cols) == (0, 0)
    assert opts.mode == "region"
    assert opts.debug_diff is True


def test_invalid_roi_is_rejected_with_message(cli, capsys):
    with pytest.raises(SystemExit):
        cli.build_arg_parser().parse_args(["--title", "Zoom", "--roi", "0.5,0,0.6,1"])
    assert "1을 넘습니다" in capsys.readouterr().err


def test_main_requires_title(cli, capsys):
    assert cli.main([]) == 1
    assert "--title" in capsys.readouterr().err


def test_main_reports_missing_window(cli, core, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(core, "find_windows_by_title", lambda title: [])
    code = cli.main(["--title", "nothing", "--outdir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "찾지 못했습니다" in out
    assert "--list-windows" in out
