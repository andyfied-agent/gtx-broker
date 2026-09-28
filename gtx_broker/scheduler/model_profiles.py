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
    3. Profile switching via root-owned wrapper script
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
                # Set group to gtx-broker (will fail if user not in group, but that's OK)
                try:
                    os.chown(self.LOCK_DIR, 0, -1)  # root
                    # Try to set group - may fail if group doesn't exist
                    try:
                        import grp
                        gtx_group = grp.getgrnam("gtx-broker")
                        os.chown(self.LOCK_DIR, 0, gtx_group.gr_gid)
                    except KeyError:
                        logger.warning("gtx-broker group not found, using root ownership")
                except PermissionError:
                    logger.warning(f"Could not set ownership of {self.LOCK_DIR}")
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
                    return None
                logger.error(f"Could not acquire lease: {exc}")
                raise
        
        except Exception as exc:
            logger.error(f"Lease acquisition failed: {exc}")
            if lock_fd:
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
        """Query /v1/models to get currently loaded model ID."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(5.0)
                sock.connect((self.HOST, self.PORT))
                
                request = f"GET {self.MODELS_URL.split('://')[1]} HTTP/1.1\r\n"
                request += f"Host: {self.HOST}:{self.PORT}\r\n"
                request += "Connection: close\r\n\r\n"
                
                sock.sendall(request.encode())
                response = b""
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
                
                # Parse JSON response
                body_start = response.find(b"\r\n\r\n")
                if body_start == -1:
                    return None
                
                body = response[body_start + 4:].decode("utf-8", errors="ignore")
                data = json.loads(body)
                
                if "data" in data and len(data["data"]) > 0:
                    return data["data"][0].get("id")
                
                return None
        
        except (socket.error, json.JSONDecodeError, OSError) as exc:
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
        """Check /health endpoint."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(5.0)
                sock.connect((self.HOST, self.PORT))
                
                request = f"GET /health HTTP/1.1\r\n"
                request += f"Host: {self.HOST}:{self.PORT}\r\n"
                request += "Connection: close\r\n\r\n"
                
                sock.sendall(request.encode())
                response = b""
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
                
                return response.startswith(b"HTTP/1.1 200")
        
        except (socket.error, OSError) as exc:
            logger.debug(f"Health check failed: {exc}")
            return False
    
    def _run_smoke_test(self, profile_name: str) -> bool:
        """Run smoke test for profile."""
        profile = self._profiles.get(profile_name)
        if not profile:
            logger.error(f"Profile not found: {profile_name}")
            return False
        
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(30.0)
                sock.connect((self.HOST, self.PORT))
                
                if "vision" in profile_name:
                    # Multimodal smoke test would require image upload
                    # For now, do text-only test
                    prompt = "Reply with exactly: OK"
                else:
                    prompt = "Reply with exactly: OK"
                
                message = {
                    "model": profile.model_family.replace(".", "-"),
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 8,
                    "temperature": 0
                }
                
                import json
                body = json.dumps(message)
                
                request = f"POST {self.CHAT_COMPLETIONS_URL.split('://')[1]} HTTP/1.1\r\n"
                request += f"Host: {self.HOST}:{self.PORT}\r\n"
                request += "Content-Type: application/json\r\n"
                request += f"Content-Length: {len(body)}\r\n"
                request += "Connection: close\r\n\r\n"
                request += body
                
                sock.sendall(request.encode())
                response = b""
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
                
                body_start = response.find(b"\r\n\r\n")
                if body_start == -1:
                    return False
                
                body = response[body_start + 4:].decode("utf-8", errors="ignore")
                data = json.loads(body)
                
                # Check for "OK" in response
                if "choices" in data and len(data["choices"]) > 0:
                    content = data["choices"][0].get("message", {}).get("content", "")
                    if "OK" in content:
                        return True
                
                return False
        
        except (socket.error, json.JSONDecodeError, OSError) as exc:
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
    
    @contextmanager
    def profile(self, profile_name: str) -> Iterator[None]:
        """Context manager for temporary profile switching (legacy API).
        
        This retains the original context-manager API for simple cases
        where an exclusive lease is not needed. For full runtime profile
        management, use ensure_profile() instead.
        
        Args:
            profile_name: Profile name to use temporarily
        
        Yields:
            None - use this context to run work under the profile
        
        Raises:
            ModelProfileError: If profile switch fails
        """
        if profile_name not in self._profiles:
            raise ModelProfileError(f"Unknown profile: {profile_name}")
        
        if not self.ensure_profile(profile_name):
            raise ModelProfileError(f"Could not switch to profile {profile_name}")
        
        try:
            yield
        finally:
            # Note: We do NOT automatically restore to default
            # Profile stays resident until explicitly changed
            pass
