# orm/path_registry.py
# Copyright (C) 2005-2025 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: https://www.opensource.org/licenses/mit-license.php
"""Path tracking utilities, representing mapper graph traversals.

"""

from __future__ import annotations

from functools import reduce
from itertools import chain
import logging
import operator
from typing import Any
from typing import cast
from typing import Dict
from typing import Iterator
from typing import List
from typing import Optional
from typing import overload
from typing import Sequence
from typing import Tuple
from typing import TYPE_CHECKING
from typing import Union

from . import base as orm_base
from ._typing import insp_is_mapper_property
from .. import exc
from .. import util
from ..sql import visitors
from ..sql.cache_key import HasCacheKey

if TYPE_CHECKING:
    from ._typing import _InternalEntityType
    from .interfaces import StrategizedProperty
    from .mapper import Mapper
    from .relationships import RelationshipProperty
    from .util import AliasedInsp
    from ..sql.cache_key import _CacheKeyTraversalType
    from ..sql.elements import BindParameter
    from ..sql.visitors import anon_map
    from ..util.typing import _LiteralStar
    from ..util.typing import TypeGuard

    def is_root(path: PathRegistry) -> TypeGuard[RootRegistry]: ...

    def is_entity(path: PathRegistry) -> TypeGuard[AbstractEntityRegistry]: ...

else:
    is_root = operator.attrgetter("is_root")
    is_entity = operator.attrgetter("is_entity")


_SerializedPath = List[Any]
_StrPathToken = str
_PathElementType = Union[
    _StrPathToken, "_InternalEntityType[Any]", "StrategizedProperty[Any]"
]

# the representation is in fact
# a tuple with alternating:
# [_InternalEntityType[Any], Union[str, StrategizedProperty[Any]],
# _InternalEntityType[Any], Union[str, StrategizedProperty[Any]], ...]
# this might someday be a tuple of 2-tuples instead, but paths can be
# chopped at odd intervals as well so this is less flexible
_PathRepresentation = Tuple[_PathElementType, ...]

# NOTE: these names are weird since the array is 0-indexed,
# the "_Odd" entries are at 0, 2, 4, etc
_OddPathRepresentation = Sequence["_InternalEntityType[Any]"]
_EvenPathRepresentation = Sequence[Union["StrategizedProperty[Any]", str]]


log = logging.getLogger(__name__)


def _unreduce_path(path: _SerializedPath) -> PathRegistry:
    return PathRegistry.deserialize(path)


_WILDCARD_TOKEN: _LiteralStar = "*"
_DEFAULT_TOKEN = "_sa_default"


class PathRegistry(HasCacheKey):
    """Represent query load paths and registry functions.

    Basically represents structures like:

    (<User mapper>, "orders", <Order mapper>, "items", <Item mapper>)

    These structures are generated by things like
    query options (joinedload(), subqueryload(), etc.) and are
    used to compose keys stored in the query._attributes dictionary
    for various options.

    They are then re-composed at query compile/result row time as
    the query is formed and as rows are fetched, where they again
    serve to compose keys to look up options in the context.attributes
    dictionary, which is copied from query._attributes.

    The path structure has a limited amount of caching, where each
    "root" ultimately pulls from a fixed registry associated with
    the first mapper, that also contains elements for each of its
    property keys.  However paths longer than two elements, which
    are the exception rather than the rule, are generated on an
    as-needed basis.

    """

    __slots__ = ()

    is_token = False
    is_root = False
    has_entity = False
    is_property = False
    is_entity = False

    is_unnatural: bool

    path: _PathRepresentation
    natural_path: _PathRepresentation
    parent: Optional[PathRegistry]
    root: RootRegistry

    _cache_key_traversal: _CacheKeyTraversalType = [
        ("path", visitors.ExtendedInternalTraversal.dp_has_cache_key_list)
    ]

    def __eq__(self, other: Any) -> bool:
        try:
            return other is not None and self.path == other._path_for_compare
        except AttributeError:
            util.warn(
                "Comparison of PathRegistry to %r is not supported"
                % (type(other))
            )
            return False

    def __ne__(self, other: Any) -> bool:
        try:
            return other is None or self.path != other._path_for_compare
        except AttributeError:
            util.warn(
                "Comparison of PathRegistry to %r is not supported"
                % (type(other))
            )
            return True

    @property
    def _path_for_compare(self) -> Optional[_PathRepresentation]:
        return self.path

    def odd_element(self, index: int) -> _InternalEntityType[Any]:
        return self.path[index]  # type: ignore

    def set(self, attributes: Dict[Any, Any], key: Any, value: Any) -> None:
        log.debug("set '%s' on path '%s' to '%s'", key, self, value)
        attributes[(key, self.natural_path)] = value

    def setdefault(
        self, attributes: Dict[Any, Any], key: Any, value: Any
    ) -> None:
        log.debug("setdefault '%s' on path '%s' to '%s'", key, self, value)
        attributes.setdefault((key, self.natural_path), value)

    def get(
        self, attributes: Dict[Any, Any], key: Any, value: Optional[Any] = None
    ) -> Any:
        key = (key, self.natural_path)
        if key in attributes:
            return attributes[key]
        else:
            return value

    def __len__(self) -> int:
        return len(self.path)

    def __hash__(self) -> int:
        return id(self)

    if TYPE_CHECKING:
        @overload
        def __getitem__(self, entity: _StrPathToken) -> TokenRegistry: ...

        @overload
        def __getitem__(self, entity: int) -> _PathElementType: ...

        @overload
        def __getitem__(self, entity: slice) -> _PathRepresentation: ...

        @overload
        def __getitem__(
            self, entity: _InternalEntityType[Any]
        ) -> AbstractEntityRegistry: ...

        @overload
        def __getitem__(
            self, entity: StrategizedProperty[Any]
        ) -> PropRegistry: ...

    def __getitem__(
        self,
        entity: Union[
            _StrPathToken,
            int,
            slice,
            _InternalEntityType[Any],
            StrategizedProperty[Any],
        ],
    ) -> Union[
        TokenRegistry,
        _PathElementType,
        _PathRepresentation,
        PropRegistry,
        AbstractEntityRegistry,
    ]:
        raise NotImplementedError()

    # TODO: what are we using this for?
    @property
    def length(self) -> int:
        return len(self.path)

    def pairs(
        self,
    ) -> Iterator[
        Tuple[_InternalEntityType[Any], Union[str, StrategizedProperty[Any]]]
    ]:
        odd_path = cast(_OddPathRepresentation, self.path)
        even_path = cast(_EvenPathRepresentation, odd_path)
        for i in range(0, len(odd_path), 2):
            yield odd_path[i], even_path[i + 1]

    def contains_mapper(self, mapper: Mapper[Any]) -> bool:
        _m_path = cast(_OddPathRepresentation, self.path)
        for path_mapper in [_m_path[i] for i in range(0, len(_m_path), 2)]:
            if path_mapper.mapper.isa(mapper):
                return True
        else:
            return False

    def contains(self, attributes: Dict[Any, Any], key: Any) -> bool:
        return (key, self.path) in attributes

    def __reduce__(self) -> Any:
        return _unreduce_path, (self.serialize(),)

    @classmethod
    def _serialize_path(cls, path: _PathRepresentation) -> _SerializedPath:
        _m_path = cast(_OddPathRepresentation, path)
        _p_path = cast(_EvenPathRepresentation, path)

        return list(
            zip(
                tuple(
                    m.class_ if (m.is_mapper or m.is_aliased_class) else str(m)
                    for m in [_m_path[i] for i in range(0, len(_m_path), 2)]
                ),
                tuple(
                    p.key if insp_is_mapper_property(p) else str(p)
                    for p in [_p_path[i] for i in range(1, len(_p_path), 2)]
                )
                + (None,),
            )
        )

    @classmethod
    def _deserialize_path(cls, path: _SerializedPath) -> _PathRepresentation:
        def _deserialize_mapper_token(mcls: Any) -> Any:
            return (
                # note: we likely dont want configure=True here however
                # this is maintained at the moment for backwards compatibility
                orm_base._inspect_mapped_class(mcls, configure=True)
                if mcls not in PathToken._intern
                else PathToken._intern[mcls]
            )

        def _deserialize_key_token(mcls: Any, key: Any) -> Any:
            if key is None:
                return None
            elif key in PathToken._intern:
                return PathToken._intern[key]
            else:
                mp = orm_base._inspect_mapped_class(mcls, configure=True)
                assert mp is not None
                return mp.attrs[key]

        p = tuple(
            chain(
                *[
                    (
                        _deserialize_mapper_token(mcls),
                        _deserialize_key_token(mcls, key),
                    )
                    for mcls, key in path
                ]
            )
        )
        if p and p[-1] is None:
            p = p[0:-1]
        return p

    def serialize(self) -> _SerializedPath:
        path = self.path
        return self._serialize_path(path)

    @classmethod
    def deserialize(cls, path: _SerializedPath) -> PathRegistry:
        assert path is not None
        p = cls._deserialize_path(path)
        return cls.coerce(p)

    if TYPE_CHECKING:
        @overload
        @classmethod
        def per_mapper(cls, mapper: Mapper[Any]) -> CachingEntityRegistry: ...

        @overload
        @classmethod
        def per_mapper(cls, mapper: AliasedInsp[Any]) -> SlotsEntityRegistry: ...

    @classmethod
    def per_mapper(
        cls, mapper: _InternalEntityType[Any]
    ) -> AbstractEntityRegistry:
        if mapper.is_mapper:
            return CachingEntityRegistry(cls.root, mapper)
        else:
            return SlotsEntityRegistry(cls.root, mapper)

    @classmethod
    def coerce(cls, raw: _PathRepresentation) -> PathRegistry:
        def _red(prev: PathRegistry, next_: _PathElementType) -> PathRegistry:
            return prev[next_]

        # can't quite get mypy to appreciate this one :)
        return reduce(_red, raw, cls.root)  # type: ignore

    def __add__(self, other: PathRegistry) -> PathRegistry:
        def _red(prev: PathRegistry, next_: _PathElementType) -> PathRegistry:
            return prev[next_]

        return reduce(_red, other.path, self)

    def __str__(self) -> str:
        return f"ORM Path[{' -> '.join(str(elem) for elem in self.path)}]"

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.path!r})"


class CreatesToken(PathRegistry):
    __slots__ = ()

    is_aliased_class: bool
    is_root: bool

    def token(self, token: _StrPathToken) -> TokenRegistry:
        if token.endswith(f":{_WILDCARD_TOKEN}"):
            return TokenRegistry(self, token)
        elif token.endswith(f":{_DEFAULT_TOKEN}"):
            return TokenRegistry(self.root, token)
        else:
            raise exc.ArgumentError(f"invalid token: {token}")


class RootRegistry(CreatesToken):
    """Root registry, defers to mappers so that
    paths are maintained per-root-mapper.

    """

    __slots__ = ()

    inherit_cache = True

    path = natural_path = ()
    has_entity = False
    is_aliased_class = False
    is_root = True
    is_unnatural = False

    def _getitem(
        self, entity: Any
    ) -> Union[TokenRegistry, AbstractEntityRegistry]:
        if entity in PathToken._intern:
            if TYPE_CHECKING:
                assert isinstance(entity, _StrPathToken)
            return TokenRegistry(self, PathToken._intern[entity])
        else:
            try:
                return entity._path_registry  # type: ignore
            except AttributeError:
                raise IndexError(
                    f"invalid argument for RootRegistry.__getitem__: {entity}"
                )

    def _truncate_recursive(self) -> RootRegistry:
        return self

    if not TYPE_CHECKING:
        __getitem__ = _getitem


PathRegistry.root = RootRegistry()


class PathToken(orm_base.InspectionAttr, HasCacheKey, str):
    """cacheable string token"""

    _intern: Dict[str, PathToken] = {}

    def _gen_cache_key(
        self, anon_map: anon_map, bindparams: List[BindParameter[Any]]
    ) -> Tuple[Any, ...]:
        return (str(self),)

    @property
    def _path_for_compare(self) -> Optional[_PathRepresentation]:
        return None

    @classmethod
    def intern(cls, strvalue: str) -> PathToken:
        if strvalue in cls._intern:
            return cls._intern[strvalue]
        else:
            cls._intern[strvalue] = result = PathToken(strvalue)
            return result


class TokenRegistry(PathRegistry):
    __slots__ = ("token", "parent", "path", "natural_path")

    inherit_cache = True

    token: _StrPathToken
    parent: CreatesToken

    def __init__(self, parent: CreatesToken, token: _StrPathToken):
        token = PathToken.intern(token)

        self.token = token
        self.parent = parent
        self.path = parent.path + (token,)
        self.natural_path = parent.natural_path + (token,)

    has_entity = False

    is_token = True

    def generate_for_superclasses(self) -> Iterator[PathRegistry]:
        # NOTE: this method is no longer used.  consider removal
        parent = self.parent
        if is_root(parent):
            yield self
            return

        if TYPE_CHECKING:
            assert isinstance(parent, AbstractEntityRegistry)
        if not parent.is_aliased_class:
            for mp_ent in parent.mapper.iterate_to_root():
                yield TokenRegistry(parent.parent[mp_ent], self.token)
        elif (
            parent.is_aliased_class
            and cast(
                "AliasedInsp[Any]",
                parent.entity,
            )._is_with_polymorphic
        ):
            yield self
            for ent in cast(
                "AliasedInsp[Any]", parent.entity
            )._with_polymorphic_entities:
                yield TokenRegistry(parent.parent[ent], self.token)
        else:
            yield self

    def _generate_natural_for_superclasses(
        self,
    ) -> Iterator[_PathRepresentation]:
        parent = self.parent
        if is_root(parent):
            yield self.natural_path
            return

        if TYPE_CHECKING:
            assert isinstance(parent, AbstractEntityRegistry)
        for mp_ent in parent.mapper.iterate_to_root():
            yield TokenRegistry(parent.parent[mp_ent], self.token).natural_path
        if (
            parent.is_aliased_class
            and cast(
                "AliasedInsp[Any]",
                parent.entity,
            )._is_with_polymorphic
        ):
            yield self.natural_path
            for ent in cast(
                "AliasedInsp[Any]", parent.entity
            )._with_polymorphic_entities:
                yield (
                    TokenRegistry(parent.parent[ent], self.token).natural_path
                )
        else:
            yield self.natural_path

    def _getitem(self, entity: Any) -> Any:
        try:
            return self.path[entity]
        except TypeError as err:
            raise IndexError(f"{entity}") from err

    if not TYPE_CHECKING:
        __getitem__ = _getitem


class PropRegistry(PathRegistry):
    __slots__ = (
        "prop",
        "parent",
        "path",
        "natural_path",
        "has_entity",
        "entity",
        "mapper",
        "_wildcard_path_loader_key",
        "_default_path_loader_key",
        "_loader_key",
        "is_unnatural",
    )
    inherit_cache = True
    is_property = True

    prop: StrategizedProperty[Any]
    mapper: Optional[Mapper[Any]]
    entity: Optional[_InternalEntityType[Any]]

    def __init__(
        self, parent: AbstractEntityRegistry, prop: StrategizedProperty[Any]
    ):

        # restate this path in terms of the
        # given StrategizedProperty's parent.
        insp = cast("_InternalEntityType[Any]", parent[-1])
        natural_parent: AbstractEntityRegistry = parent

        # inherit "is_unnatural" from the parent
        self.is_unnatural = parent.parent.is_unnatural or bool(
            parent.mapper.inherits
        )

        if not insp.is_aliased_class or insp._use_mapper_path:  # type: ignore
            parent = natural_parent = parent.parent[prop.parent]
        elif (
            insp.is_aliased_class
            and insp.with_polymorphic_mappers
            and prop.parent in insp.with_polymorphic_mappers
        ):
            subclass_entity: _InternalEntityType[Any] = parent[-1]._entity_for_mapper(prop.parent)  # type: ignore  # noqa: E501
            parent = parent.parent[subclass_entity]

            # when building a path where with_polymorphic() is in use,
            # special logic to determine the "natural path" when subclass
            # entities are used.
            #
            # here we are trying to distinguish between a path that starts
            # on a with_polymorphic entity vs. one that starts on a
            # normal entity that introduces a with_polymorphic() in the
            # middle using of_type():
            #
            #  # as in test_polymorphic_rel->
            #  #    test_subqueryload_on_subclass_uses_path_correctly
            #  wp = with_polymorphic(RegularEntity, "*")
            #  sess.query(wp).options(someload(wp.SomeSubEntity.foos))
            #
            # vs
            #
            #  # as in test_relationship->JoinedloadWPolyOfTypeContinued
            #  wp = with_polymorphic(SomeFoo, "*")
            #  sess.query(RegularEntity).options(
            #       someload(RegularEntity.foos.of_type(wp))
            #       .someload(wp.SubFoo.bar)
            #   )
            #
            # in the former case, the Query as it generates a path that we
            # want to match will be in terms of the with_polymorphic at the
            # beginning.  in the latter case, Query will generate simple
            # paths that don't know about this with_polymorphic, so we must
            # use a separate natural path.
            #
            #
            if parent.parent:
                natural_parent = parent.parent[subclass_entity.mapper]
                self.is_unnatural = True
            else:
                natural_parent = parent
        elif (
            natural_parent.parent
            and insp.is_aliased_class
            and prop.parent  # this should always be the case here
            is not insp.mapper
            and insp.mapper.isa(prop.parent)
        ):
            natural_parent = parent.parent[prop.parent]

        self.prop = prop
        self.parent = parent
        self.path = parent.path + (prop,)
        self.natural_path = natural_parent.natural_path + (prop,)

        self.has_entity = prop._links_to_entity
        if prop._is_relationship:
            if TYPE_CHECKING:
                assert isinstance(prop, RelationshipProperty)
            self.entity = prop.entity
            self.mapper = prop.mapper
        else:
            self.entity = None
            self.mapper = None

        self._wildcard_path_loader_key = (
            "loader",
            parent.natural_path + self.prop._wildcard_token,
        )
        self._default_path_loader_key = self.prop._default_path_loader_key
        self._loader_key = ("loader", self.natural_path)

    def _truncate_recursive(self) -> PropRegistry:
        earliest = None
        for i, token in enumerate(reversed(self.path[:-1])):
            if token is self.prop:
                earliest = i

        if earliest is None:
            return self
        else:
            return self.coerce(self.path[0 : -(earliest + 1)])  # type: ignore

    @property
    def entity_path(self) -> AbstractEntityRegistry:
        assert self.entity is not None
        return self[self.entity]

    def _getitem(
        self, entity: Union[int, slice, _InternalEntityType[Any]]
    ) -> Union[AbstractEntityRegistry, _PathElementType, _PathRepresentation]:
        if isinstance(entity, (int, slice)):
            return self.path[entity]
        else:
            return SlotsEntityRegistry(self, entity)

    if not TYPE_CHECKING:
        __getitem__ = _getitem


class AbstractEntityRegistry(CreatesToken):
    __slots__ = (
        "key",
        "parent",
        "is_aliased_class",
        "path",
        "entity",
        "natural_path",
    )

    has_entity = True
    is_entity = True

    parent: Union[RootRegistry, PropRegistry]
    key: _InternalEntityType[Any]
    entity: _InternalEntityType[Any]
    is_aliased_class: bool

    def __init__(
        self,
        parent: Union[RootRegistry, PropRegistry],
        entity: _InternalEntityType[Any],
    ):
        self.key = entity
        self.parent = parent
        self.is_aliased_class = entity.is_aliased_class
        self.entity = entity
        self.path = parent.path + (entity,)

        # the "natural path" is the path that we get when Query is traversing
        # from the lead entities into the various relationships; it corresponds
        # to the structure of mappers and relationships. when we are given a
        # path that comes from loader options, as of 1.3 it can have ac-hoc
        # with_polymorphic() and other AliasedInsp objects inside of it, which
        # are usually not present in mappings.  So here we track both the
        # "enhanced" path in self.path and the "natural" path that doesn't
        # include those objects so these two traversals can be matched up.

        # the test here for "(self.is_aliased_class or parent.is_unnatural)"
        # are to avoid the more expensive conditional logic that follows if we
        # know we don't have to do it.   This conditional can just as well be
        # "if parent.path:", it just is more function calls.
        #
        # This is basically the only place that the "is_unnatural" flag
        # actually changes behavior.
        if parent.path and (self.is_aliased_class or parent.is_unnatural):
            # this is an infrequent code path used only for loader strategies
            # that also make use of of_type().
            if entity.mapper.isa(parent.natural_path[-1].mapper):  # type: ignore # noqa: E501
                self.natural_path = parent.natural_path + (entity.mapper,)
            else:
                self.natural_path = parent.natural_path + (
                    parent.natural_path[-1].entity,  # type: ignore
                )
        # it seems to make sense that since these paths get mixed up
        # with statements that are cached or not, we should make
        # sure the natural path is cacheable across different occurrences
        # of equivalent AliasedClass objects.  however, so far this
        # does not seem to be needed for whatever reason.
        # elif not parent.path and self.is_aliased_class:
        #     self.natural_path = (self.entity._generate_cache_key()[0], )
        else:
            self.natural_path = self.path

    def _truncate_recursive(self) -> AbstractEntityRegistry:
        return self.parent._truncate_recursive()[self.entity]

    @property
    def root_entity(self) -> _InternalEntityType[Any]:
        return self.odd_element(0)

    @property
    def entity_path(self) -> PathRegistry:
        return self

    @property
    def mapper(self) -> Mapper[Any]:
        return self.entity.mapper

    def __bool__(self) -> bool:
        return True

    def _getitem(
        self, entity: Any
    ) -> Union[_PathElementType, _PathRepresentation, PathRegistry]:
        if isinstance(entity, (int, slice)):
            return self.path[entity]
        elif entity in PathToken._intern:
            return TokenRegistry(self, PathToken._intern[entity])
        else:
            return PropRegistry(self, entity)

    if not TYPE_CHECKING:
        __getitem__ = _getitem


class SlotsEntityRegistry(AbstractEntityRegistry):
    # for aliased class, return lightweight, no-cycles created
    # version
    inherit_cache = True


class _ERDict(Dict[Any, Any]):
    def __init__(self, registry: CachingEntityRegistry):
        self.registry = registry

    def __missing__(self, key: Any) -> PropRegistry:
        self[key] = item = PropRegistry(self.registry, key)

        return item


class CachingEntityRegistry(AbstractEntityRegistry):
    # for long lived mapper, return dict based caching
    # version that creates reference cycles

    __slots__ = ("_cache",)

    inherit_cache = True

    def __init__(
        self,
        parent: Union[RootRegistry, PropRegistry],
        entity: _InternalEntityType[Any],
    ):
        super().__init__(parent, entity)
        self._cache = _ERDict(self)

    def pop(self, key: Any, default: Any) -> Any:
        return self._cache.pop(key, default)

    def _getitem(self, entity: Any) -> Any:
        if isinstance(entity, (int, slice)):
            return self.path[entity]
        elif isinstance(entity, PathToken):
            return TokenRegistry(self, entity)
        else:
            return self._cache[entity]

    if not TYPE_CHECKING:
        __getitem__ = _getitem


if TYPE_CHECKING:

    def path_is_entity(
        path: PathRegistry,
    ) -> TypeGuard[AbstractEntityRegistry]: ...

    def path_is_property(path: PathRegistry) -> TypeGuard[PropRegistry]: ...

else:
    path_is_entity = operator.attrgetter("is_entity")
    path_is_property = operator.attrgetter("is_property")
