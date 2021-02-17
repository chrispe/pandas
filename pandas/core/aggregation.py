"""
aggregation.py contains utility functions to handle multiple named and lambda
kwarg aggregations in groupby and DataFrame/Series aggregation
"""

from __future__ import annotations

from collections import defaultdict
from functools import partial
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    DefaultDict,
    Dict,
    Hashable,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
    cast,
)

from pandas._typing import (
    AggFuncType,
    AggFuncTypeBase,
    AggFuncTypeDict,
    Axis,
    FrameOrSeries,
    FrameOrSeriesUnion,
)

from pandas.core.dtypes.common import (
    is_dict_like,
    is_list_like,
)
from pandas.core.dtypes.generic import (
    ABCDataFrame,
    ABCSeries,
)

from pandas.core.algorithms import safe_sort
from pandas.core.base import SpecificationError
import pandas.core.common as com
from pandas.core.indexes.api import Index

if TYPE_CHECKING:
    from pandas.core.series import Series


def reconstruct_func(
    func: Optional[AggFuncType], **kwargs
) -> Tuple[bool, Optional[AggFuncType], Optional[List[str]], Optional[List[int]]]:
    """
    This is the internal function to reconstruct func given if there is relabeling
    or not and also normalize the keyword to get new order of columns.

    If named aggregation is applied, `func` will be None, and kwargs contains the
    column and aggregation function information to be parsed;
    If named aggregation is not applied, `func` is either string (e.g. 'min') or
    Callable, or list of them (e.g. ['min', np.max]), or the dictionary of column name
    and str/Callable/list of them (e.g. {'A': 'min'}, or {'A': [np.min, lambda x: x]})

    If relabeling is True, will return relabeling, reconstructed func, column
    names, and the reconstructed order of columns.
    If relabeling is False, the columns and order will be None.

    Parameters
    ----------
    func: agg function (e.g. 'min' or Callable) or list of agg functions
        (e.g. ['min', np.max]) or dictionary (e.g. {'A': ['min', np.max]}).
    **kwargs: dict, kwargs used in is_multi_agg_with_relabel and
        normalize_keyword_aggregation function for relabelling

    Returns
    -------
    relabelling: bool, if there is relabelling or not
    func: normalized and mangled func
    columns: list of column names
    order: list of columns indices

    Examples
    --------
    >>> reconstruct_func(None, **{"foo": ("col", "min")})
    (True, defaultdict(<class 'list'>, {'col': ['min']}), ('foo',), array([0]))

    >>> reconstruct_func("min")
    (False, 'min', None, None)
    """
    relabeling = func is None and is_multi_agg_with_relabel(**kwargs)
    columns: Optional[List[str]] = None
    order: Optional[List[int]] = None

    if not relabeling:
        if isinstance(func, list) and len(func) > len(set(func)):

            # GH 28426 will raise error if duplicated function names are used and
            # there is no reassigned name
            raise SpecificationError(
                "Function names must be unique if there is no new column names "
                "assigned"
            )
        elif func is None:
            # nicer error message
            raise TypeError("Must provide 'func' or tuples of '(column, aggfunc).")

    if relabeling:
        func, columns, order = normalize_keyword_aggregation(kwargs)

    return relabeling, func, columns, order


def is_multi_agg_with_relabel(**kwargs) -> bool:
    """
    Check whether kwargs passed to .agg look like multi-agg with relabeling.

    Parameters
    ----------
    **kwargs : dict

    Returns
    -------
    bool

    Examples
    --------
    >>> is_multi_agg_with_relabel(a="max")
    False
    >>> is_multi_agg_with_relabel(a_max=("a", "max"), a_min=("a", "min"))
    True
    >>> is_multi_agg_with_relabel()
    False
    """
    return all(isinstance(v, tuple) and len(v) == 2 for v in kwargs.values()) and (
        len(kwargs) > 0
    )


def normalize_keyword_aggregation(kwargs: dict) -> Tuple[dict, List[str], List[int]]:
    """
    Normalize user-provided "named aggregation" kwargs.
    Transforms from the new ``Mapping[str, NamedAgg]`` style kwargs
    to the old Dict[str, List[scalar]]].

    Parameters
    ----------
    kwargs : dict

    Returns
    -------
    aggspec : dict
        The transformed kwargs.
    columns : List[str]
        The user-provided keys.
    col_idx_order : List[int]
        List of columns indices.

    Examples
    --------
    >>> normalize_keyword_aggregation({"output": ("input", "sum")})
    (defaultdict(<class 'list'>, {'input': ['sum']}), ('output',), array([0]))
    """
    # Normalize the aggregation functions as Mapping[column, List[func]],
    # process normally, then fixup the names.
    # TODO: aggspec type: typing.Dict[str, List[AggScalar]]
    # May be hitting https://github.com/python/mypy/issues/5958
    # saying it doesn't have an attribute __name__
    aggspec: DefaultDict = defaultdict(list)
    order = []
    columns, pairs = list(zip(*kwargs.items()))

    for name, (column, aggfunc) in zip(columns, pairs):
        aggspec[column].append(aggfunc)
        order.append((column, com.get_callable_name(aggfunc) or aggfunc))

    # uniquify aggfunc name if duplicated in order list
    uniquified_order = _make_unique_kwarg_list(order)

    # GH 25719, due to aggspec will change the order of assigned columns in aggregation
    # uniquified_aggspec will store uniquified order list and will compare it with order
    # based on index
    aggspec_order = [
        (column, com.get_callable_name(aggfunc) or aggfunc)
        for column, aggfuncs in aggspec.items()
        for aggfunc in aggfuncs
    ]
    uniquified_aggspec = _make_unique_kwarg_list(aggspec_order)

    # get the new index of columns by comparison
    col_idx_order = Index(uniquified_aggspec).get_indexer(uniquified_order)
    return aggspec, columns, col_idx_order


def _make_unique_kwarg_list(
    seq: Sequence[Tuple[Any, Any]]
) -> Sequence[Tuple[Any, Any]]:
    """
    Uniquify aggfunc name of the pairs in the order list

    Examples:
    --------
    >>> kwarg_list = [('a', '<lambda>'), ('a', '<lambda>'), ('b', '<lambda>')]
    >>> _make_unique_kwarg_list(kwarg_list)
    [('a', '<lambda>_0'), ('a', '<lambda>_1'), ('b', '<lambda>')]
    """
    return [
        (pair[0], "_".join([pair[1], str(seq[:i].count(pair))]))
        if seq.count(pair) > 1
        else pair
        for i, pair in enumerate(seq)
    ]


# TODO: Can't use, because mypy doesn't like us setting __name__
#   error: "partial[Any]" has no attribute "__name__"
# the type is:
#   typing.Sequence[Callable[..., ScalarResult]]
#     -> typing.Sequence[Callable[..., ScalarResult]]:


def _managle_lambda_list(aggfuncs: Sequence[Any]) -> Sequence[Any]:
    """
    Possibly mangle a list of aggfuncs.

    Parameters
    ----------
    aggfuncs : Sequence

    Returns
    -------
    mangled: list-like
        A new AggSpec sequence, where lambdas have been converted
        to have unique names.

    Notes
    -----
    If just one aggfunc is passed, the name will not be mangled.
    """
    if len(aggfuncs) <= 1:
        # don't mangle for .agg([lambda x: .])
        return aggfuncs
    i = 0
    mangled_aggfuncs = []
    for aggfunc in aggfuncs:
        if com.get_callable_name(aggfunc) == "<lambda>":
            aggfunc = partial(aggfunc)
            aggfunc.__name__ = f"<lambda_{i}>"
            i += 1
        mangled_aggfuncs.append(aggfunc)

    return mangled_aggfuncs


def maybe_mangle_lambdas(agg_spec: Any) -> Any:
    """
    Make new lambdas with unique names.

    Parameters
    ----------
    agg_spec : Any
        An argument to GroupBy.agg.
        Non-dict-like `agg_spec` are pass through as is.
        For dict-like `agg_spec` a new spec is returned
        with name-mangled lambdas.

    Returns
    -------
    mangled : Any
        Same type as the input.

    Examples
    --------
    >>> maybe_mangle_lambdas('sum')
    'sum'
    >>> maybe_mangle_lambdas([lambda: 1, lambda: 2])  # doctest: +SKIP
    [<function __main__.<lambda_0>,
     <function pandas...._make_lambda.<locals>.f(*args, **kwargs)>]
    """
    is_dict = is_dict_like(agg_spec)
    if not (is_dict or is_list_like(agg_spec)):
        return agg_spec
    mangled_aggspec = type(agg_spec)()  # dict or OrderedDict

    if is_dict:
        for key, aggfuncs in agg_spec.items():
            if is_list_like(aggfuncs) and not is_dict_like(aggfuncs):
                mangled_aggfuncs = _managle_lambda_list(aggfuncs)
            else:
                mangled_aggfuncs = aggfuncs

            mangled_aggspec[key] = mangled_aggfuncs
    else:
        mangled_aggspec = _managle_lambda_list(agg_spec)

    return mangled_aggspec


def relabel_result(
    result: FrameOrSeries,
    func: Dict[str, List[Union[Callable, str]]],
    columns: Iterable[Hashable],
    order: Iterable[int],
) -> Dict[Hashable, Series]:
    """
    Internal function to reorder result if relabelling is True for
    dataframe.agg, and return the reordered result in dict.

    Parameters:
    ----------
    result: Result from aggregation
    func: Dict of (column name, funcs)
    columns: New columns name for relabelling
    order: New order for relabelling

    Examples:
    ---------
    >>> result = DataFrame({"A": [np.nan, 2, np.nan],
    ...       "C": [6, np.nan, np.nan], "B": [np.nan, 4, 2.5]})  # doctest: +SKIP
    >>> funcs = {"A": ["max"], "C": ["max"], "B": ["mean", "min"]}
    >>> columns = ("foo", "aab", "bar", "dat")
    >>> order = [0, 1, 2, 3]
    >>> _relabel_result(result, func, columns, order)  # doctest: +SKIP
    dict(A=Series([2.0, NaN, NaN, NaN], index=["foo", "aab", "bar", "dat"]),
         C=Series([NaN, 6.0, NaN, NaN], index=["foo", "aab", "bar", "dat"]),
         B=Series([NaN, NaN, 2.5, 4.0], index=["foo", "aab", "bar", "dat"]))
    """
    reordered_indexes = [
        pair[0] for pair in sorted(zip(columns, order), key=lambda t: t[1])
    ]
    reordered_result_in_dict: Dict[Hashable, Series] = {}
    idx = 0

    reorder_mask = not isinstance(result, ABCSeries) and len(result.columns) > 1
    for col, fun in func.items():
        s = result[col].dropna()

        # In the `_aggregate`, the callable names are obtained and used in `result`, and
        # these names are ordered alphabetically. e.g.
        #           C2   C1
        # <lambda>   1  NaN
        # amax     NaN  4.0
        # max      NaN  4.0
        # sum     18.0  6.0
        # Therefore, the order of functions for each column could be shuffled
        # accordingly so need to get the callable name if it is not parsed names, and
        # reorder the aggregated result for each column.
        # e.g. if df.agg(c1=("C2", sum), c2=("C2", lambda x: min(x))), correct order is
        # [sum, <lambda>], but in `result`, it will be [<lambda>, sum], and we need to
        # reorder so that aggregated values map to their functions regarding the order.

        # However there is only one column being used for aggregation, not need to
        # reorder since the index is not sorted, and keep as is in `funcs`, e.g.
        #         A
        # min   1.0
        # mean  1.5
        # mean  1.5
        if reorder_mask:
            fun = [
                com.get_callable_name(f) if not isinstance(f, str) else f for f in fun
            ]
            col_idx_order = Index(s.index).get_indexer(fun)
            s = s[col_idx_order]

        # assign the new user-provided "named aggregation" as index names, and reindex
        # it based on the whole user-provided names.
        s.index = reordered_indexes[idx : idx + len(fun)]
        reordered_result_in_dict[col] = s.reindex(columns, copy=False)
        idx = idx + len(fun)
    return reordered_result_in_dict


def validate_func_kwargs(
    kwargs: dict,
) -> Tuple[List[str], List[Union[str, Callable[..., Any]]]]:
    """
    Validates types of user-provided "named aggregation" kwargs.
    `TypeError` is raised if aggfunc is not `str` or callable.

    Parameters
    ----------
    kwargs : dict

    Returns
    -------
    columns : List[str]
        List of user-provied keys.
    func : List[Union[str, callable[...,Any]]]
        List of user-provided aggfuncs

    Examples
    --------
    >>> validate_func_kwargs({'one': 'min', 'two': 'max'})
    (['one', 'two'], ['min', 'max'])
    """
    tuple_given_message = "func is expected but received {} in **kwargs."
    columns = list(kwargs)
    func = []
    for col_func in kwargs.values():
        if not (isinstance(col_func, str) or callable(col_func)):
            raise TypeError(tuple_given_message.format(type(col_func).__name__))
        func.append(col_func)
    if not columns:
        no_arg_message = "Must provide 'func' or named aggregation **kwargs."
        raise TypeError(no_arg_message)
    return columns, func


def transform(
    obj: FrameOrSeries, func: AggFuncType, axis: Axis, *args, **kwargs
) -> FrameOrSeriesUnion:
    """
    Transform a DataFrame or Series

    Parameters
    ----------
    obj : DataFrame or Series
        Object to compute the transform on.
    func : string, function, list, or dictionary
        Function(s) to compute the transform with.
    axis : {0 or 'index', 1 or 'columns'}
        Axis along which the function is applied:

        * 0 or 'index': apply function to each column.
        * 1 or 'columns': apply function to each row.

    Returns
    -------
    DataFrame or Series
        Result of applying ``func`` along the given axis of the
        Series or DataFrame.

    Raises
    ------
    ValueError
        If the transform function fails or does not transform.
    """
    is_series = obj.ndim == 1

    if obj._get_axis_number(axis) == 1:
        assert not is_series
        return transform(obj.T, func, 0, *args, **kwargs).T

    if is_list_like(func) and not is_dict_like(func):
        func = cast(List[AggFuncTypeBase], func)
        # Convert func equivalent dict
        if is_series:
            func = {com.get_callable_name(v) or v: v for v in func}
        else:
            func = {col: func for col in obj}

    if is_dict_like(func):
        func = cast(AggFuncTypeDict, func)
        return transform_dict_like(obj, func, *args, **kwargs)

    # func is either str or callable
    func = cast(AggFuncTypeBase, func)
    try:
        result = transform_str_or_callable(obj, func, *args, **kwargs)
    except Exception:
        raise ValueError("Transform function failed")

    # Functions that transform may return empty Series/DataFrame
    # when the dtype is not appropriate
    if isinstance(result, (ABCSeries, ABCDataFrame)) and result.empty and not obj.empty:
        raise ValueError("Transform function failed")
    if not isinstance(result, (ABCSeries, ABCDataFrame)) or not result.index.equals(
        obj.index
    ):
        raise ValueError("Function did not transform")

    return result


def transform_dict_like(
    obj: FrameOrSeries,
    func: AggFuncTypeDict,
    *args,
    **kwargs,
):
    """
    Compute transform in the case of a dict-like func
    """
    from pandas.core.reshape.concat import concat

    if len(func) == 0:
        raise ValueError("No transform functions were provided")

    if obj.ndim != 1:
        # Check for missing columns on a frame
        cols = set(func.keys()) - set(obj.columns)
        if len(cols) > 0:
            cols_sorted = list(safe_sort(list(cols)))
            raise SpecificationError(f"Column(s) {cols_sorted} do not exist")

    # Can't use func.values(); wouldn't work for a Series
    if any(is_dict_like(v) for _, v in func.items()):
        # GH 15931 - deprecation of renaming keys
        raise SpecificationError("nested renamer is not supported")

    results: Dict[Hashable, FrameOrSeriesUnion] = {}
    for name, how in func.items():
        colg = obj._gotitem(name, ndim=1)
        try:
            results[name] = transform(colg, how, 0, *args, **kwargs)
        except Exception as err:
            if str(err) in {
                "Function did not transform",
                "No transform functions were provided",
            }:
                raise err

    # combine results
    if not results:
        raise ValueError("Transform function failed")
    return concat(results, axis=1)


def transform_str_or_callable(
    obj: FrameOrSeries, func: AggFuncTypeBase, *args, **kwargs
) -> FrameOrSeriesUnion:
    """
    Compute transform in the case of a string or callable func
    """
    if isinstance(func, str):
        return obj._try_aggregate_string_function(func, *args, **kwargs)

    if not args and not kwargs:
        f = obj._get_cython_func(func)
        if f:
            return getattr(obj, f)()

    # Two possible ways to use a UDF - apply or call directly
    try:
        return obj.apply(func, args=args, **kwargs)
    except Exception:
        return func(obj, *args, **kwargs)
