"""Enhanced P40 Model Profile Controller with runtime profile management."""

from contextlib import contextmanager
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

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
    """Full Model-Profile Controller for P40 runtime profile management."""
    
    SWITCH_WRAPPER_PATH = "/usr/local/sbin/compute01-maint/p40-switch-profile"
    PROFILES_DIR = Path("/etc/llama-cpp/profiles")
    ACTIVE_CONFIG = Path("/etc/llama-cpp/p40-active.conf")
    LOCK_DIR = Path("/run/lock/gtx-broker")
    LOCK_FILE = LOCK_DIR / "p40-profile.lock"
    SERVICE_NAME = "llama-qwen35.service"
    HOST = "127.0.0.1"
    PORT = 11436
    
    def __init__(self):
        self._profiles: dict[str, ProfileMetadata] = {}
        self._current_profile: Optional[str] = None
        self._load_profiles_from_conf()
    
    @property
    def available_profiles(self) -> list[str]:
        return list(self._profiles.keys())
    
    @property
    def current_profile(self) -> Optional[str]:
        return self._current_profile
    
    def _load_profiles_from_conf(self) -> None:
        """Load profile metadata from .conf files (canonical source)."""
        if not self.PROFILES_DIR.exists():
            logger.warning(f"Profiles directory not found: {self.PROFILES_DIR}")
            return
        
        for conf_file in self.PROFILES_DIR.glob("*.conf"):
            profile_name = conf_file.stem
            try:
                profile_data = {}
                with open(conf_file, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("#") or "=" not in line:
                            continue
                        key, value = line.split("=", 1)
                        profile_data[key.strip()] = value.strip()
                
                self._profiles[profile_name] = ProfileMetadata(
                    description=f"Profile from {conf_file.name}",
                    model_family=profile_data.get("MODEL_ID", "").split("-")[0].replace(".", "-"),
                    role=profile_name.split("-")[1] if "-" in profile_name else "unknown",
                    quantization=profile_data.get("QUANTIZATION", ""),
                    gguf_path=profile_data.get("MODEL_PATH", ""),
                    expected_model_id=profile_data.get("MODEL_ID", ""),
                    expected_gguf_hash="",
                    projector_path=profile_data.get("MMPROJ_PATH"),
                    projector_type=profile_data.get("MMPROJ_TYPE"),
                    gpu_layers=int(profile_data.get("GPU_LAYERS", 99)),
                    context_size=int(profile_data.get("CONTEXT_SIZE", 262144)),
                    chat_template=profile_data.get("CHAT_TEMPLATE", ""),
                    slots=int(profile_data.get("SERVER_SLOTS", 4)),
                )
                logger.debug(f"Loaded profile: {profile_name} from {conf_file}")
            except (ValueError, KeyError) as exc:
                logger.error(f"Failed to parse profile config {conf_file}: {exc}")
    
    def _ensure_lock_dir(self) -> None:
        if not self.LOCK_DIR.exists():
            try:
                self.LOCK_DIR.mkdir(parents=True, mode=0o770)
            except OSError as exc:
                logger.error(f"Failed to create lock directory: {exc}")
                raise
    
    def _acquire_lease(self, blocking: bool = True) -> Optional[int]:
        self._ensure_lock_dir()
        lock_fd = None
        try:
            lock_fd = os.open(str(self.LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o660)
            import fcntl
            lock_type = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            try:
                fcntl.flock(lock_fd, lock_type)
                return lock_fd
            except (IOError, OSError):
                if not blocking:
                    if lock_fd is not None:
                        os.close(lock_fd)
                    return None
                raise
        except Exception:
            if lock_fd is not None:
                try:
                    os.close(lock_fd)
                except Exception:
                    pass
            raise
    
    def _release_lease(self, lock_fd: Optional[int]) -> None:
        if lock_fd is not None:
            try:
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except (IOError, OSError):
                pass
    
    def _get_current_model_id(self) -> Optional[str]:
        try:
            import urllib.request
            with urllib.request.urlopen(f"http://{self.HOST}:{self.PORT}/v1/models", timeout=5.0) as response:
                data = json.loads(response.read().decode("utf-8"))
                if "data" in data and len(data["data"]) > 0:
                    return data["data"][0].get("id")
        except Exception:
            pass
        return None
    
    def _check_service_active(self) -> bool:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "--quiet", self.SERVICE_NAME],
                capture_output=True, timeout=10
            )
            return result.returncode == 0
        except Exception:
            return False
    
    def _check_health(self) -> bool:
        try:
            import urllib.request
            with urllib.request.urlopen(f"http://{self.HOST}:{self.PORT}/health", timeout=5.0) as response:
                return response.status == 200
        except Exception:
            return False
    
    def _run_smoke_test(self, profile_name: str) -> bool:
        if profile_name not in self._profiles:
            return False
        
        profile = self._profiles[profile_name]
        
        if "vision" in profile_name:
            test_image = "/usr/local/share/gtx-broker/test-images/receipt_small.jpg"
            if not os.path.exists(test_image):
                logger.error("Vision test image not found: %s", test_image)
                return False
        
        try:
            import urllib.request
            import base64
            
            if "vision" in profile_name:
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
                message = {
                    "model": profile.expected_model_id,
                    "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
                    "max_tokens": 8,
                    "temperature": 0
                }
            
            data = json.dumps(message).encode("utf-8")
            req = urllib.request.Request(
                f"http://{self.HOST}:{self.PORT}/v1/chat/completions",
                data=data,
                headers={"Content-Type": "application/json"}
            )
            
            with urllib.request.urlopen(req, timeout=30.0) as response:
                result = json.loads(response.read().decode("utf-8"))
                if "choices" in result and len(result["choices"]) > 0:
                    content = result["choices"][0].get("message", {}).get("content", "")
                    if "vision" in profile_name:
                        return "29.24" in content
                    return "OK" in content
        except Exception:
            pass
        return False
    
    def _verify_profile_switch(self, profile_name: str) -> bool:
        if not self._check_service_active():
            return False
        if not self._check_health():
            return False
        
        profile = self._profiles.get(profile_name)
        if not profile:
            return False
        
        current_model = self._get_current_model_id()
        if current_model != profile.expected_model_id:
            return False
        
        if not self._run_smoke_test(profile_name):
            return False
        
        return True
    
    def _ensure_profile_under_lease(self, profile_name: str) -> bool:
        """Ensure profile is active (assumes caller holds lease)."""
        if profile_name not in self._profiles:
            logger.error(f"Unknown profile: {profile_name}")
            return False
        
        current_model = self._get_current_model_id()
        expected_model = self._profiles[profile_name].expected_model_id
        
        if current_model == expected_model:
            self._current_profile = profile_name
            return True
        
        if not os.path.exists(self.SWITCH_WRAPPER_PATH):
            logger.error(f"Switch wrapper not found: {self.SWITCH_WRAPPER_PATH}")
            return False
        
        try:
            result = subprocess.run(
                [self.SWITCH_WRAPPER_PATH, profile_name],
                capture_output=True, text=True, timeout=120
            )
            
            if result.returncode != 0:
                logger.error(f"Profile switch failed: {result.stderr}")
                return False
            
        except subprocess.TimeoutExpired:
            logger.error(f"Profile switch timed out: {profile_name}")
            return False
        
        if not self._verify_profile_switch(profile_name):
            return False
        
        self._current_profile = profile_name
        return True
    
    def ensure_profile(self, profile_name: str) -> bool:
        """Ensure profile is active (acquires own lease)."""
        lock_fd = self._acquire_lease(blocking=True)
        try:
            return self._ensure_profile_under_lease(profile_name)
        finally:
            self._release_lease(lock_fd)
    
    @contextmanager
    def profile(self, profile_name: str) -> Iterator[None]:
        """Context manager that holds lock during task execution."""
        if profile_name not in self._profiles:
            raise ModelProfileError(f"Unknown profile: {profile_name}")
        
        fd = self._acquire_lease(blocking=True)
        try:
            if not self._ensure_profile_under_lease(profile_name):
                raise ModelProfileError(f"Could not switch to profile {profile_name}")
            yield
        finally:
            self._release_lease(fd)
