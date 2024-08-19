# Copyright 2024 The Flax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import typing as tp
from functools import partial

import jax.tree_util as jtu

from flax.nnx.nnx import (
  filterlib,
  graph,
)
from flax.nnx.nnx import variables as variableslib
from flax.nnx.nnx.graph import GraphDef
from flax.nnx.nnx.object import Object, ObjectMeta
from flax.nnx.nnx.graph import GraphState, StateLeaf
from flax.nnx.nnx.state import State
from flax.typing import Key, Path, PathParts

A = tp.TypeVar('A')
B = tp.TypeVar('B')
M = tp.TypeVar('M', bound='Module')
S = tp.TypeVar('S', bound=tp.Union[GraphState, tuple[GraphState, ...]])
V = tp.TypeVar('V', bound=variableslib.Variable[tp.Any])
F = tp.TypeVar('F', bound=tp.Callable[..., tp.Any])

StateMapping = tp.Mapping[Path, tp.Any]
tuple_reduce = lambda xs, x: xs + (x,)
tuple_init = lambda: ()


class ModuleMeta(ObjectMeta):
  # we keep a trivial derived class just in case we need to
  # add more functionality in the future
  pass


class Module(Object, metaclass=ModuleMeta):
  """Base class for all neural network modules.

  Layers and models should subclass this class.

  ``Module``'s can contain submodules, and in this way can be nested in a tree
  structure. Submodules can be assigned as regular attributes inside the
  ``__init__`` method.

  You can define arbitrary "forward pass" methods on your ``Module`` subclass.
  While no methods are special-cased, ``__call__`` is a popular choice since
  you can call the ``Module`` directly::

    >>> from flax import nnx
    >>> import jax.numpy as jnp

    >>> class Model(nnx.Module):
    ...   def __init__(self, rngs):
    ...     self.linear1 = nnx.Linear(2, 3, rngs=rngs)
    ...     self.linear2 = nnx.Linear(3, 4, rngs=rngs)
    ...   def __call__(self, x):
    ...     x = self.linear1(x)
    ...     x = nnx.relu(x)
    ...     x = self.linear2(x)
    ...     return x

    >>> x = jnp.ones((1, 2))
    >>> model = Model(rngs=nnx.Rngs(0))
    >>> y = model(x)
  """

  def sow(
    self,
    variable_type: tp.Type[variableslib.Variable[tp.Any]],
    name: str,
    value: A,
    reduce_fn: tp.Callable[[B, A], B] = tuple_reduce,
    init_fn: tp.Callable[[], B] = tuple_init,  # type: ignore
  ) -> None:
    """``sow()`` can be used to collect intermediate values without
    the overhead of explicitly passing a container through each Module call.
    ``sow()`` stores a value in a new ``Module`` attribute, denoted by ``name``.
    The value will be wrapped by a :class:`Variable` of type ``variable_type``,
    which can be useful to filter for in :func:`split`, :func:`state` and
    :func:`pop`.

    By default the values are stored in a tuple and each stored value
    is appended at the end. This way all intermediates can be tracked when
    the same module is called multiple times.

    Example usage::

      >>> from flax import nnx
      >>> import jax.numpy as jnp

      >>> class Model(nnx.Module):
      ...   def __init__(self, rngs):
      ...     self.linear1 = nnx.Linear(2, 3, rngs=rngs)
      ...     self.linear2 = nnx.Linear(3, 4, rngs=rngs)
      ...   def __call__(self, x, add=0):
      ...     x = self.linear1(x)
      ...     self.sow(nnx.Intermediate, 'i', x+add)
      ...     x = self.linear2(x)
      ...     return x

      >>> x = jnp.ones((1, 2))
      >>> model = Model(rngs=nnx.Rngs(0))
      >>> assert not hasattr(model, 'i')

      >>> y = model(x)
      >>> assert hasattr(model, 'i')
      >>> assert len(model.i.value) == 1 # tuple of length 1
      >>> assert model.i.value[0].shape == (1, 3)

      >>> y = model(x, add=1)
      >>> assert len(model.i.value) == 2 # tuple of length 2
      >>> assert (model.i.value[0] + 1 == model.i.value[1]).all()

    Alternatively, a custom init/reduce function can be passed::

      >>> class Model(nnx.Module):
      ...   def __init__(self, rngs):
      ...     self.linear1 = nnx.Linear(2, 3, rngs=rngs)
      ...     self.linear2 = nnx.Linear(3, 4, rngs=rngs)
      ...   def __call__(self, x):
      ...     x = self.linear1(x)
      ...     self.sow(nnx.Intermediate, 'sum', x,
      ...              init_fn=lambda: 0,
      ...              reduce_fn=lambda prev, curr: prev+curr)
      ...     self.sow(nnx.Intermediate, 'product', x,
      ...              init_fn=lambda: 1,
      ...              reduce_fn=lambda prev, curr: prev*curr)
      ...     x = self.linear2(x)
      ...     return x

      >>> x = jnp.ones((1, 2))
      >>> model = Model(rngs=nnx.Rngs(0))

      >>> y = model(x)
      >>> assert (model.sum.value == model.product.value).all()
      >>> intermediate = model.sum.value

      >>> y = model(x)
      >>> assert (model.sum.value == intermediate*2).all()
      >>> assert (model.product.value == intermediate**2).all()

    Args:
      variable_type: The :class:`Variable` type for the stored value.
        Typically :class:`Intermediate` is used to indicate an
        intermediate value.
      name: A string denoting the ``Module`` attribute name, where
        the sowed value is stored.
      value: The value to be stored.
      reduce_fn: The function used to combine the existing value with the new
        value. The default is to append the value to a tuple.
      init_fn: For the first value stored, ``reduce_fn`` will be passed the result
        of ``init_fn`` together with the value to be stored. The default is an
        empty tuple.
    """
    if hasattr(self, name):
      variable = getattr(self, name)
      if not isinstance(variable, variableslib.Variable):
        raise ValueError(
          f"Expected '{name}' to be a Variable, got {type(variable).__name__}"
        )
      elif type(variable) != variable_type:
        raise ValueError(
          f"Expected '{name}' to be of type '{variable_type.__name__}', "
          f"got '{type(variable).__name__}'"
        )
      variable.raw_value = reduce_fn(variable.raw_value, value)
    else:
      reduced_value = reduce_fn(init_fn(), value)
      setattr(self, name, variable_type(reduced_value))

  def iter_modules(self) -> tp.Iterator[tuple[PathParts, Module]]:
    """Recursively iterates over all nested :class:`Module`'s of the current Module, including
    the current Module.

    ``iter_modules`` creates a generator that yields the path and the Module instance, where
    the path is a tuple of strings or integers representing the path to the Module from the
    root Module.

    Example::

      >>> from flax import nnx
      ...
      >>> class SubModule(nnx.Module):
      ...   def __init__(self, din, dout, rngs):
      ...     self.linear1 = nnx.Linear(din, dout, rngs=rngs)
      ...     self.linear2 = nnx.Linear(din, dout, rngs=rngs)
      ...
      >>> class Block(nnx.Module):
      ...   def __init__(self, din, dout, *, rngs: nnx.Rngs):
      ...     self.linear = nnx.Linear(din, dout, rngs=rngs)
      ...     self.submodule = SubModule(din, dout, rngs=rngs)
      ...     self.dropout = nnx.Dropout(0.5)
      ...     self.batch_norm = nnx.BatchNorm(10, rngs=rngs)
      ...
      >>> model = Block(2, 5, rngs=nnx.Rngs(0))
      >>> for path, module in model.iter_modules():
      ...   print(path, type(module).__name__)
      ...
      ('batch_norm',) BatchNorm
      ('dropout',) Dropout
      ('linear',) Linear
      ('submodule', 'linear1') Linear
      ('submodule', 'linear2') Linear
      ('submodule',) SubModule
      () Block
    """
    for path, value in graph.iter_graph(self):
      if isinstance(value, Module):
        yield path, value

  def iter_children(self) -> tp.Iterator[tuple[Key, Module]]:
    """Iterates over all children :class:`Module`'s of the current Module. This
    method is similar to :func:`iter_modules`, except it only iterates over the
    immediate children, and does not recurse further down.

    ``iter_children`` creates a generator that yields the key and the Module instance,
    where the key is a string representing the attribute name of the Module to access
    the corresponding child Module.

    Example::

      >>> from flax import nnx
      ...
      >>> class SubModule(nnx.Module):
      ...   def __init__(self, din, dout, rngs):
      ...     self.linear1 = nnx.Linear(din, dout, rngs=rngs)
      ...     self.linear2 = nnx.Linear(din, dout, rngs=rngs)
      ...
      >>> class Block(nnx.Module):
      ...   def __init__(self, din, dout, *, rngs: nnx.Rngs):
      ...     self.linear = nnx.Linear(din, dout, rngs=rngs)
      ...     self.submodule = SubModule(din, dout, rngs=rngs)
      ...     self.dropout = nnx.Dropout(0.5)
      ...     self.batch_norm = nnx.BatchNorm(10, rngs=rngs)
      ...
      >>> model = Block(2, 5, rngs=nnx.Rngs(0))
      >>> for path, module in model.iter_children():
      ...  print(path, type(module).__name__)
      ...
      batch_norm BatchNorm
      dropout Dropout
      linear Linear
      submodule SubModule
    """
    node_dict = graph.get_node_impl(self).node_dict(self)
    for key, value in node_dict.items():
      if isinstance(value, Module):
        yield key, value

  def set_attributes(
    self,
    *filters: filterlib.Filter,
    raise_if_not_found: bool = True,
    **attributes: tp.Any,
  ) -> None:
    """Sets the attributes of nested Modules including the current Module.
    If the attribute is not found in the Module, it is ignored.

    Example::

      >>> from flax import nnx
      ...
      >>> class Block(nnx.Module):
      ...   def __init__(self, din, dout, *, rngs: nnx.Rngs):
      ...     self.linear = nnx.Linear(din, dout, rngs=rngs)
      ...     self.dropout = nnx.Dropout(0.5, deterministic=False)
      ...     self.batch_norm = nnx.BatchNorm(10, use_running_average=False, rngs=rngs)
      ...
      >>> block = Block(2, 5, rngs=nnx.Rngs(0))
      >>> block.dropout.deterministic, block.batch_norm.use_running_average
      (False, False)
      >>> block.set_attributes(deterministic=True, use_running_average=True)
      >>> block.dropout.deterministic, block.batch_norm.use_running_average
      (True, True)

    ``Filter``'s can be used to set the attributes of specific Modules::

      >>> block = Block(2, 5, rngs=nnx.Rngs(0))
      >>> block.set_attributes(nnx.Dropout, deterministic=True)
      >>> # Only the dropout will be modified
      >>> block.dropout.deterministic, block.batch_norm.use_running_average
      (True, False)

    Args:
      *filters: Filters to select the Modules to set the attributes of.
      raise_if_not_found: If True (default), raises a ValueError if at least one attribute
        instance is not found in one of the selected Modules.
      **attributes: The attributes to set.
    """
    remaining_attributes = set(attributes.keys())
    if not filters:
      filters = (True,)
    predicates = tuple(map(filterlib.to_predicate, filters))
    for path, module in self.iter_modules():
      for predicate in predicates:
        if predicate(path, module):
          for name, value in attributes.items():
            if hasattr(module, name):
              if name in remaining_attributes:
                remaining_attributes.remove(name)
              setattr(module, name, value)
          break

    if remaining_attributes and raise_if_not_found:
      raise ValueError(
        f'Could not find at least one instance of the following attributes: {remaining_attributes}'
      )

  def train(self, **attributes):
    """Sets the Module to training mode.

    ``train`` uses ``set_attributes`` to recursively set attributes ``deterministic=False``
    and ``use_running_average=False`` of all nested Modules that have these attributes.
    Its primarily used to control the runtime behavior of the ``Dropout`` and ``BatchNorm``
    Modules.

    Example::

      >>> from flax import nnx
      ...
      >>> class Block(nnx.Module):
      ...   def __init__(self, din, dout, *, rngs: nnx.Rngs):
      ...     self.linear = nnx.Linear(din, dout, rngs=rngs)
      ...     # initialize Dropout and BatchNorm in eval mode
      ...     self.dropout = nnx.Dropout(0.5, deterministic=True)
      ...     self.batch_norm = nnx.BatchNorm(10, use_running_average=True, rngs=rngs)
      ...
      >>> block = Block(2, 5, rngs=nnx.Rngs(0))
      >>> block.dropout.deterministic, block.batch_norm.use_running_average
      (True, True)
      >>> block.train()
      >>> block.dropout.deterministic, block.batch_norm.use_running_average
      (False, False)

    Args:
      **attributes: additional attributes passed to ``set_attributes``.
    """
    return self.set_attributes(
      deterministic=False,
      use_running_average=False,
      **attributes,
      raise_if_not_found=False,
    )

  def eval(self, **attributes):
    """Sets the Module to evaluation mode.

    ``eval`` uses ``set_attributes`` to recursively set attributes ``deterministic=True``
    and ``use_running_average=True`` of all nested Modules that have these attributes.
    Its primarily used to control the runtime behavior of the ``Dropout`` and ``BatchNorm``
    Modules.

    Example::

      >>> from flax import nnx
      ...
      >>> class Block(nnx.Module):
      ...   def __init__(self, din, dout, *, rngs: nnx.Rngs):
      ...     self.linear = nnx.Linear(din, dout, rngs=rngs)
      ...     self.dropout = nnx.Dropout(0.5)
      ...     self.batch_norm = nnx.BatchNorm(10, rngs=rngs)
      ...
      >>> block = Block(2, 5, rngs=nnx.Rngs(0))
      >>> block.dropout.deterministic, block.batch_norm.use_running_average
      (False, False)
      >>> block.eval()
      >>> block.dropout.deterministic, block.batch_norm.use_running_average
      (True, True)

    Args:
      **attributes: additional attributes passed to ``set_attributes``.
    """
    return self.set_attributes(
      deterministic=True,
      use_running_average=True,
      **attributes,
      raise_if_not_found=False,
    )

  def __init_subclass__(cls, experimental_pytree: bool = False) -> None:
    super().__init_subclass__()

    if experimental_pytree:
      jtu.register_pytree_with_keys(
        cls,
        partial(_module_flatten, with_keys=True),
        _module_unflatten,  # type: ignore[arg-type]
        flatten_func=partial(_module_flatten, with_keys=False),
      )

  def __treescope_repr__(self, path, subtree_renderer):
    import treescope  # type: ignore[import-not-found,import-untyped]
    children = {}
    for name, value in vars(self).items():
      if name.startswith('_'):
        continue
      children[name] = value
    return treescope.repr_lib.render_object_constructor(
        object_type=type(self),
        attributes=children,
        path=path,
        subtree_renderer=subtree_renderer,
        color=treescope.formatting_util.color_from_string(
            type(self).__qualname__
        )
    )

# -------------------------
# Pytree Definition
# -------------------------
def _module_flatten(module: Module, *, with_keys: bool):
  graphdef, state = graph.split(module)
  key_values = sorted(state.raw_mapping.items())
  keys = tuple(key for key, _ in key_values)

  children: tuple[tp.Any, ...]
  if with_keys:
    children = tuple((jtu.DictKey(key), value) for key, value in key_values)
  else:
    children = tuple(value for _, value in key_values)

  return children, (keys, graphdef)


def _module_unflatten(
  paths_moduledef: tuple[tuple[Path, ...], GraphDef[M]],
  variables: tuple[StateLeaf, ...],
) -> M:
  paths, graphdef = paths_moduledef
  return graph.merge(graphdef, State(zip(paths, variables)))


def first_from(*args: tp.Optional[A], error_msg: str) -> A:
  """Return the first non-None argument.

  If all arguments are None, raise a ValueError with the given error message.

  Args:
    *args: the arguments to check
    error_msg: the error message to raise if all arguments are None
  Returns:
    The first non-None argument.
  """
  for arg in args:
    if arg is not None:
      return arg
  raise ValueError(error_msg)
