"""Environment service — list, inspect, create, edit, delete, set active."""

from __future__ import annotations

import json
import os
import stat as _stat_mod
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from platform_atlas.core.context import ctx
from platform_atlas.core.environment import (
    Environment,
    get_environment_manager,
    propagate_ssh_key,
    validate_env_name,
)
from platform_atlas.core.topology import DeploymentTopology
from platform_atlas.core.paths import ATLAS_ENVIRONMENTS_DIR


_TOPOLOGY_FORM_FIELDS = (
    "deployment_mode", "iap_host", "mongo_host", "redis_host", "iag_host",
    "saas_gateway_kind", "saas_gw4_ssh", "saas_gw5_same_host",
    "iap_host_ha", "iap_host_2", "iap_host_3",
    "mongo_host_ha", "mongo_host_2", "mongo_host_3",
    "redis_host_ha", "redis_host_2", "redis_host_3",
    "iag_host_ha",
    "gateway5_source", "gateway5_source_path",
    "ssh_user", "ssh_port",
    "iap_transport", "iap_cm_socket", "iap_cm_target", "iap_cm_port",
)


def _host_from_uri(uri: str) -> str:
    """Best-effort hostname extraction from a URI ('https://x.com:443/p' → 'x.com')."""
    if not uri:
        return ""
    raw = uri.strip()
    if not raw:
        return ""
    # urlparse needs a scheme to populate netloc; tolerate bare hosts too.
    if "://" not in raw:
        raw = "//" + raw
    try:
        parsed = urlparse(raw)
    except Exception:
        return ""
    return (parsed.hostname or "").strip()


_PRESERVED_TRANSPORTS: tuple[str, ...] = ("control_master", "local")


def _preserve_transport_fields(existing_node: dict, host: str, primary: bool) -> dict:
    """Carry over a non-SSH node's transport-specific fields (CM socket/target,
    local marker, etc.) while updating the host/primary slot. Strips SSH-only
    fields so the persisted node doesn't carry stale ssh_user/ssh_key/ssh_port."""
    preserved = dict(existing_node)
    preserved["host"] = host
    preserved["primary"] = primary
    for ssh_only in ("ssh_user", "ssh_key"):
        preserved.pop(ssh_only, None)
    # Keep ssh_port on ControlMaster nodes — it's the master connection port.
    if preserved.get("transport") != "control_master":
        preserved.pop("ssh_port", None)
    return preserved


def _build_saas_topology(payload: dict[str, Any]) -> dict | None:
    """Gateway-only topology for a SaaS env, from the form's gateway fields.

    GW4: an SSH node only when the optional SSH block is enabled and a host
    given — otherwise None (API-only audit; the capture engine synthesizes
    the single ipsdk target from ``gateway4_uri``). GW5: an SSH node, or a
    file-backed (``transport=gateway5_file``) node per the source picker.
    Mirrors the CLI's SaaS wizard.
    """
    kind = (payload.get("saas_gateway_kind") or "").strip().lower()
    ssh_user = (payload.get("ssh_user") or "atlas").strip() or "atlas"
    try:
        ssh_port = int((payload.get("ssh_port") or 22))
    except (TypeError, ValueError):
        ssh_port = 22
    ssh_key = (payload.get("ssh_key") or "").strip()
    ssh_auth_method = (payload.get("ssh_auth_method") or "key").strip().lower()
    if ssh_auth_method not in ("key", "password"):
        ssh_auth_method = "key"

    def _gw_ssh_node(host: str, modules: list[str]) -> dict[str, Any]:
        n: dict[str, Any] = {
            "role": "iag", "host": host, "label": "iag-01",
            "transport": "ssh", "ssh_user": ssh_user, "ssh_port": ssh_port,
            "modules": modules,
        }
        if ssh_auth_method == "password":
            n["ssh_auth_method"] = "password"
        elif ssh_key:
            n["ssh_key"] = ssh_key
        return n

    nodes: list[dict[str, Any]] = []
    if kind in ("gateway4", "gw4-gw5"):
        # Gateway 4 is ALWAYS audited over SSH (in addition to its REST API) —
        # the gateway SSH host is required, no API-only path.
        host = (payload.get("iag_host") or "").strip()
        if not host:
            raise ValueError(
                "A SaaS Gateway 4 environment requires an SSH host for the gateway "
                "server — it is audited over SSH in addition to its REST API."
            )
        nodes.append(_gw_ssh_node(host, ["system", "gateway4", "filesystem"]))

    if kind in ("gateway5", "gw4-gw5"):
        source = (payload.get("gateway5_source") or "").strip().lower()
        path = (payload.get("gateway5_source_path") or "").strip()
        conf_path = (payload.get("gateway5_conf_path") or "").strip()
        same_host = (payload.get("saas_gw5_same_host") or "").strip().lower() in ("1", "on", "true", "yes")
        gw4_host = (payload.get("iag_host") or "").strip()
        if source in ("compose", "helm") and path:
            # Local Compose/Helm file — parsed with no SSH/host access.
            nodes.append({
                "role": "iag", "host": "gateway5-file", "label": "iag5-file",
                "transport": "gateway5_file", "gateway5_source_path": path,
                "modules": ["gateway5"],
            })
        else:
            # SSH-based GW5 (printenv or the server gateway.conf). Host resolution:
            #  * GW4+GW5, same host → reuse the Gateway 4 host
            #  * GW4+GW5, separate  → the dedicated Gateway 5 SSH host
            #  * GW5-only           → the single gateway host
            if kind == "gw4-gw5":
                gw5_host = gw4_host if same_host else (payload.get("gw5_iag_host") or "").strip()
            else:
                gw5_host = gw4_host
            if gw5_host:
                node = _gw_ssh_node(gw5_host, ["system", "gateway5", "filesystem"])
                node["label"] = "iag5-01"
                if source == "conf":
                    node["gateway5_conf_path"] = conf_path
                nodes.append(node)
        gw5_count = sum(1 for n in nodes if "gateway5" in n.get("modules", []))
        if kind == "gateway5" and not gw5_count:
            raise ValueError(
                "A SaaS Gateway 5 environment needs a source — an SSH host (for "
                "printenv or the server gateway.conf), or a Compose / Helm file path."
            )
        if kind == "gw4-gw5" and not gw5_count:
            raise ValueError(
                "A GW4+GW5 environment needs a Gateway 5 source — a separate SSH host, "
                "the same host as Gateway 4, or a Compose / Helm file path."
            )

    if not nodes:
        return None  # Platform-only SaaS (no gateway) — no gateway topology.

    deployment: dict[str, Any] = {
        "mode": "gateway_only",
        # GW4 and GW5 both use the "iag" role, so primary_only (one node per
        # role) would silently drop the second gateway of a GW4+GW5 env. Use
        # all_nodes whenever more than one gateway node is present.
        "capture_scope": "all_nodes" if len(nodes) > 1 else "primary_only",
        "nodes": nodes,
    }
    if any(n.get("transport") == "ssh" for n in nodes):
        sd: dict[str, Any] = {"username": ssh_user, "port": ssh_port, "auth_method": ssh_auth_method}
        if ssh_auth_method == "key" and ssh_key:
            sd["key_path"] = ssh_key
        deployment["ssh_defaults"] = sd
    return deployment


def build_topology_from_form(payload: dict[str, Any], existing: dict | None = None) -> dict | None:
    """Construct a ``deployment`` dict from the env form's topology fields.

    Returns None for Standard envs (deployment is synthesized from
    ``platform_uri`` at capture time). For Extended envs, builds the
    deployment dict for standalone or HA2 modes; Kubernetes saves no
    SSH topology (the K8s collectors drive it via kubectl/values.yaml);
    Custom passes through the existing topology unchanged.

    When ``existing`` already has nodes whose transport is ``control_master``
    or ``local`` (configured via the CLI wizard), those transport fields are
    preserved across save — only the host fields are updated. The form's HA2
    pane explicitly tells the user this happens; the standalone pane silently
    extends the same protection to Mongo/Redis so a no-op WebUI save can't
    downgrade an entire PSMP-routed deployment back to plain SSH.
    """
    tier = (payload.get("tier") or "").strip().lower()
    if tier == "standard":
        return None
    if tier == "saas":
        return _build_saas_topology(payload)

    mode = (payload.get("deployment_mode") or "standalone").strip().lower()

    # Custom mode is CLI-managed — never overwrite from the WebUI form.
    if mode == "custom":
        return existing if isinstance(existing, dict) else None

    # Kubernetes mode doesn't use SSH host fields. The kubectl context and
    # values.yaml fields (handled separately as top-level env attrs) drive
    # capture there. Drop any prior SSH-style topology so it doesn't pretend
    # to still be in effect.
    if mode == "kubernetes":
        result = DeploymentTopology.kubernetes().to_dict()
        result["capture_scope"] = "primary_only"
        return result

    ssh_user = (payload.get("ssh_user") or "atlas").strip() or "atlas"
    try:
        ssh_port = int((payload.get("ssh_port") or 22))
    except (TypeError, ValueError):
        ssh_port = 22
    ssh_key = (payload.get("ssh_key") or "").strip()
    ssh_auth_method = (payload.get("ssh_auth_method") or "key").strip().lower()
    if ssh_auth_method not in ("key", "password"):
        ssh_auth_method = "key"

    # IAP transport: applies to the primary IAP node in standalone and HA2.
    # Secondary/tertiary HA2 IAP slots are preserved-from-existing (same as
    # before) — Atlas only uses the primary for protocol-level checks anyway.
    iap_transport = (payload.get("iap_transport") or "ssh").strip().lower()
    if iap_transport not in ("ssh", "control_master", "local"):
        iap_transport = "ssh"
    iap_cm_socket = (payload.get("iap_cm_socket") or "").strip()
    iap_cm_target = (payload.get("iap_cm_target") or "").strip()
    try:
        iap_cm_port = int((payload.get("iap_cm_port") or 22))
    except (TypeError, ValueError):
        iap_cm_port = 22

    # Required-field validation: without these the saved env would fail at
    # capture time inside ControlMasterConfig with the same message — surface
    # it at form-submit instead so the user can correct it immediately.
    if mode in ("standalone", "ha2") and iap_transport == "control_master":
        if not iap_cm_socket:
            raise ValueError(
                "ControlMaster socket path is required when 'Platform connection type' "
                "is ControlMaster."
            )
        if not iap_cm_target:
            raise ValueError(
                "ControlMaster SSH destination is required when 'Platform connection type' "
                "is ControlMaster."
            )

    if iap_cm_socket:
        # SEC-02: the socket path must not point at an existing non-socket file
        # (e.g. ~/.ssh/authorized_keys). A missing path is fine (ssh creates it).
        if "\0" in iap_cm_socket:
            raise ValueError("ControlMaster socket path contains invalid characters.")
        try:
            _mode = os.lstat(iap_cm_socket).st_mode
        except OSError:
            _mode = None
        if _mode is not None and not _stat_mod.S_ISSOCK(_mode):
            raise ValueError(
                f"ControlMaster socket path '{iap_cm_socket}' already exists and is not a socket. "
                "Choose a path that does not exist yet (ssh will create the socket there)."
            )

    fallback_host = _host_from_uri(payload.get("platform_uri") or "") or "localhost"

    existing_nodes_list: list[dict] = []
    if isinstance(existing, dict):
        for n in existing.get("nodes") or []:
            if isinstance(n, dict):
                existing_nodes_list.append(n)

    def _node(role: str, host: str, primary: bool) -> dict[str, Any]:
        n: dict[str, Any] = {
            "role": role, "host": host,
            "ssh_user": ssh_user, "ssh_port": ssh_port,
            "primary": primary,
        }
        if ssh_auth_method == "password":
            n["ssh_auth_method"] = "password"
        elif ssh_key:
            n["ssh_key"] = ssh_key
        return n

    def _existing_role_node(role: str, slot: int = 0) -> dict | None:
        """Return the slot-th existing node for this role (or None)."""
        matches = [
            n for n in existing_nodes_list
            if (n.get("role") or "").lower() == role
        ]
        return matches[slot] if slot < len(matches) else None

    def _iap_node(host: str, primary: bool) -> dict[str, Any]:
        """Build the IAP node respecting the chosen transport type."""
        if iap_transport == "local":
            return {"role": "iap", "host": host, "transport": "local", "primary": primary}
        if iap_transport == "control_master":
            n: dict[str, Any] = {
                "role": "iap", "host": host,
                "transport": "control_master",
                "primary": primary,
                "ssh_control_socket": iap_cm_socket,
                "ssh_control_target": iap_cm_target,
            }
            if iap_cm_port != 22:
                n["ssh_port"] = iap_cm_port
            return n
        return _node("iap", host, primary=primary)

    def _node_preserve(role: str, host: str, primary: bool, slot: int = 0) -> dict[str, Any]:
        """Build a node, preserving an existing CM/local transport for this role-slot.

        Falls through to a fresh SSH node when no matching existing node has a
        non-SSH transport — preserving today's behavior for SSH-only envs.
        """
        ex = _existing_role_node(role, slot)
        if ex is not None and ex.get("transport") in _PRESERVED_TRANSPORTS:
            return _preserve_transport_fields(ex, host, primary)
        return _node(role, host, primary=primary)

    def _append_gateway5_node(target_nodes: list[dict[str, Any]]) -> None:
        """Append the Gateway 5 node per the chosen source (only one is used).

        * compose/helm — an SSH-less, file-backed node (``transport=gateway5_file``)
          whose env vars are parsed from a local Compose/Helm file at capture time.
        * ssh          — a normal SSH IAG node (collects Gateway 5 via printenv,
          plus Gateway 4 over SSH via the IAG role defaults), built from ``iag_host``.
        * conf         — a normal SSH IAG node that reads the server's gateway.conf
          over SSH (``gateway5_conf_path``) instead of printenv.
        * legacy posts without the picker field fall back to ``iag_host`` == SSH,
          preserving behavior for older forms / direct API submissions.
        """
        source = (payload.get("gateway5_source") or "").strip().lower()
        path = (payload.get("gateway5_source_path") or "").strip()
        conf_path = (payload.get("gateway5_conf_path") or "").strip()
        ssh_host = (payload.get("iag_host") or "").strip()
        if source in ("compose", "helm") and path:
            target_nodes.append({
                "role": "iag",
                "host": "gateway5-file",
                "label": "iag5-file",
                "transport": "gateway5_file",
                "gateway5_source_path": path,
                "modules": ["gateway5"],
            })
        elif source == "conf" and ssh_host:
            node = _node_preserve("iag", ssh_host, primary=True)
            node["gateway5_conf_path"] = conf_path
            target_nodes.append(node)
        elif source == "ssh" and ssh_host:
            target_nodes.append(_node_preserve("iag", ssh_host, primary=True))
        elif "gateway5_source" not in payload and ssh_host:
            target_nodes.append(_node_preserve("iag", ssh_host, primary=True))

    nodes: list[dict[str, Any]] = []
    if mode == "ha2":
        # HA2 reads its own _ha-suffixed primary fields so the standalone and
        # HA2 panes can both have a "first IAP host" input without colliding.
        iap1 = (payload.get("iap_host_ha") or fallback_host).strip()
        iap2 = (payload.get("iap_host_2") or "").strip()
        iap3 = (payload.get("iap_host_3") or "").strip()
        mongo1 = (payload.get("mongo_host_ha") or "").strip()
        mongo2 = (payload.get("mongo_host_2") or "").strip()
        mongo3 = (payload.get("mongo_host_3") or "").strip()
        redis1 = (payload.get("redis_host_ha") or "").strip()
        redis2 = (payload.get("redis_host_2") or "").strip()
        redis3 = (payload.get("redis_host_3") or "").strip()

        # IAP slots: the form's transport selector applies to the PRIMARY
        # only (Atlas just uses the primary for protocol-level checks).
        # Secondary and tertiary IAP slots preserve whatever transport the
        # CLI wizard previously set for them.
        for i, h in enumerate((iap1, iap2, iap3)):
            if not h:
                continue
            if i == 0:
                nodes.append(_iap_node(h, primary=True))
            else:
                nodes.append(_node_preserve("iap", h, primary=False, slot=i))
        for i, h in enumerate((mongo1, mongo2, mongo3)):
            if h:
                nodes.append(_node_preserve("mongo", h, primary=(i == 0), slot=i))
        for i, h in enumerate((redis1, redis2, redis3)):
            if h:
                nodes.append(_node_preserve("redis", h, primary=(i == 0), slot=i))
        _append_gateway5_node(nodes)

        deployment: dict[str, Any] = {
            "mode": "ha2",
            "capture_scope": "primary_only",
            "nodes": nodes,
        }
    else:  # standalone (default)
        iap_host = (payload.get("iap_host") or fallback_host).strip()
        mongo_host = (payload.get("mongo_host") or iap_host).strip()
        redis_host = (payload.get("redis_host") or iap_host).strip()

        nodes = [
            _iap_node(iap_host, primary=True),
            _node_preserve("mongo", mongo_host, primary=True),
            _node_preserve("redis", redis_host, primary=True),
        ]
        _append_gateway5_node(nodes)

        deployment = {
            "mode": "standalone",
            "capture_scope": "primary_only",
            "nodes": nodes,
        }

    ssh_defaults: dict[str, Any] = {"username": ssh_user, "port": ssh_port, "auth_method": ssh_auth_method}
    if ssh_auth_method == "key" and ssh_key:
        ssh_defaults["key_path"] = ssh_key
    deployment["ssh_defaults"] = ssh_defaults
    return deployment


def topology_summary(env_data: dict | None) -> dict:
    """Pull a human-friendly topology summary out of ``env_data`` for the
    detail page card. Returns ``{configured: False}`` when no topology is
    set so the template can render an empty/seed state."""
    deployment = (env_data or {}).get("deployment") or {}
    if not deployment:
        return {"configured": False, "mode": "", "nodes": [], "has_control_master": False}
    mode = (deployment.get("mode") or "").strip()
    raw_nodes = deployment.get("nodes") or []
    nodes = []
    has_cm = False
    for n in raw_nodes:
        if not isinstance(n, dict):
            continue
        if (n.get("transport") or "") == "control_master":
            has_cm = True
        nodes.append({
            "role": str(n.get("role") or "").lower(),
            "host": str(n.get("host") or ""),
            "primary": bool(n.get("primary")),
        })
    return {"configured": True, "mode": mode, "nodes": nodes, "has_control_master": has_cm}


def cm_socket_status(env_data: dict | None) -> list[dict]:
    """Return socket status for every ControlMaster node in ``env_data``.

    Each entry: ``{label, status, path, target, ssh_cmd}`` where ``status``
    is one of ``ok | missing | stale | unconfigured``.
    """
    import stat as _stat
    import subprocess as _sp

    deployment = (env_data or {}).get("deployment") or {}
    raw_nodes = deployment.get("nodes") or []
    cm_nodes = [
        n for n in raw_nodes
        if isinstance(n, dict) and (n.get("transport") or "") == "control_master"
    ]

    try:
        from platform_atlas.core.context import ctx as _ctx
        persist = f"{_ctx().config.control_persist_minutes}m"
    except Exception:  # noqa: BLE001
        persist = "60m"

    results = []
    for node in cm_nodes:
        target = (node.get("ssh_control_target") or "").strip()
        path = (node.get("ssh_control_socket") or "").strip()
        label = (node.get("label") or node.get("role") or "node").strip()
        try:
            port = int(node.get("ssh_port") or 22)
        except (TypeError, ValueError):
            port = 22
        port_arg = f"-p {port} " if port != 22 else ""
        ssh_cmd = (
            f"ssh -M -S {path} {port_arg}"
            f"-o ControlPersist={persist} "
            f"-o StrictHostKeyChecking=accept-new "
            f"-o UserKnownHostsFile=/dev/null "
            f"-fN {target}"
        ) if path and target else ""

        if not target:
            results.append({"label": label, "status": "unconfigured", "path": path, "target": "", "ssh_cmd": ""})
            continue
        if not path:
            results.append({"label": label, "status": "missing", "path": "", "target": target, "ssh_cmd": ""})
            continue
        # lstat (no symlink following): only a real socket may be probed or
        # ever considered for cleanup. Regular files/dirs/symlinks are
        # reported as "not_socket" and are never deleted (SEC-02).
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            results.append({"label": label, "status": "missing", "path": path, "target": target, "ssh_cmd": ssh_cmd})
            continue
        except OSError:
            results.append({"label": label, "status": "not_socket", "path": path, "target": target, "ssh_cmd": ssh_cmd})
            continue
        if not _stat.S_ISSOCK(mode):
            results.append({"label": label, "status": "not_socket", "path": path, "target": target, "ssh_cmd": ssh_cmd})
            continue
        try:
            chk = _sp.run(
                ["ssh", "-O", "check", "-S", path, target],
                capture_output=True, text=True, timeout=5, check=False,
            )
            status = "ok" if chk.returncode == 0 else "stale"
        except Exception:  # noqa: BLE001
            status = "stale"
        results.append({"label": label, "status": status, "path": path, "target": target, "ssh_cmd": ssh_cmd})

    return results


def clean_stale_sockets(env_data: dict | None) -> dict:
    """Remove stale ControlMaster socket files from the filesystem.

    Returns ``{cleaned: int, errors: list[str]}``.
    """
    statuses = cm_socket_status(env_data)
    cleaned = 0
    errors: list[str] = []
    for s in statuses:
        if s["status"] == "stale" and s["path"]:
            p = Path(s["path"])
            try:
                # Re-check at delete time (no symlink following): never
                # unlink anything that is not a real socket.
                if not _stat_mod.S_ISSOCK(os.lstat(p).st_mode):
                    errors.append(f"Refusing to remove {s['path']}: not a socket")
                    continue
                p.unlink(missing_ok=True)
                cleaned += 1
            except OSError as exc:
                errors.append(f"Could not remove {s['path']}: {exc}")
    return {"cleaned": cleaned, "errors": errors}


def store_ssh_password(env_name: str, backend: str, password: str) -> None:
    """Write the SSH password into ``env_name``'s local secret store.

    Direct-substrate write (mirrors ``store_ssh_passphrase``): the env being
    edited may not be the active config, so ``active_secret_store()`` could
    target the wrong backend. Vault-backed envs must not arrive here.
    """
    from platform_atlas.core.credentials import (
        CredentialKey,
        FileSecretStore,
        KeyringSecretStore,
        scoped_service_name,
    )
    substrate = FileSecretStore() if (backend or "").strip().lower() == "file" else KeyringSecretStore()
    substrate.set(scoped_service_name(env_name), CredentialKey.SSH_PASSWORD.value, password)


def store_ssh_passphrase(env_name: str, backend: str, passphrase: str) -> None:
    """Write the SSH key passphrase into ``env_name``'s local secret store.

    Direct-substrate write (mirrors ``setup.bootstrap``): the env being
    edited may not be the active config, so ``active_secret_store()`` could
    target the wrong backend — write straight to the chosen substrate
    under the env-scoped service name. Capture reads it back through the
    same scoped service (``credential_store().get(SSH_PASSPHRASE)``).
    Vault-backed envs must not arrive here — Vault is read-only from Atlas.
    """
    from platform_atlas.core.credentials import (
        CredentialKey,
        FileSecretStore,
        KeyringSecretStore,
        scoped_service_name,
    )
    substrate = FileSecretStore() if (backend or "").strip().lower() == "file" else KeyringSecretStore()
    substrate.set(scoped_service_name(env_name), CredentialKey.SSH_PASSPHRASE.value, passphrase)


def list_environments() -> list[dict[str, Any]]:
    """Return all environment definitions as a list of plain dicts."""
    if not ATLAS_ENVIRONMENTS_DIR.is_dir():
        return []
    try:
        active_name = ctx().active_environment
    except Exception:
        active_name = None

    items: list[dict[str, Any]] = []
    for path in sorted(ATLAS_ENVIRONMENTS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        name = path.stem
        items.append({
            "name": name,
            "is_active": name == active_name,
            "organization_name": data.get("organization_name") or "",
            "platform_uri": data.get("platform_uri") or "",
            "tier": data.get("tier") or "extended",
            "deployment_mode": (data.get("deployment") or {}).get("mode") or "",
            "credential_backend": data.get("credential_backend") or "keyring",
        })
    return items


def get_environment(name: str) -> dict[str, Any] | None:
    """Return a single environment by name, or None if missing."""
    path: Path = ATLAS_ENVIRONMENTS_DIR / f"{name}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        active_name = ctx().active_environment
    except Exception:
        active_name = None
    return {
        "name": name,
        "data": data,
        "tier": data.get("tier") or "extended",
        "is_active": name == active_name,
    }


def save_environment(payload: dict[str, Any]) -> Environment:
    """Validate the payload, persist it, and return the Environment.

    The caller is responsible for routing to /environments/<name> on success.
    Raises ValueError on bad name, FileExistsError if creating a duplicate.
    """
    name = (payload.get("name") or "").strip()
    if not validate_env_name(name):
        raise ValueError(
            f"Invalid environment name '{name}'. Names must start with a "
            "letter or digit and may only contain letters, digits, spaces, "
            "dots, hyphens, and underscores (1–128 characters). Examples: "
            "'production', 'Acme Prod', 'dev-2', 'qa.staging'."
        )

    mgr = get_environment_manager()
    creating = not mgr.exists(name)

    # Build a clean dict — start from existing on edit so we don't drop fields
    # the form didn't include.
    base: dict[str, Any] = {}
    prior_tier = ""
    prior_kind = ""
    prior_gateway_kind = ""
    if not creating:
        existing = mgr.load(name)
        base = existing.to_dict()
        prior_tier = (base.get("tier") or "").strip().lower()
        prior_kind = (base.get("saas_gateway_kind") or "").strip().lower()
        prior_gateway_kind = (base.get("gateway_kind") or "").strip().lower()

    # Whitelist fields we accept from the form. Anything else is ignored.
    # ``organization_name`` is intentionally absent — it lives in the global
    # Configuration as the single source of truth, so the env form shows it
    # read-only and the WebUI never writes it into an env overlay.
    accepted = {
        "name", "description", "platform_uri",
        "platform_client_id", "credential_backend", "vault_secret_store", "tier",
        "saas_gateway_kind", "gateway_kind",
        "gateway4_uri", "gateway4_username",
        "ssh_key",
        "values_yaml_path", "iag5_values_yaml_path",
        "kubectl_context", "kubectl_namespace", "use_kubectl", "kubectl_binary_path",
        "deployment",
    }
    for key in accepted:
        if key in payload:
            base[key] = payload[key]

    # Coerce tier to None if blank — keeps it out of the overlay.
    if not base.get("tier"):
        base["tier"] = None

    # SaaS environments bind their tier and gateway kind at create time —
    # converting either direction would leave the env half-invalid.
    new_tier = (base.get("tier") or "").strip().lower()
    if not creating and "tier" in payload:
        if prior_tier == "saas" and new_tier != "saas":
            raise ValueError(
                "A SaaS environment's tier is fixed at create time — create a "
                "new environment for a Standard or Extended audit."
            )
        if prior_tier and prior_tier != "saas" and new_tier == "saas":
            raise ValueError(
                "An existing Standard/Extended environment can't be converted "
                "to SaaS — create a new SaaS environment instead."
            )
    if (not creating and prior_tier == "saas" and prior_kind
            and (base.get("saas_gateway_kind") or "").strip().lower() != prior_kind):
        raise ValueError(
            "A SaaS environment's gateway kind is fixed at create time — "
            "create a new environment to audit the other gateway."
        )

    # "gw4-gw5" (dual-gateway) can only be set on a new environment. Editing an
    # existing env that was ALREADY created as gw4-gw5 is fine (re-saving its own
    # value); only setting it for the first time on an existing env is blocked.
    if (not creating
            and (base.get("gateway_kind") or "").strip().lower() == "gw4-gw5"
            and prior_gateway_kind != "gw4-gw5"):
        raise ValueError(
            "Dual-gateway (GW4 + GW5) can only be configured on a new environment — "
            "create a new environment to audit both gateways together."
        )

    # Strip empty gateway_kind (the "No Gateway" radio submits an empty string).
    if not base.get("gateway_kind"):
        base.pop("gateway_kind", None)

    # saas_gateway_kind only means something for SaaS envs — keep it out of
    # other tiers' overlays. The gateway is now OPTIONAL for SaaS (Platform-only
    # is valid); when a gateway IS chosen it must be a known kind, and a GW4
    # audit needs the API URL. An empty kind = Platform-only → drop the marker.
    if (base.get("tier") or "").strip().lower() != "saas":
        base.pop("saas_gateway_kind", None)
    else:
        _saas_kind = (base.get("saas_gateway_kind") or "").strip().lower()
        if not _saas_kind:
            # Platform-only SaaS — no gateway to audit.
            base.pop("saas_gateway_kind", None)
        elif _saas_kind not in ("gateway4", "gateway5", "gw4-gw5"):
            raise ValueError(
                "A SaaS environment's gateway must be Gateway 4, Gateway 5, both, "
                "or none (Platform only)."
            )
        elif _saas_kind in ("gateway4", "gw4-gw5") and not (base.get("gateway4_uri") or "").strip():
            raise ValueError(
                "A SaaS Gateway 4 environment needs the Gateway 4 API URL — the "
                "API is the primary audit source (SSH is the optional supplement)."
            )

    # use_kubectl arrives as a string from the form
    base["use_kubectl"] = bool(base.get("use_kubectl")) and base.get("use_kubectl") not in ("0", "off", "false", "False", "")

    # Build / refresh the deployment topology from the form's topology fields
    # if any were posted. Skipping this for Standard tier (returns None) keeps
    # the env file slim — Standard captures synthesize their target list from
    # platform_uri at runtime and never read ``deployment``. For Extended this
    # is what stops captures from blowing up with "No 'deployment' section".
    posted_topo = any(k in payload for k in _TOPOLOGY_FORM_FIELDS)
    if posted_topo:
        new_topo = build_topology_from_form(payload, existing=base.get("deployment"))
        if new_topo is not None:
            base["deployment"] = new_topo
        elif (base.get("tier") or "").strip().lower() in ("standard", "saas"):
            # Standard never uses a topology; a SaaS build returning None is
            # an API-only GW4 audit — drop any stale topology either way so
            # it doesn't drift out of date silently.
            base.pop("deployment", None)
    elif creating and (base.get("tier") or "extended").strip().lower() not in ("standard", "saas") and not base.get("deployment"):
        # Brand-new Extended env with no topology fields posted → seed a
        # placeholder deployment so the first capture attempt doesn't error
        # out on missing 'deployment'. The user can refine it on edit.
        seeded = build_topology_from_form({**payload, "deployment_mode": "standalone"}, existing=None)
        if seeded is not None:
            base["deployment"] = seeded

    # Propagate ssh_key into the deployment topology so the transport layer
    # reads the updated path from nodes and ssh_defaults without requiring a
    # topology re-wizard.
    if "ssh_key" in payload and base.get("deployment"):
        base["deployment"] = propagate_ssh_key(base["deployment"], (payload["ssh_key"] or "").strip())

    env = Environment.from_dict(base)
    mgr.save(env)
    return env


def delete_environment(name: str) -> None:
    from platform_atlas.core import baseline_store

    mgr = get_environment_manager()
    mgr.remove(name)
    baseline_store.clear(name)


def set_active(name: str) -> None:
    mgr = get_environment_manager()
    mgr.set_active(name)
