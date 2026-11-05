"""Model download: the completion marker that install.sh checks."""

import sys
from types import SimpleNamespace

import pytest

from npu_voice_to_text import models


def test_marker_only_after_a_finished_download(tmp_path, monkeypatch):
    dest = tmp_path / "whisper"
    monkeypatch.setattr(models, "model_path", lambda model_id: dest)
    marker = dest / models.COMPLETE_MARKER

    def interrupted(repo_id, local_dir):
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / "openvino_encoder_model.xml").touch()   # small file done, .bin not
        raise KeyboardInterrupt
    monkeypatch.setitem(sys.modules, "huggingface_hub",
                        SimpleNamespace(snapshot_download=interrupted))
    with pytest.raises(KeyboardInterrupt):
        models.download("OpenVINO/whisper")
    assert not marker.exists()

    monkeypatch.setitem(sys.modules, "huggingface_hub",
                        SimpleNamespace(snapshot_download=lambda repo_id, local_dir: None))
    assert models.download("OpenVINO/whisper") == 0
    assert marker.exists()


def test_marker_name_matches_install_sh():
    install = (models.Path(__file__).parents[1] / "install.sh").read_text()
    assert f'/{models.COMPLETE_MARKER}"' in install
