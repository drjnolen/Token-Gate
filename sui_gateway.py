"""GraphQL-first access to Sui mainnet.

The public Sui fullnodes no longer expose JSON-RPC.  This module keeps all
chain reads behind a small, typed boundary so callers can distinguish a real
zero/negative result from an unavailable provider.
"""

from __future__ import annotations

import base64
import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Iterator
from urllib.parse import urlsplit

import requests


DEFAULT_SUI_GRAPHQL_URL = "https://graphql.mainnet.sui.io/graphql"


class SuiGatewayError(RuntimeError):
    """Base class for indeterminate Sui provider results."""


class SuiDeadlineExceeded(SuiGatewayError):
    """The caller's overall operation budget expired."""


class SuiGraphQLError(SuiGatewayError):
    """A GraphQL response contained errors instead of usable data."""

    def __init__(self, errors: list[dict[str, Any]], *, endpoint: str):
        self.errors = errors
        self.endpoint = endpoint
        messages = "; ".join(str(error.get("message", "unknown error")) for error in errors)
        super().__init__(f"Sui GraphQL error from {endpoint}: {messages}")

    @property
    def codes(self) -> set[str]:
        return {
            str((error.get("extensions") or {}).get("code", ""))
            for error in self.errors
            if isinstance(error, dict)
        }


@dataclass
class _ProviderState:
    failures: int = 0
    open_until: float = 0.0


def encode_personal_message(message: str) -> str:
    """Return Base64 message bytes for GraphQL ``PERSONAL_MESSAGE`` intent.

    The service applies the PersonalMessage BCS wrapper and intent itself.
    Supplying a pre-wrapped vector causes otherwise valid signatures to fail.
    """
    return base64.b64encode(message.encode("utf-8")).decode("ascii")


class SuiGraphQLGateway:
    """Small Sui GraphQL client with bounded retries and provider failover."""

    BALANCE_QUERY = """
        query Balance($owner: SuiAddress!, $coinType: String!) {
          address(address: $owner) {
            balance(coinType: $coinType) { totalBalance }
          }
        }
    """
    OWNED_OBJECTS_QUERY = """
        query OwnedObjects(
          $owner: SuiAddress!,
          $type: String,
          $first: Int!,
          $after: String
        ) {
          address(address: $owner) {
            objects(first: $first, after: $after, filter: {type: $type}) {
              pageInfo { hasNextPage endCursor }
              nodes {
                address
                contents { type { repr } json }
              }
            }
          }
        }
    """
    OBJECT_QUERY = """
        query Object($address: SuiAddress!) {
          object(address: $address) {
            address
            contents { type { repr } json }
          }
        }
    """
    DYNAMIC_FIELDS_QUERY = """
        query DynamicFields($parent: SuiAddress!, $first: Int!, $after: String) {
          object(address: $parent) {
            dynamicFields(first: $first, after: $after) {
              pageInfo { hasNextPage endCursor }
              nodes {
                name { type { repr } json }
                value {
                  __typename
                  ... on MoveObject {
                    address
                    contents { type { repr } json }
                  }
                  ... on MoveValue {
                    type { repr }
                    json
                  }
                }
              }
            }
          }
        }
    """
    VERIFY_SIGNATURE_QUERY = """
        query VerifySignature(
          $message: Base64!,
          $signature: Base64!,
          $author: SuiAddress!
        ) {
          verifySignature(
            message: $message,
            signature: $signature,
            intentScope: PERSONAL_MESSAGE,
            author: $author
          ) {
            success
          }
        }
    """
    CHAIN_IDENTIFIER_QUERY = "query ChainIdentifier { chainIdentifier }"

    def __init__(
        self,
        endpoints: Iterable[str] | None = None,
        *,
        timeout_seconds: float = 20.0,
        max_retries: int = 2,
        circuit_failures: int = 3,
        circuit_cooldown_seconds: float = 30.0,
        session: requests.Session | None = None,
    ):
        cleaned = [endpoint.strip().rstrip("/") for endpoint in (endpoints or []) if endpoint.strip()]
        self.endpoints = cleaned or [DEFAULT_SUI_GRAPHQL_URL]
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(1, max_retries)
        self.circuit_failures = max(1, circuit_failures)
        self.circuit_cooldown_seconds = max(1.0, circuit_cooldown_seconds)
        self._provided_session = session
        self._thread_local = threading.local()
        if session is not None:
            self._configure_session(session)
        self._states = {endpoint: _ProviderState() for endpoint in self.endpoints}
        self._state_lock = threading.Lock()

    def _configure_session(self, session: requests.Session) -> None:
        session.headers.update(
            {
                "Content-Type": "application/json",
                "User-Agent": "CityWatchBot/3.0",
            }
        )
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=max(4, len(self.endpoints) * 2),
            pool_maxsize=20,
        )
        session.mount("https://", adapter)

    def _session_for_thread(self) -> requests.Session:
        if self._provided_session is not None:
            return self._provided_session
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            self._configure_session(session)
            self._thread_local.session = session
        return session

    def _available_endpoints(self) -> list[str]:
        now = time.monotonic()
        with self._state_lock:
            return [
                endpoint
                for endpoint in self.endpoints
                if self._states[endpoint].open_until <= now
            ]

    def _circuit_retry_after(self) -> float:
        now = time.monotonic()
        with self._state_lock:
            return max(
                0.0,
                min(state.open_until for state in self._states.values()) - now,
            )

    def _record_success(self, endpoint: str) -> None:
        with self._state_lock:
            self._states[endpoint] = _ProviderState()

    def _record_failure(self, endpoint: str) -> None:
        with self._state_lock:
            state = self._states[endpoint]
            state.failures += 1
            if state.failures >= self.circuit_failures:
                state.open_until = time.monotonic() + self.circuit_cooldown_seconds

    def execute(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        operation_name: str,
        deadline_monotonic: float | None = None,
    ) -> dict[str, Any]:
        """Execute one operation, failing over only for indeterminate errors."""
        if (
            deadline_monotonic is not None
            and deadline_monotonic <= time.monotonic()
        ):
            raise SuiDeadlineExceeded(
                f"Sui GraphQL {operation_name} operation deadline exceeded"
            )
        last_error: Exception | None = None
        endpoints = self._available_endpoints()
        if not endpoints:
            retry_after = self._circuit_retry_after()
            raise SuiGatewayError(
                "All Sui GraphQL providers are circuit-open; "
                f"retry in {retry_after:.1f}s"
            )

        for endpoint in endpoints:
            for attempt in range(self.max_retries):
                try:
                    if deadline_monotonic is None:
                        request_timeout = self.timeout_seconds
                    else:
                        request_timeout = min(
                            self.timeout_seconds,
                            deadline_monotonic - time.monotonic(),
                        )
                        if request_timeout <= 0:
                            raise SuiDeadlineExceeded(
                                f"Sui GraphQL {operation_name} operation deadline exceeded"
                            )
                    response = self._session_for_thread().post(
                        endpoint,
                        json={"query": query, "variables": variables or {}},
                        timeout=(min(5.0, request_timeout), request_timeout),
                    )
                    response.raise_for_status()
                    payload = response.json()
                    errors = payload.get("errors") or []
                    if errors:
                        graphql_error = SuiGraphQLError(errors, endpoint=endpoint)
                        # Validation and bad input are deterministic. Retrying
                        # another provider would only add latency.
                        if graphql_error.codes & {
                            "BAD_USER_INPUT",
                            "GRAPHQL_PARSE_FAILED",
                            "GRAPHQL_VALIDATION_FAILED",
                        }:
                            raise graphql_error
                        raise graphql_error
                    data = payload.get("data")
                    if not isinstance(data, dict):
                        raise SuiGatewayError(
                            f"Sui GraphQL {operation_name} returned no data"
                        )
                    self._record_success(endpoint)
                    return data
                except SuiGraphQLError as exc:
                    if exc.codes & {
                        "BAD_USER_INPUT",
                        "GRAPHQL_PARSE_FAILED",
                        "GRAPHQL_VALIDATION_FAILED",
                    }:
                        raise
                    last_error = exc
                except SuiDeadlineExceeded:
                    raise
                except (
                    requests.RequestException,
                    ValueError,
                    SuiGatewayError,
                ) as exc:
                    last_error = exc

                self._record_failure(endpoint)
                if attempt + 1 < self.max_retries:
                    delay = min(0.25 * (2**attempt), 1.0)
                    response = getattr(last_error, "response", None)
                    retry_after = (
                        getattr(response, "headers", {}).get("Retry-After")
                        if response is not None
                        else None
                    )
                    if retry_after:
                        try:
                            delay = min(max(float(retry_after), 0.0), 10.0)
                        except (TypeError, ValueError):
                            pass
                    delay *= random.uniform(0.8, 1.2)
                    if deadline_monotonic is not None:
                        remaining = deadline_monotonic - time.monotonic()
                        if remaining <= 0:
                            raise SuiDeadlineExceeded(
                                f"Sui GraphQL {operation_name} operation deadline exceeded"
                            ) from last_error
                        delay = min(delay, remaining)
                    time.sleep(delay)
            logging.warning(
                "Sui GraphQL provider %s failed during %s: %s",
                endpoint,
                operation_name,
                last_error,
            )
        raise SuiGatewayError(
            f"All Sui GraphQL providers failed during {operation_name}: {last_error}"
        ) from last_error

    @staticmethod
    def _move_object(node: dict[str, Any]) -> dict[str, Any]:
        contents = node.get("contents") or {}
        type_info = contents.get("type") or {}
        return {
            "objectId": node.get("address") or "",
            "type": type_info.get("repr") or "",
            "content": {
                "dataType": "moveObject",
                "type": type_info.get("repr") or "",
                "fields": contents.get("json") or {},
            },
        }

    def get_balance_atomic(
        self,
        owner: str,
        coin_type: str,
        *,
        deadline_monotonic: float | None = None,
    ) -> int:
        data = self.execute(
            self.BALANCE_QUERY,
            {"owner": owner, "coinType": coin_type},
            operation_name="balance",
            deadline_monotonic=deadline_monotonic,
        )
        address = data.get("address")
        if address is None:
            return 0
        balance = address.get("balance")
        if balance is None:
            return 0
        return int(balance.get("totalBalance") or 0)

    def iter_owned_objects(
        self,
        owner: str,
        type_filter: str | None = None,
        *,
        page_size: int = 50,
        max_pages: int = 1000,
        max_items: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        item_count = 0
        for _ in range(max_pages):
            data = self.execute(
                self.OWNED_OBJECTS_QUERY,
                {
                    "owner": owner,
                    "type": type_filter or None,
                    "first": min(max(page_size, 1), 50),
                    "after": cursor,
                },
                operation_name="owned objects",
                deadline_monotonic=deadline_monotonic,
            )
            connection = ((data.get("address") or {}).get("objects") or {})
            nodes = connection.get("nodes") or []
            if max_items is not None and item_count + len(nodes) > max_items:
                raise SuiGatewayError("Owned-object pagination exceeded item safety limit")
            for node in nodes:
                item_count += 1
                yield self._move_object(node)
            page_info = connection.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                return
            cursor = page_info.get("endCursor")
            if not cursor:
                raise SuiGatewayError("Owned-object pagination omitted endCursor")
        raise SuiGatewayError("Owned-object pagination exceeded safety limit")

    def list_owned_objects(
        self,
        owner: str,
        type_filter: str | None = None,
        *,
        page_size: int = 50,
        max_pages: int = 1000,
        max_items: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> list[dict[str, Any]]:
        return list(
            self.iter_owned_objects(
                owner,
                type_filter,
                page_size=page_size,
                max_pages=max_pages,
                max_items=max_items,
                deadline_monotonic=deadline_monotonic,
            )
        )

    def get_object(
        self,
        address: str,
        *,
        deadline_monotonic: float | None = None,
    ) -> dict[str, Any] | None:
        data = self.execute(
            self.OBJECT_QUERY,
            {"address": address},
            operation_name="object",
            deadline_monotonic=deadline_monotonic,
        )
        node = data.get("object")
        return self._move_object(node) if node else None

    def iter_dynamic_fields(
        self,
        parent: str,
        *,
        page_size: int = 50,
        max_pages: int = 1000,
        max_items: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        item_count = 0
        for _ in range(max_pages):
            data = self.execute(
                self.DYNAMIC_FIELDS_QUERY,
                {
                    "parent": parent,
                    "first": min(max(page_size, 1), 50),
                    "after": cursor,
                },
                operation_name="dynamic fields",
                deadline_monotonic=deadline_monotonic,
            )
            connection = ((data.get("object") or {}).get("dynamicFields") or {})
            nodes = connection.get("nodes") or []
            if max_items is not None and item_count + len(nodes) > max_items:
                raise SuiGatewayError("Dynamic-field pagination exceeded item safety limit")
            for node in nodes:
                item_count += 1
                yield node
            page_info = connection.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                return
            cursor = page_info.get("endCursor")
            if not cursor:
                raise SuiGatewayError("Dynamic-field pagination omitted endCursor")
        raise SuiGatewayError("Dynamic-field pagination exceeded safety limit")

    def list_dynamic_fields(
        self,
        parent: str,
        *,
        page_size: int = 50,
        max_pages: int = 1000,
        max_items: int | None = None,
        deadline_monotonic: float | None = None,
    ) -> list[dict[str, Any]]:
        return list(
            self.iter_dynamic_fields(
                parent,
                page_size=page_size,
                max_pages=max_pages,
                max_items=max_items,
                deadline_monotonic=deadline_monotonic,
            )
        )

    def verify_personal_message(
        self,
        *,
        author: str,
        message: str,
        signature: str,
    ) -> bool:
        try:
            data = self.execute(
                self.VERIFY_SIGNATURE_QUERY,
                {
                    "message": encode_personal_message(message),
                    "signature": signature,
                    "author": author,
                },
                operation_name="signature verification",
            )
        except SuiGraphQLError as exc:
            if "BAD_USER_INPUT" in exc.codes:
                return False
            raise
        result = data.get("verifySignature")
        return bool(result and result.get("success"))

    def chain_identifier(self) -> str:
        data = self.execute(
            self.CHAIN_IDENTIFIER_QUERY,
            operation_name="chain identifier",
        )
        identifier = data.get("chainIdentifier")
        if not isinstance(identifier, str) or not identifier:
            raise SuiGatewayError("Sui GraphQL returned an empty chain identifier")
        return identifier

    @staticmethod
    def _endpoint_label(endpoint: str) -> str:
        parsed = urlsplit(endpoint)
        host = parsed.hostname or "unknown-provider"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return f"{host}{parsed.path or ''}"

    def provider_status(self) -> list[dict[str, Any]]:
        """Return credential-free circuit state for health/admin diagnostics."""
        now = time.monotonic()
        with self._state_lock:
            return [
                {
                    "provider": self._endpoint_label(endpoint),
                    "failures": state.failures,
                    "circuit_open": state.open_until > now,
                    "retry_after_seconds": round(max(0.0, state.open_until - now), 3),
                }
                for endpoint, state in self._states.items()
            ]
