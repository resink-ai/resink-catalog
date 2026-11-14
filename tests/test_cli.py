"""The CLI: add-folder proposes, import applies, scan re-checks."""

import json

import pytest

from resink_catalog.cli import main


def run(capsys, *argv):
    code = main(list(argv))
    return code, capsys.readouterr().out


def test_add_folder_then_import_then_scan(tmp_path, capsys):
    home, folder = tmp_path / "home", tmp_path / "exp"
    folder.mkdir()
    (folder / "a.csv").write_text("id,v\n1,x\n")
    (folder / "notes.docx").write_text("ignored")
    code, out = run(capsys, "add-folder", str(folder), "--home", str(home), "--json")
    proposed = json.loads(out)
    assert code == 0 and [s["status"] for s in proposed] == ["proposed"]
    code, out = run(capsys, "import", str(folder), "--home", str(home), "--json")
    assert code == 0 and json.loads(out)[0]["outcome"] == "created"
    code, out = run(capsys, "import", str(folder), "--home", str(home), "--json")
    assert json.loads(out) == []  # nothing left to apply
    (folder / "a.csv").write_text("id,v\n1,x\n2,y\n")
    code, out = run(capsys, "scan", "--home", str(home), "--apply", "--json")
    assert json.loads(out)["imported"][0]["outcome"] == "updated"


def test_import_single_file_with_table_name(tmp_path, capsys):
    f = tmp_path / "x.csv"
    f.write_text("a\n1\n")
    code, out = run(capsys, "import", str(f), "--table", "mine.x", "--home", str(tmp_path / "h"))
    assert code == 0 and "mine.x" in out


def test_import_of_unadded_folder_explains_what_to_do(tmp_path, capsys):
    (tmp_path / "f").mkdir()
    code = main(["import", str(tmp_path / "f"), "--home", str(tmp_path / "h")])
    assert code == 2 and "add-folder" in capsys.readouterr().err


def test_import_returns_nonzero_when_a_file_fails(tmp_path, capsys):
    bad = tmp_path / "bad.csv"
    bad.write_bytes(bytes(range(256)) * 8)
    code, out = run(capsys, "import", str(bad), "--home", str(tmp_path / "h"))
    assert code == 1 and "error" in out


@pytest.mark.parametrize("cmd", ["import", "add-folder"])
def test_commands_require_a_path(cmd):
    with pytest.raises(SystemExit):
        main([cmd])


def test_preview_and_type_override_from_the_cli(tmp_path, capsys):
    f = tmp_path / "s.csv"
    f.write_text("id,amount\n1,5\n2,6\n")
    home = str(tmp_path / "h")
    code, out = run(capsys, "preview", str(f), "--home", home, "--json")
    view = json.loads(out)
    assert code == 0 and [(c["name"], c["type"]) for c in view["columns"]] == [("id", "long"), ("amount", "long")]
    code, out = run(capsys, "import", str(f), "--table", "t", "--type", "id=string", "--home", home, "--json")
    assert code == 0 and json.loads(out)[0]["outcome"] == "created"
    code, out = run(capsys, "preview", str(f), "--home", home)
    assert "string" in out and "inferred long" in out  # the saved override is shown next to the inferred type


def test_bad_type_arguments(tmp_path, capsys):
    f = tmp_path / "s.csv"
    f.write_text("id\n1\n")
    assert main(["import", str(f), "--type", "oops", "--home", str(tmp_path / "h")]) == 2
    assert main(["import", str(f), "--type", "id=varchar", "--home", str(tmp_path / "h")]) == 2
    (tmp_path / "d").mkdir()
    assert main(["import", str(tmp_path / "d"), "--type", "id=string", "--home", str(tmp_path / "h")]) == 2
