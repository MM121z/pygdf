from collections import OrderedDict

import numpy as np

from numba import cuda

from .dataframe import DataFrame, Series
from .column import Column
from .buffer import Buffer


TEN_MB = 10 ** 7


class Appender(object):
    """For fast appending of data into a Column.
    """
    def __init__(self, parent, bufsize=TEN_MB):
        # Keep reference to parent Column
        self._parent = parent
        # Get physical dtype
        dtype = np.dtype(parent.data.dtype)
        # Max queue size is buffer size divided by itemsize
        self._max_q_sz = max(bufsize // dtype.itemsize, 1)
        self._queue = []
        # Initialize empty Column
        raw_buf = cuda.device_array(shape=0, dtype=dtype)
        self._result = Column(Buffer.from_empty(raw_buf))

    def append(self, value):
        self._queue.append(value)
        # Flush when queue is full
        if len(self._queue) >= self._max_q_sz:
            self.flush()

    def flush(self):
        # Append to Series
        buf = Buffer(np.asarray(self._queue, dtype=self._result.dtype))
        self._result = self._result.append(Column(buf))
        # Reset queue
        self._queue.clear()

    def get(self):
        self.flush()
        assert self._result is not None
        assert not self._result.has_null_mask
        col = self._result
        return Series(self._parent.replace(data=col.data, mask=None))


def _auto_generate_grouper_agg(members):
    def make_fun(f):
        return lambda self: self.agg(f)

    for k, f in members['_NAMED_FUNCTIONS'].items():
        fn = make_fun(f)
        fn.__name__ = k
        fn.__doc__ = """Compute the {} of each group

Returns
-------

result : DataFrame
""".format(k)
        members[k] = fn


class Grouper(object):
    _NAMED_FUNCTIONS = {'mean': Series.mean,
                        'std': Series.std,
                        'min': Series.min,
                        'max': Series.max,
                        'count': Series.count,
                        }

    def __init__(self, df, by):
        self._df = df
        self._by = [by] if isinstance(by, str) else list(by)
        self._val_columns = [idx for idx in self._df.columns
                             if idx not in self._by]

    def _form_groups(self, functors):
        """
        Parameters
        ----------
        functors: dict
            Contains key for column names and value for list of functors.

        """
        functors_mapping = OrderedDict()
        appenders = OrderedDict()
        # The "by" columns
        for k in self._by:
            appenders[k] = Appender(parent=self._df[k]._column)
        # The "value" columns
        for k, vs in functors.items():
            if k not in self._df.columns:
                raise NameError('column {:r} not found'.format(k))
            if len(vs) == 1:
                [functor] = vs
                appenders[k] = Appender(parent=self._df[k]._column)
                functors_mapping[k] = {k: functor}
            else:
                functors_mapping[k] = cur_fn_mapping = OrderedDict()
                for functor in vs:
                    newk = '{}_{}'.format(k, functor.__name__)
                    appenders[newk] = Appender(parent=self._df[k]._column)
                    cur_fn_mapping[newk] = functor
        # Grouping
        for idx, grp in self._group_level(self._df, self._by):
            for k, v in zip(self._by, idx):
                appenders[k].append(v)
            for k in grp.columns:
                for newk, functor in functors_mapping[k].items():
                    appenders[newk].append(functor(grp[k]))

        outdf = DataFrame()
        for k, app in appenders.items():
            outdf[k] = app.get()
        return outdf

    def _group_level(self, df, levels, indices=[]):
        """A generator that yields (indices, grouped_df).
        """
        col = levels[0]
        innerlevels = levels[1:]
        df = df.set_index(col).sort_index()
        segs = df.index.find_segments()
        for s, e in zip(segs, segs[1:] + [None]):
            grouped = df[s:e]
            if len(grouped):
                # NOTE: index at the Buffer level to get raw values
                # FIXME numpy.scalar getitem to Index
                #       (e.g. the need of `int(s)`)
                index = df.index.as_column().data[int(s)]
                inner_indices = indices + [index]
                if innerlevels:
                    for grp in self._group_level(grouped, innerlevels,
                                                 indices=inner_indices):
                        yield grp
                else:
                    yield inner_indices, grouped

    def agg(self, args):
        """Invoke aggregation functions on the groups.

        Parameters
        ----------
        args: dict, list, str, callable
            - str
                The aggregate function name.
            - callable
                The aggregate function.
            - list
                List of *str* or *callable* of the aggregate function.
            - dict
                key-value pairs of source column name and list of
                aggregate functions as *str* or *callable*.

        Returns
        -------
        result : DataFrame

        Notes
        -----
        """
        def _get_function(x):
            if isinstance(x, str):
                return self._NAMED_FUNCTIONS[x]
            else:
                return x

        functors = OrderedDict()
        if isinstance(args, (tuple, list)):
            for k in self._val_columns:
                functors[k] = [_get_function(x) for x in args]

        elif isinstance(args, dict):
            for k, v in args.items():
                functors[k] = ([_get_function(v)]
                               if not isinstance(v, (tuple, list))
                               else [_get_function(x) for x in v])
        else:
            return self.agg([args])
        return self._form_groups(functors)

    _auto_generate_grouper_agg(locals())
