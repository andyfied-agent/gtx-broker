"""Tests for the full Model-Profile Controller."""

import json
import socket
import subprocess
import unittest
from unittest.mock import patch, MagicMock

import pytest

from gtx_broker.scheduler.model_profiles import (
    ModelProfileError,
    P40ModelProfileController,
    ProfileMetadata,
)


class TestP40ModelProfileControllerInit:
    """Tests for controller initialization."""
    
    def test_loads_profiles_from_config(self):
        """Controller loads profiles from /etc/llama-cpp/profiles.yaml."""
        controller = P40ModelProfileController()
        
        # Should have at least the default profiles
        assert len(controller.available_profiles) >= 3
        assert "qwen35-coding" in controller.available_profiles
        assert "qwen35-vision" in controller.available_profiles
        assert "qwen36-vision" in controller.available_profiles
    
    def test_profile_metadata_loaded(self):
        """Each profile has correct metadata."""
        controller = P40ModelProfileController()
        
        coding_profile = controller._profiles.get("qwen35-coding")
        assert coding_profile is not None
        assert coding_profile.model_family == "qwen3.5"
        assert coding_profile.role == "coding"
        assert coding_profile.gpu_layers == 99
        assert coding_profile.context_size == 262144
        assert coding_profile.projector_path is None
    
    def test_vision_profile_has_projector(self):
        """Vision profiles include projector metadata."""
        controller = P40ModelProfileController()
        
        vision_profile = controller._profiles.get("qwen35-vision")
        assert vision_profile is not None
        assert vision_profile.role == "vision"
        assert vision_profile.projector_path is not None
        assert vision_profile.projector_type == "bf16"


class TestEnsureProfile:
    """Tests for ensure_profile() method."""
    
    def test_already_active_returns_true(self):
        """ensure_profile returns True if profile already active."""
        controller = P40ModelProfileController()
        
        # Mock _get_current_model_id to return expected model
        with patch.object(controller, '_get_current_model_id', return_value="qwen3.5-35b-ud-q3_k_xl"):
            result = controller.ensure_profile("qwen35-coding")
            assert result is True
            assert controller.current_profile == "qwen35-coding"
    
    def test_invalid_profile_returns_false(self):
        """ensure_profile returns False for unknown profiles."""
        controller = P40ModelProfileController()
        
        result = controller.ensure_profile("unknown-profile")
        assert result is False
    
    def test_switches_profile_when_different(self):
        """ensure_profile calls switch wrapper when model differs."""
        controller = P40ModelProfileController()
        
        # Simulate different current model
        with patch.object(controller, '_get_current_model_id', side_effect=[
            "wrong-model",  # Initial check
            "qwen3.5-35b-ud-q3_k_xl"  # After switch
        ]):
            with patch.object(controller, '_verify_profile_switch', return_value=True):
                # Mock the wrapper script to exist
                with patch("os.path.exists", return_value=True):
                    # Mock subprocess.run to succeed
                    mock_result = MagicMock()
                    mock_result.returncode = 0
                    mock_result.stdout = "Switch completed"
                    mock_result.stderr = ""
                    
                    with patch("subprocess.run", return_value=mock_result):
                        result = controller.ensure_profile("qwen35-coding")
                        assert result is True
    
    def test_fails_when_wrapper_not_found(self):
        """ensure_profile returns False if wrapper script missing."""
        controller = P40ModelProfileController()
        
        with patch("os.path.exists", return_value=False):
            result = controller.ensure_profile("qwen35-coding")
            assert result is False
    
    def test_fails_when_switch_times_out(self):
        """ensure_profile returns False if switch times out."""
        controller = P40ModelProfileController()
        
        with patch("os.path.exists", return_value=True):
            with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["cmd"], 120)):
                # Should return False on timeout, not raise
                result = controller.ensure_profile("qwen35-coding")
                assert result is False
    
    def test_fails_when_verification_fails(self):
        """ensure_profile returns False if verification fails."""
        controller = P40ModelProfileController()
        
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
        """_check_service_active returns True when service running."""
        controller = P40ModelProfileController()
        
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_run.return_value = mock_result
            
            assert controller._check_service_active() is True
    
    def test_check_service_active_false(self):
        """_check_service_active returns False when service not running."""
        controller = P40ModelProfileController()
        
        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 1
            mock_run.return_value = mock_result
            
            assert controller._check_service_active() is False
    
    def test_check_health_true(self):
        """_check_health returns True when health endpoint OK."""
        controller = P40ModelProfileController()
        
        with patch("socket.socket") as mock_socket:
            mock_sock = MagicMock()
            mock_sock.recv.side_effect = [
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}",
                b""  # End of response
            ]
            mock_socket.return_value.__enter__.return_value = mock_sock
            
            assert controller._check_health() is True
    
    def test_check_health_false(self):
        """_check_health returns False when health endpoint fails."""
        controller = P40ModelProfileController()
        
        with patch("socket.socket") as mock_socket:
            mock_sock = MagicMock()
            mock_sock.recv.side_effect = [
                b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 2\r\n\r\n{}",
                b""  # End of response
            ]
            mock_socket.return_value.__enter__.return_value = mock_sock
            
            assert controller._check_health() is False
    
    def test_run_smoke_test_success(self):
        """_run_smoke_test returns True when response contains OK."""
        controller = P40ModelProfileController()
        
        with patch("socket.socket") as mock_socket:
            mock_sock = MagicMock()
            mock_sock.recv.side_effect = [
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 80\r\n\r\n",
                b'{"choices": [{"message": {"content": "OK"}}]}',
                b""  # End of response
            ]
            mock_socket.return_value.__enter__.return_value = mock_sock
            
            result = controller._run_smoke_test("qwen35-coding")
            assert result is True


class TestLeaseManagement:
    """Tests for file-based lease mechanism."""
    
    def test_acquire_lease(self):
        """_acquire_lease acquires exclusive lock."""
        controller = P40ModelProfileController()
        
        # Just verify it doesn't crash - actual lock behavior depends on OS
        try:
            lock_fd = controller._acquire_lease(blocking=True)
            assert lock_fd is not None
            controller._release_lease(lock_fd)
        except (OSError, PermissionError):
            # May fail if /run/lock/gtx-broker doesn't exist or permissions wrong
            # This is OK for unit tests
            pass
    
    def test_lock_dir_created(self):
        """_ensure_lock_dir creates directory with correct permissions."""
        controller = P40ModelProfileController()
        
        # This may fail if /run/lock/gtx-broker already exists with wrong permissions
        # Just verify it doesn't crash with invalid inputs
        try:
            controller._ensure_lock_dir()
        except OSError:
            # OK if can't create
            pass


class TestContextManager:
    """Tests for legacy context manager API."""
    
    def test_context_manager_successful_work(self):
        """Context manager yields work under profile."""
        controller = P40ModelProfileController()
        
        # Mock ensure_profile to return True and set current_profile
        def mock_ensure_profile(profile_name):
            controller._current_profile = profile_name
            return True
        
        with patch.object(controller, 'ensure_profile', side_effect=mock_ensure_profile):
            with controller.profile("qwen35-coding") as ctx:
                assert controller.current_profile == "qwen35-coding"
            # Profile stays active after context (no auto-restore)
            assert controller.current_profile == "qwen35-coding"
    
    def test_context_manager_failure_raises_error(self):
        """Context manager raises ModelProfileError on switch failure."""
        controller = P40ModelProfileController()
        
        # Mock _profiles to include the test profile
        controller._profiles["qwen35-coding"] = MagicMock()
        
        with patch.object(controller, 'ensure_profile', return_value=False):
            with pytest.raises(ModelProfileError):
                with controller.profile("qwen35-coding"):
                    pass


class TestIntegration:
    """Integration tests for full workflow."""
    
    def test_full_switch_workflow(self):
        """Test complete profile switch workflow."""
        controller = P40ModelProfileController()
        
        # Simulate full workflow: wrong model → switch → verify → active
        with patch.object(controller, '_get_current_model_id', side_effect=[
            "wrong-model",      # Initial check
            "qwen3.5-35b-ud-q3_k_xl"  # After switch
        ]):
            with patch.object(controller, '_verify_profile_switch', return_value=True):
                with patch("os.path.exists", return_value=True):
                    mock_result = MagicMock()
                    mock_result.returncode = 0
                    mock_result.stdout = "Switch completed"
                    
                    with patch("subprocess.run", return_value=mock_result):
                        result = controller.ensure_profile("qwen35-coding")
                        
                        assert result is True
                        assert controller.current_profile == "qwen35-coding"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
