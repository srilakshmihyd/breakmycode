import tarfile
import json

from breakmycode.serverless import build_source_archive


def test_serverless_archive_excludes_secrets_and_git_metadata(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("print('safe')\n", encoding="utf-8")
    (project / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("secret", encoding="utf-8")
    output = tmp_path / "job.tar.gz"

    build_source_archive(project, output, "scan_test")

    with tarfile.open(output, "r:gz") as archive:
        names = archive.getnames()
        correlation = json.loads(
            archive.extractfile(".breakmycode-job.json").read().decode("utf-8")
        )
    assert names == ["app.py", ".breakmycode-job.json"]
    assert correlation == {"scan_id": "scan_test"}
