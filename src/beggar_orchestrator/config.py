from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .providers import CLIProvider, PROVIDER_TYPES
from .runtime import EventHandler, ProviderRuntime, Route, RouteTarget


logger = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Settings:
    providers: dict[str, dict[str, Any]]
    routes: dict[str, dict[str, Any]]


@lru_cache(maxsize=None)
def _load_config_file(absolute_path: str) -> Settings:
    """Read and parse each absolute config path at most once per process."""
    with Path(absolute_path).open("rb") as handle:
        payload = tomllib.load(handle)
    providers = payload.get("providers") or {}
    routes = payload.get("routes") or {}
    if not routes:
        raise ConfigError("Configuration has no routes")
    logger.info(
        "orchestrator_config_loaded path=%s providers=%s routes=%s",
        absolute_path,
        sorted(providers),
        sorted(routes),
    )
    return Settings(providers=providers, routes=routes)


def load_config(path: str | Path) -> Settings:
    # Normalize equivalent relative/absolute paths into one cache key. abspath is
    # lexical and does not reopen the target config file.
    absolute_path = os.path.abspath(os.fspath(path))
    return _load_config_file(absolute_path)


def _keyring():
    try:
        import keyring
    except ImportError as exc:
        raise ConfigError("Install beggar-orchestrator[auth] to use Keychain") from exc
    return keyring


def read_token(reference: str) -> str | None:
    if reference.startswith("env:"):
        return os.getenv(reference.removeprefix("env:"))
    if reference.startswith("keyring:"):
        account = reference.removeprefix("keyring:").strip()
        if not account:
            raise ConfigError("Keyring credential is missing an account name")
        return _keyring().get_password("beggar-orchestrator", account)
    raise ConfigError("Credential must use env:VARIABLE or keyring:ACCOUNT")


def store_token(provider: str, token: str) -> None:
    _keyring().set_password("beggar-orchestrator", provider, token)


def delete_token(provider: str) -> None:
    keyring = _keyring()
    try:
        keyring.delete_password("beggar-orchestrator", provider)
    except keyring.errors.PasswordDeleteError:
        pass


def _build_runtime(
    settings: Settings,
    *,
    system: str,
    on_event: EventHandler | None = None,
) -> ProviderRuntime:
    providers = {}
    for name, values in settings.providers.items():
        if not values.get("enabled", True):
            logger.info("orchestrator_provider_disabled provider=%s", name)
            continue
        provider_type = values.get("type", name)
        provider_class = PROVIDER_TYPES.get(provider_type)
        if provider_class is None:
            raise ConfigError(f"Unsupported provider type {provider_type!r}")
        reference = values.get("credential")
        if reference is None and provider_class.requires_credential:
            reference = f"keyring:{name}"
        token = read_token(reference) if reference is not None else None
        try:
            providers[name] = provider_class.from_config(
                name=name,
                values=values,
                token=token,
            )
        except (TypeError, ValueError) as exc:
            logger.error(
                "orchestrator_provider_config_invalid provider=%s type=%s error_type=%s "
                "error=%s",
                name,
                provider_type,
                type(exc).__name__,
                str(exc).replace("\n", " ")[:300],
            )
            raise ConfigError(f"Invalid configuration for provider {name!r}: {exc}") from exc
        logger.info(
            "orchestrator_provider_configured provider=%s type=%s transport=%s model=%s "
            "credential_configured=%s",
            name,
            provider_type,
            "cli" if isinstance(providers[name], CLIProvider) else "http",
            providers[name].model,
            bool(token),
        )

    routes = {}
    for name, values in settings.routes.items():
        if "max_attempts" in values:
            raise ConfigError(
                f"Route {name!r} cannot define max_attempts; every Agent call now "
                "selects exactly one provider"
            )
        raw_targets = values.get("targets", [])
        if any("priority" in target for target in raw_targets):
            raise ConfigError(
                f"Route {name!r} cannot define target priority; providers are selected "
                "explicitly by the caller"
            )
        targets = [RouteTarget(**target) for target in raw_targets]
        if not targets:
            raise ConfigError(f"Route {name!r} has no targets")
        provider_names = [target.provider for target in targets]
        if len(provider_names) != len(set(provider_names)):
            raise ConfigError(f"Route {name!r} contains duplicate provider targets")
        routes[name] = Route(
            targets=targets,
            deadline_seconds=float(values.get("deadline_seconds", 60)),
        )
        logger.info(
            "orchestrator_route_configured route=%s providers=%s deadline_seconds=%s",
            name,
            provider_names,
            routes[name].deadline_seconds,
        )
    return ProviderRuntime(
        system=system,
        providers=providers,
        routes=routes,
        on_event=on_event,
    )


async def _verify(provider: str, config_path: str) -> int:
    runtime = _build_runtime(
        load_config(config_path),
        system="Verify that the configured LLM provider is reachable.",
    )
    instance = runtime.providers.get(provider)
    if instance is None:
        print(f"Provider {provider!r} is missing or disabled", file=sys.stderr)
        return 2
    try:
        result = await instance.verify()
    except Exception as exc:
        print(f"Verification failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        f"Verified {provider}; model={result.model} latency_ms={result.latency_ms} "
        f"tokens={result.usage.total_tokens}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage LLM provider credentials")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("set", "status", "delete"):
        child = subparsers.add_parser(command)
        child.add_argument("provider")
    verify = subparsers.add_parser("verify")
    verify.add_argument("provider")
    verify.add_argument("--config", default="providers.toml")
    args = parser.parse_args()

    if args.command == "set":
        token = getpass.getpass(f"API key for {args.provider}: ").strip()
        if not token:
            print("No credential entered", file=sys.stderr)
            return 2
        store_token(args.provider, token)
        print(f"Stored credential for {args.provider} in the operating-system keyring")
        return 0
    if args.command == "status":
        configured = bool(read_token(f"keyring:{args.provider}"))
        print(f"{args.provider}: {'configured' if configured else 'missing'}")
        return 0 if configured else 1
    if args.command == "delete":
        delete_token(args.provider)
        print(f"Deleted credential for {args.provider} from the operating-system keyring")
        return 0
    return asyncio.run(_verify(args.provider, args.config))


if __name__ == "__main__":
    raise SystemExit(main())
