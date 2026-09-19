"""Global IP Dataset boundary for explicit, auditable browser egress profiles."""
import json
import os
import re
from importlib import metadata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from udaan.config import settings
from udaan.contracts import DomainError


@dataclass(frozen=True)
class EgressProfile:
    id: str
    label: str
    provider: str
    proxy_server: str | None = None
    username_env: str | None = None
    password_env: str | None = None

    def public(self):
        return {"id": self.id, "label": self.label, "provider": self.provider,
                "routing": "configured" if self.proxy_server else "system"}

    def browser_proxy(self):
        if not self.proxy_server:
            return None
        proxy = {"server": self.proxy_server}
        for key, env_name in (("username", self.username_env), ("password", self.password_env)):
            if env_name:
                value = os.environ.get(env_name)
                if not value:
                    raise DomainError("NETWORK_PROFILE_UNAVAILABLE", f"Network profile {self.label} is missing its configured credential")
                proxy[key] = value
        return proxy


class GlobalIPDataset:
    """Read installed profile metadata; credentials stay in named environment variables."""

    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else settings().global_ip_dataset_path
        self.dataset = None

    def _configured(self):
        if not self.path:
            return []
        path = Path(self.path).expanduser()
        if not path.is_file():
            raise DomainError("GLOBAL_IP_DATASET_UNAVAILABLE", "Configured Global IP Dataset file is unavailable")
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise DomainError("GLOBAL_IP_DATASET_INVALID", "Configured Global IP Dataset cannot be read") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("profiles"), list):
            raise DomainError("GLOBAL_IP_DATASET_INVALID", "Global IP Dataset must contain a profiles list")
        provider = payload.get("provider")
        if not isinstance(provider, str) or not provider.strip() or len(provider) > 120:
            raise DomainError("GLOBAL_IP_DATASET_INVALID", "Global IP Dataset provider is missing")
        self.dataset = {"provider": provider.strip(), "version": str(payload.get("version") or "unversioned"),
                        "path": str(path.resolve())}
        profiles = []
        for item in payload["profiles"]:
            if not isinstance(item, dict):
                raise DomainError("GLOBAL_IP_DATASET_INVALID", "Global IP Dataset profile is invalid")
            identifier, label, server = item.get("id"), item.get("label"), item.get("proxy_server")
            if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", identifier) or identifier == "automatic":
                raise DomainError("GLOBAL_IP_DATASET_INVALID", "Global IP Dataset profile id is invalid")
            if not isinstance(label, str) or not label.strip() or len(label) > 120:
                raise DomainError("GLOBAL_IP_DATASET_INVALID", "Global IP Dataset profile label is invalid")
            parsed = urlsplit(server) if isinstance(server, str) else None
            if not parsed or parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise DomainError("GLOBAL_IP_DATASET_INVALID", "Network proxy must be an HTTP(S) or SOCKS5 endpoint without embedded credentials")
            envs = [item.get("username_env"), item.get("password_env")]
            if any(value is not None and (not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", value)) for value in envs):
                raise DomainError("GLOBAL_IP_DATASET_INVALID", "Network credential environment name is invalid")
            profiles.append(EgressProfile(identifier, label.strip(), provider.strip(), server, *envs))
        if len({profile.id for profile in profiles}) != len(profiles):
            raise DomainError("GLOBAL_IP_DATASET_INVALID", "Global IP Dataset profile ids must be unique")
        return profiles

    def profiles(self):
        automatic = EgressProfile("automatic", "Automatic", "system-network")
        return [automatic, *self._configured()]

    def get(self, identifier):
        profile = next((item for item in self.profiles() if item.id == identifier), None)
        if profile is None:
            raise DomainError("NETWORK_PROFILE_UNAVAILABLE", "Selected network profile is no longer available")
        return profile

    def status(self):
        profiles = self.profiles()
        installed = {}
        for package in ("geoip2", "maxminddb"):
            try:
                installed[package] = metadata.version(package)
            except metadata.PackageNotFoundError:
                pass
        routed = any(profile.proxy_server for profile in profiles)
        return {"adapter": f"{type(self).__module__}.{type(self).__name__}",
                "status": "CONNECTED" if installed or self.dataset else "NOT CONFIGURED",
                "provider": self.dataset["provider"] if self.dataset else "MaxMind GeoIP libraries" if installed else None,
                "type": "EGRESS PROFILES" if routed else "IP/GEOLOCATION DATA ONLY",
                "installed_packages": installed,
                "dataset": self.dataset, "profiles": [profile.public() for profile in profiles],
                "egress_capability": "PROVIDED" if routed else "NOT PROVIDED",
                "network": "CONFIGURED PROFILE" if routed else "SYSTEM DEFAULT"}
