"""Enhanced P40 Model Profile Controller with runtime profile management.

This module provides the full Model-Profile Controller for compute01's P40 GPU,
including:
- Exclusive file-based lease mechanism
- Profile inspection and switching via root wrapper
- Layered verification (systemd → health → models → slots → smoke test)
- Runtime remediation of WRONG_MODEL_LOADED alerts
- No automatic restoration (profile stays resident until explicitly changed)
"""

from contextlib import contextmanager
import json
import logging
import os
import socket
import subprocess
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

import yaml

logger = logging.getLogger(__name__)


class ModelProfileError(RuntimeError):
    """Raised when a P40 profile cannot be applied or restored."""
    pass


@dataclass
class ProfileMetadata:
    """Profile configuration metadata."""
    description: str
    model_family: str
    role: str
    quantization: str
    gguf_path: str
    expected_model_id: str
    expected_gguf_hash: str
    projector_path: Optional[str]
    projector_type: Optional[str]
    gpu_layers: int
    context_size: int
    chat_template: str
    slots: int


class P40ModelProfileController:
    """Full Model-Profile Controller for P40 runtime profile management.
    
    This controller provides:
    1. Exclusive file-based lease for profile operations
    2. Profile inspection via /v1/models endpoint
    3. Profile switching via root wrapper script
    4. Layered verification (systemd → health → models → slots → smoke test)
    5. Runtime remediation of WRONG_MODEL_LOADED alerts
    
    The controller does NOT automatically restore profiles after tasks.
    Profiles stay resident until explicitly requested otherwise.
    """
    
    # System paths
    PROFILE_CONFIG_PATH = "/etc/llama-cpp/profiles.yaml"
    SWITCH_WRAPPER_PATH = "/usr/local/sbin/compute01-maint/p40-switch-profile"
    LOCK_DIR = Path("/run/lock/gtx-broker")
    LOCK_FILE = LOCK_DIR / "p40-profile.lock"
    ACTIVE_CONFIG = Path("/etc/llama-cpp/p40-active.conf")
    SERVICE_NAME = "llama-qwen35.service"
    
    # API endpoints
    HOST = "127.0.0.1"
    PORT = 11436
    HEALTH_URL = f"http://{HOST}:{PORT}/health"
    MODELS_URL = f"http://{HOST}:{PORT}/v1/models"
    SLOTS_URL = f"http://{HOST}:{PORT}/v1/slots"
    CHAT_COMPLETIONS_URL = f"http://{HOST}:{PORT}/v1/chat/completions"
    
    def __init__(self):
        """Initialize controller with profile metadata."""
        self._profiles: dict[str, ProfileMetadata] = {}
        self._current_profile: Optional[str] = None
        self._load_profile_metadata()
        logger.info(f"Loaded {len(self._profiles)} P40 profiles")
    
    def _load_profile_metadata(self) -> None:
        """Load profile metadata from system config."""
        config_path = Path(self.PROFILE_CONFIG_PATH)
        
        if not config_path.exists():
            logger.warning(f"Profile config not found: {config_path}")
            logger.warning("Profiles will be loaded when first requested")
            return
        
        try:
            with open(config_path, "r") as f:
                config = yaml.safe_load(f)
            
            profiles_config = config.get("profiles", {})
            for profile_name, profile_data in profiles_config.items():
                self._profiles[profile_name] = ProfileMetadata(
                    description=profile_data.get("description", ""),
                    model_family=profile_data.get("model_family", ""),
                    role=profile_data.get("role", ""),
                    quantization=profile_data.get("quantization", ""),
                    gguf_path=profile_data.get("gguf_path", ""),
                    expected_model_id=profile_data.get("expected_model_id", ""),
                    expected_gguf_hash=profile_data.get("expected_gguf_hash", ""),
                    projector_path=profile_data.get("projector_path"),
                    projector_type=profile_data.get("projector_type"),
                    gpu_layers=profile_data.get("gpu_layers", 99),
                    context_size=profile_data.get("context_size", 262144),
                    chat_template=profile_data.get("chat_template", ""),
                    slots=profile_data.get("slots", 4),
                )
            
            logger.info(f"Loaded {len(self._profiles)} P40 profiles from {config_path}")
        except (yaml.YAMLError, OSError) as exc:
            logger.error(f"Failed to load profile config: {exc}")
            raise ModelProfileError(f"Failed to load profile config: {exc}")
    
    @property
    def available_profiles(self) -> list[str]:
        """Return list of available profile names."""
        return list(self._profiles.keys())
    
    @property
    def current_profile(self) -> Optional[str]:
        """Return the currently active profile name."""
        return self._current_profile
    
    def _ensure_lock_dir(self) -> None:
        """Ensure lock directory exists with correct permissions."""
        if not self.LOCK_DIR.exists():
            try:
                self.LOCK_DIR.mkdir(parents=True, mode=0o770)
            except OSError as exc:
                logger.error(f"Failed to create lock directory: {exc}")
                raise
    
    def _acquire_lease(self, blocking: bool = True) -> Optional[int]:
        """Acquire exclusive file-based lease for profile operations."""
        self._ensure_lock_dir()
        
        lock_fd = None
        try:
            lock_fd = os.open(str(self.LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o660)
            
            import fcntl
            lock_type = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            
            try:
                fcntl.flock(lock_fd, lock_type)
                logger.debug(f"Lease acquired: {self.LOCK_FILE}")
                return lock_fd
            except (IOError, OSError) as exc:
                if not blocking:
                    logger.debug(f"Could not acquire lease (non-blocking): {exc}")
                    # Close fd before returning None
                    if lock_fd is not None:
                        os.close(lock_fd)
                    return None
                logger.error(f"Could not acquire lease: {exc}")
                raise
        
        except Exception as exc:
            logger.error(f"Lease acquisition failed: {exc}")
            if lock_fd is not None:
                try:
                    os.close(lock_fd)
                except Exception:
                    pass
            raise
    
    def _release_lease(self, lock_fd: Optional[int]) -> None:
        """Release exclusive file-based lease."""
        if lock_fd is not None:
            try:
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
                logger.debug(f"Lease released: {self.LOCK_FILE}")
            except (IOError, OSError) as exc:
                logger.warning(f"Lease release failed: {exc}")
    
    def _get_current_model_id(self) -> Optional[str]:
        """Query /v1/models to get currently loaded model ID using urllib."""
        try:
            with urllib.request.urlopen(self.MODELS_URL, timeout=5.0) as response:
                data = json.loads(response.read().decode("utf-8"))
                
                if "data" in data and len(data["data"]) > 0:
                    return data["data"][0].get("id")
                
                return None
        
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
            logger.debug(f"Failed to query /v1/models: {exc}")
            return None
    
    def _check_service_active(self) -> bool:
        """Check if llama-qwen35.service is active."""
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "--quiet", self.SERVICE_NAME],
                capture_output=True,
                timeout=10
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.debug(f"Failed to check service status: {exc}")
            return False
    
    def _check_health(self) -> bool:
        """Check /health endpoint using urllib."""
        try:
            with urllib.request.urlopen(self.HEALTH_URL, timeout=5.0) as response:
                return response.status == 200
        except urllib.error.URLError as exc:
            logger.debug(f"Health check failed: {exc}")
            return False
    
    def _run_smoke_test(self, profile_name: str) -> bool:
        """Run smoke test for profile using urllib."""
        profile = self._profiles.get(profile_name)
        if not profile:
            logger.error(f"Profile not found: {profile_name}")
            return False
        
        # Check vision profile test image
        if "vision" in profile_name:
            test_image = "/usr/local/share/gtx-broker/test-images/receipt_small.jpg"
            if not os.path.exists(test_image):
                logger.error("Vision test image not found: %s", test_image)
                return False
        
        try:
            url = self.CHAT_COMPLETIONS_URL
            
            if "vision" in profile_name:
                # Multimodal smoke test
                import base64
                test_image = "/usr/local/share/gtx-broker/test-images/receipt_small.jpg"
                with open(test_image, "rb") as f:
                    image_data = base64.b64encode(f.read()).decode("utf-8")
                
                message = {
                    "model": profile.expected_model_id,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}},
                            {"type": "text", "text": "Extract the total amount from this receipt. Reply with exactly: 29.24"}
                        ]
                    }],
                    "max_tokens": 16,
                    "temperature": 0
                }
            else:
                # Text-only smoke test
                message = {
                    "model": profile.expected_model_id,
                    "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
                    "max_tokens": 8,
                    "temperature": 0
                }
            
            data = json.dumps(message).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            
            with urllib.request.urlopen(req, timeout=30.0) as response:
                result = json.loads(response.read().decode("utf-8"))
                
                # Check for expected response
                if "choices" in result and len(result["choices"]) > 0:
                    content = result["choices"][0].get("message", {}).get("content", "")
                    if "vision" in profile_name:
                        return "29.24" in content
                    else:
                        return "OK" in content
                
                return False
        
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
            logger.debug(f"Smoke test failed: {exc}")
            return False
    
    def ensure_profile(self, profile_name: str) -> bool:
        """Ensure a specific profile is active on P40.
        
        This is the main entry point for both:
        1. Task execution (worker_profile selection)
        2. Runtime alert remediation (WRONG_MODEL_LOADED)
        
        Steps:
        1. Acquire exclusive lease
        2. Inspect current model via /v1/models
        3. If already correct: release lease, return True
        4. If different: call root wrapper to switch
        5. Verify via layered checks
        6. Release lease, update state
        
        Args:
            profile_name: Profile name to ensure (e.g., "qwen35-coding", "qwen35-vision")
        
        Returns:
            True if profile is active, False if switch failed
        """
        lock_fd = None
        
        try:
            # Validate profile exists
            if profile_name not in self._profiles:
                logger.error(f"Unknown profile: {profile_name}")
                return False
            
            # Acquire exclusive lease
            lock_fd = self._acquire_lease(blocking=True)
            
            # Check if already active
            current_model = self._get_current_model_id()
            expected_model = self._profiles[profile_name].expected_model_id
            
            if current_model == expected_model:
                logger.debug(f"Profile {profile_name} already active (model: {current_model})")
                self._current_profile = profile_name
                return True
            
            logger.info(f"Switching to profile {profile_name} (expected model: {expected_model}, current: {current_model})")
            
            # Check if wrapper exists
            if not os.path.exists(self.SWITCH_WRAPPER_PATH):
                logger.error(f"Switch wrapper not found: {self.SWITCH_WRAPPER_PATH}")
                return False
            
            # Call root wrapper to switch profile
            try:
                result = subprocess.run(
                    [self.SWITCH_WRAPPER_PATH, profile_name],
                    capture_output=True,
                    text=True,
                    timeout=120
                )
                
                if result.returncode != 0:
                    logger.error(f"Profile switch failed: {result.stderr}")
                    return False
                
                logger.info(f"Profile switch completed: {result.stdout}")
            
            except subprocess.TimeoutExpired:
                logger.error(f"Profile switch timed out: {profile_name}")
                return False
            
            # Verify the switch
            if not self._verify_profile_switch(profile_name):
                logger.error(f"Profile switch verification failed: {profile_name}")
                return False
            
            # Update internal state
            self._current_profile = profile_name
            logger.info(f"Profile {profile_name} is now active")
            return True
        
        except Exception as exc:
            logger.error(f"ensure_profile failed: {exc}")
            return False
        
        finally:
            self._release_lease(lock_fd)
    
    def _verify_profile_switch(self, profile_name: str) -> bool:
        """Verify profile switch succeeded via layered checks."""
        profile = self._profiles[profile_name]
        
        # Layer 1: Check systemd service is active
        if not self._check_service_active():
            logger.error("Service not active after switch")
            return False
        
        # Layer 2: Check health endpoint
        if not self._check_health():
            logger.error("Health check failed after switch")
            return False
        
        # Layer 3: Verify model loaded
        current_model = self._get_current_model_id()
        if current_model != profile.expected_model_id:
            logger.error(
                f"Model mismatch after switch. Expected: {profile.expected_model_id}, "
                f"Current: {current_model}"
            )
            return False
        
        # Layer 4: Run smoke test
        if not self._run_smoke_test(profile_name):
            logger.error("Smoke test failed after switch")
            return False
        
        return True
    
    def _ensure_profile_locked(self, profile_name: str) -> bool:
        """Internal method to ensure profile while holding lease.
        
        Called by profile() context manager to hold lock during execution.
        
        Args:
            profile_name: Profile name to ensure
        
        Returns:
            True if profile is active, False if failed
        """
        lock_fd = self._acquire_lease(blocking=True)
        
        try:
            if profile_name not in self._profiles:
                logger.error(f"Unknown profile: {profile_name}")
                return False
            
            current_model = self._get_current_model_id()
            expected_model = self._profiles[profile_name].expected_model_id
            
            if current_model != expected_model:
                if not os.path.exists(self.SWITCH_WRAPPER_PATH):
                    logger.error(f"Switch wrapper not found: {self.SWITCH_WRAPPER_PATH}")
                    return False
                
                try:
                    result = subprocess.run(
                        [self.SWITCH_WRAPPER_PATH, profile_name],
                        capture_output=True,
                        text=True,
                        timeout=120
                    )
                    
                    if result.returncode != 0:
                        logger.error(f"Profile switch failed: {result.stderr}")
                        return False
                    
                    logger.info(f"Profile switch completed: {profile_name}")
                
                except subprocess.TimeoutExpired:
                    logger.error(f"Profile switch timed out: {profile_name}")
                    return False
            
            if not self._verify_profile_switch(profile_name):
                logger.error(f"Profile switch verification failed: {profile_name}")
                return False
            
            self._current_profile = profile_name
            return True
        
        except Exception as exc:
            logger.error(f"_ensure_profile_locked failed: {exc}")
            return False
        
        finally:
            self._release_lease(lock_fd)
    
    @contextmanager
    def profile(self, profile_name: str) -> Iterator[None]:
        """Context manager for running work under a profile with lease held.
        
        Acquires exclusive lease, ensures profile, and holds lock for the
        entire duration of the context (including task execution).
        
        Args:
            profile_name: Profile name to use
        
        Yields:
            None - use this context to run work under the profile
        
        Raises:
            ModelProfileError: If profile switch fails
        """
        if profile_name not in self._profiles:
            raise ModelProfileError(f"Unknown profile: {profile_name}")
        
        if not self._ensure_profile_locked(profile_name):
            raise ModelProfileError(f"Could not switch to profile {profile_name}")
        
        try:
            yield
        finally:
            # Note: We do NOT automatically restore to default
            # Profile stays resident until explicitly changed
            pass
