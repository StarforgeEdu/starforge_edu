"""A tiny IoC / dependency-injection container (clean-architecture foundation).

The wiring backbone for the layered style (views → service → repository → ORM):
services declare their repository dependencies as constructor type hints, and the
container resolves + injects them, so a view only does ``container.resolve(SomeService)``
and never news-up a repository by hand. Bindings map an INTERFACE (an abstract
repository / port) to a concrete implementation, so the data layer is swappable
(real ORM repo in prod, a fake in tests) without touching a single service.

Registrations live in ``core.bootstrap.configure_container`` (called once at startup).
"""

from __future__ import annotations

from typing import Any, TypeVar, get_type_hints

T = TypeVar("T")


class Container:
    """Constructor-injection IoC container with autowiring by type hints."""

    def __init__(self) -> None:
        self._bindings: dict[type, tuple[type, bool]] = {}
        self._singletons: dict[type, Any] = {}

    def register(self, interface: type, implementation: type, *, singleton: bool = True) -> Container:
        """Bind an abstract ``interface`` (port) to a concrete ``implementation``.

        ``singleton=True`` (the default) reuses one instance for the process —
        repositories are stateless, so this is both correct and cheap."""
        self._bindings[interface] = (implementation, singleton)
        return self

    def register_instance(self, interface: type, instance: Any) -> Container:
        """Bind an already-built instance (e.g. a configured client)."""
        self._singletons[interface] = instance
        self._bindings[interface] = (type(instance), True)
        return self

    def resolve(self, abstract: type[T]) -> T:
        """Return an instance for ``abstract``, autowiring its dependencies.

        A registered binding is honoured (and cached when singleton). A concrete
        class with no binding is built directly. An UNBOUND abstract base is a
        configuration error (raises) — it must be registered first."""
        if abstract in self._bindings:
            impl_class, is_singleton = self._bindings[abstract]
            if is_singleton and abstract in self._singletons:
                return self._singletons[abstract]
            instance = self._build(impl_class)
            if is_singleton:
                self._singletons[abstract] = instance
            return instance
        if not getattr(abstract, "__abstractmethods__", None):
            return self._build(abstract)
        raise LookupError(f"No binding registered for {abstract.__name__}")

    def _build(self, cls: type) -> Any:
        """Instantiate ``cls``, recursively resolving constructor type hints."""
        init = cls.__init__  # type: ignore[misc]
        if init is object.__init__:
            return cls()
        try:
            hints = get_type_hints(init)
        except Exception:
            return cls()
        hints.pop("return", None)
        return cls(**{name: self.resolve(hint) for name, hint in hints.items()})

    def is_registered(self, interface: type) -> bool:
        return interface in self._bindings

    def reset(self) -> None:
        """Drop cached singletons (tests rebind repositories with fakes)."""
        self._singletons.clear()


# The process-wide container. Populated once by core.bootstrap.configure_container.
container = Container()
