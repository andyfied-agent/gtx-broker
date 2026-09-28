"""Tests for the full Model-Profile Controller."""

import json
import os
import tempfile
from unittest.mock import patch, MagicMock

import pytest

from gtx_broker.scheduler.model_profiles import (
    ModelProfileError,
    P40ModelProfileController,
    ProfileMetadata,
)


class TestProfileLoading:
    """Tests for profile loading from .conf files."""
    
    def test_loads_profiles_from_conf_files(self, tmp_path):
        """Controller loads profiles from /etc/llama-cpp/profiles/*.conf."""
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        
        (profiles_dir / "qwen35-coding.conf").write_text("""
MODEL_PATH=/path/to/model.gguf
CHAT_TEMPLATE=qwen3.5
QUANTIZATION=Q3_K_XL
GPU_LAYERS=99
CONTEXT_SIZE=262144
SERVER_SLOTS=4
MODEL_ID=qwen3.5-35b-ud-q3_k_xl
""")
        
        (profiles_dir / "qwen35-vision.conf").write_text("""
MODEL_PATH=/path/to/vision.gguf
CHAT_TEMPLATE=qwen3.5-vision
QUANTIZATION=Q2_K
GPU_LAYERS=99
CONTEXT_SIZE=262144
SERVER_SLOTS=4
MODEL_ID=qwen3.5-35b-vision-q2_k
MMPROJ_PATH=/path/to/mmproj
MMPROJ_TYPE=bf16
""")
        
        controller = P40ModelProfileController()
        with patch.object(controller, 'PROFILES_DIR', profiles_dir):
            controller._load_profiles_from_conf()
        
        assert len(controller.available_profiles) == 2
        assert "qwen35-coding" in controller.available_profiles
        assert "qwen35-vision" in controller.available_profiles
    
    def test_profile_metadata_parsed(self, tmp_path):
        """Each profile has correct metadata from .conf files."""
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        
        (profiles_dir / "test-profile.conf").write_text("""
MODEL_PATH=/path/to/model.gguf
CHAT_TEMPLATE=test
QUANTIZATION=Q4_K_M
GPU_LAYERS=50
CONTEXT_SIZE=131072
SERVER_SLOTS=2
MODEL_ID=test-model-v1
""")
        
        controller = P40ModelProfileController()
        with patch.object(controller, 'PROFILES_DIR', profiles_dir):
            controller._load_profiles_from_conf()
        
        profile = controller._profiles.get("test-profile")
        assert profile is not None
        assert profile.expected_model_id == "test-model-v1"
        assert profile.quantization == "Q4_K_M"
        assert profile.gpu_layers == 50
        assert profile.context_size == 131072


class TestEnsureProfile:
    """Tests for ensure_profile() method."""
    
    def test_already_active_returns_true(self):
        controller = P40ModelProfileController()
        controller._profiles["qwen35-coding"] = MagicMock()
        controller._profiles["qwen35-coding"].expected_model_id = "qwen3.5-35b-ud-q3_k_xl"
        
        with patch.object(controller, '_get_current_model_id', return_value="qwen3.5-35b-ud-q3_k_xl"):
            result = controller.ensure_profile("qwen35-coding")
            assert result is True
            assert controller.current_profile == "qwen35-coding"
    
    def test_invalid_profile_returns_false(self):
        controller = P40ModelProfileController()
        result = controller.ensure_profile("unknown-profile")
        assert result is False
    
    def test_switches_profile_when_different(self):
        controller = P40ModelProfileController()
        controller._profiles["qwen35-coding"] = MagicMock()
        controller._profiles["qwen35-coding"].expected_model_id = "qwen3.5-35b-ud-q3_k_xl"
        
        with patch.object(controller, '_get_current_model_id', side_effect=[
            "wrong-model",
            "qwen3.5-35b-ud-q3_k_xl"
        ]):
            with patch.object(controller, '_verify_profile_switch', return_value=True):
                with patch("os.path.exists", return_value=True):
                    mock_result = MagicMock()
                    mock_result.returncode = 0
                    mock_result.stdout = "Switch completed"
                    
                    with patch("subprocess.run", return_value=mock_result):
                        result = controller.ensure_profile("qwen35-coding")
                        assert result is True
    
    def test_fails_when_wrapper_not_found(self):
        controller = P40ModelProfileController()
        controller._profiles["qwen35-coding"] = MagicMock()
        
        with patch("os.path.exists", return_value=False):
            result = controller.ensure_profile("qwen35-coding")
            assert result is False
    
    def test_fails_when_switch_times_out(self):
        controller = P40ModelProfileController()
        controller._profiles["qwen35-coding"] = MagicMock()
        
        with patch("os.path.exists", return_value=True):
            with patch("subprocess.run", side_effect=Exception("timeout")):
                result = controller.ensure_profile("qwen35-coding")
                assert result is False
    
    def test_fails_when_verification_fails(self):
        controller = P40ModelProfileController()
        controller._profiles["qwen35-coding"] = MagicMock()
        
        with patch.object(controller, '_get_current_model_id', side_effect=[
            "wrong-model",
            "qwen3.5-35b-ud-q3_k_xl"
        ]):
            with patch.object(controller, '_verify_profile_switch', return_value=False):
                with patch("os.path.exists", return_value=True):
                    result = controller.ensure_profile("qwen35-coding")
                    assert result is False


class TestLayeredVerification:
    """Tests for individual verification layers."""
    
    def test_check_service_active_true(self):
        controller = P40ModelProfileController()
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_run.return_value = mock_result
            assert controller._check_service_active() is True
    
    def test_check_health_true(self):
        controller = P40ModelProfileController()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.__enter__.return_value = mock_response
            mock_response.__exit__.return_value = False
            mock_urlopen.return_value = mock_response
            assert controller._check_health() is True
    
    def test_check_health_false(self):
        controller = P40ModelProfileController()
        with patch("urllib.request.urlopen") as mock_urlopen:
            import urllib.error
            mock_urlopen.side_effect = urllib.error.URLError("connection refused")
            assert controller._check_health() is False


class TestSmokeTest:
    """Tests for smoke test functionality."""
    
    def test_run_smoke_test_success(self):
        controller = P40ModelProfileController()
        controller._profiles["qwen35-coding"] = MagicMock()
        controller._profiles["qwen35-coding"].expected_model_id = "qwen3.5-35b-ud-q3_k_xl"
        
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.__enter__.return_value = mock_response
            mock_response.__exit__.return_value = False
            mock_response.read.return_value = json.dumps({
                "choices": [{"message": {"content": "OK"}}]
            }).encode()
            mock_urlopen.return_value = mock_response
            
            result = controller._run_smoke_test("qwen35-coding")
            assert result is True
    
    def test_vision_smoke_test_fails_without_image(self):
        controller = P40ModelProfileController()
        controller._profiles["qwen35-vision"] = MagicMock()
        controller._profiles["qwen35-vision"].expected_model_id = "qwen3.5-35b-vision-q2_k"
        
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.__enter__.return_value = mock_response
            mock_response.__exit__.return_value = False
            mock_response.read.return_value = json.dumps({
                "choices": [{"message": {"content": "OK"}}]
            }).encode()
            mock_urlopen.return_value = mock_response
            
            result = controller._run_smoke_test("qwen35-vision")
            assert result is False


class TestLeaseManagement:
    """Tests for file-based lease mechanism."""
    
    def test_acquire_lease(self):
        controller = P40ModelProfileController()
        try:
            lock_fd = controller._acquire_lease(blocking=True)
            assert lock_fd is not None
            controller._release_lease(lock_fd)
        except (OSError, PermissionError):
            pass
    
    def test_nonblocking_acquire_returns_none_on_contention(self):
        controller = P40ModelProfileController()
        fd1 = controller._acquire_lease(blocking=True)
        fd2 = controller._acquire_lease(blocking=False)
        assert fd2 is None
        controller._release_lease(fd1)


class TestContextManager:
    """Tests for profile context manager (holds lock during execution)."""
    
    def test_context_manager_holds_lock_during_yield(self):
        controller = P40ModelProfileController()
        controller._profiles["qwen35-coding"] = MagicMock()
        controller._profiles["qwen35-coding"].expected_model_id = "qwen3.5-35b-ud-q3_k_xl"
        
        lock_acquired = []
        lock_released = []
        
        def mock_acquire(*args, **kwargs):
            fd = 123
            lock_acquired.append(True)
            return fd
        
        def mock_release(fd):
            lock_released.append(True)
        
        with patch.object(controller, '_acquire_lease', side_effect=mock_acquire):
            with patch.object(controller, '_release_lease', side_effect=mock_release):
                with patch.object(controller, '_ensure_profile_under_lease', return_value=True):
                    with controller.profile("qwen35-coding") as ctx:
                        assert len(lock_acquired) == 1
                        assert len(lock_released) == 0
                    
                    assert len(lock_released) == 1
    
    def test_context_manager_failure_raises_error(self):
        controller = P40ModelProfileController()
        
        def mock_acquire(*args, **kwargs):
            return 123
        
        with patch.object(controller, '_acquire_lease', side_effect=mock_acquire):
            with patch.object(controller, '_ensure_profile_under_lease', return_value=False):
                with patch.object(controller, '_release_lease'):
                    with pytest.raises(ModelProfileError):
                        with controller.profile("qwen35-coding"):
                            pass


class TestEnsureProfileUnderLease:
    """Tests for internal _ensure_profile_under_lease method (no lock handling)."""
    
    def test_assumes_lock_already_held(self):
        controller = P40ModelProfileController()
        controller._profiles["qwen35-coding"] = MagicMock()
        controller._profiles["qwen35-coding"].expected_model_id = "qwen3.5-35b-ud-q3_k_xl"
        
        with patch.object(controller, '_get_current_model_id', return_value="qwen3.5-35b-ud-q3_k_xl"):
            result = controller._ensure_profile_under_lease("qwen35-coding")
            assert result is True
    
    def test_updates_current_profile_on_success(self):
        controller = P40ModelProfileController()
        controller._profiles["qwen35-vision"] = MagicMock()
        controller._profiles["qwen35-vision"].expected_model_id = "qwen3.5-35b-vision-q2_k"
        
        with patch.object(controller, '_get_current_model_id', side_effect=[
            None,
            "qwen3.5-35b-vision-q2_k"
        ]):
            with patch.object(controller, '_verify_profile_switch', return_value=True):
                with patch("os.path.exists", return_value=True):
                    with patch("subprocess.run", return_value=MagicMock(returncode=0)):
                        result = controller._ensure_profile_under_lease("qwen35-vision")
                        assert result is True
                        assert controller.current_profile == "qwen35-vision"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
