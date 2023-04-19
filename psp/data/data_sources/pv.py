import abc
import copy
import datetime
import pathlib
from typing import TypeVar

import xarray as xr

from psp.typings import PvId, Timestamp
from psp.utils.dates import to_pydatetime

_ID = "pv_id"
_TS = "ts"

# https://peps.python.org/pep-0673/
_Self = TypeVar("_Self", bound="PvDataSource")


class PvDataSource(abc.ABC):
    """Definition of the interface for loading PV data."""

    @abc.abstractmethod
    def get(
        self,
        pv_ids: list[PvId] | PvId,
        start_ts: Timestamp | None = None,
        end_ts: Timestamp | None = None,
    ) -> xr.Dataset:
        # We assume that the returned Dataset has dimensions "id" and "ts".
        # Any number of coordinates or variables could be present - it will be up to the models to
        # know about the specifics of a given data source.
        pass

    @abc.abstractmethod
    def list_pv_ids(self) -> list[PvId]:
        pass

    @abc.abstractmethod
    def min_ts(self) -> Timestamp:
        pass

    @abc.abstractmethod
    def max_ts(self) -> Timestamp:
        pass

    @abc.abstractmethod
    def without_future(self: _Self, ts: Timestamp, *, blackout: int = 0) -> _Self:
        """Return a copy of the data source but without the data after `ts - blackout`.

        This is a intended as a safety mechanism when we want to make sure we can't use data after
        a certain point in time. In particular, we don't want to be able to use data from the
        future when training models.

        Arguments:
        ---------
            ts: The "now" timestamp, everything after is the future.
            blackout: A number of minutes before `ts` ("now") that we also want to ignore.
        """
        pass


def min_timestamp(a: Timestamp | None, b: Timestamp | None) -> Timestamp | None:
    """Util function to calculate the minimum between two timestamps that supports `None`.

    `None` values are assumed to be greater always.
    """
    if a is None:
        if b is None:
            return None
        else:
            return b
    else:
        # a is not None
        if b is None:
            return a
        else:
            return min(a, b)


class NetcdfPvDataSource(PvDataSource):
    def __init__(
        self,
        filepath: pathlib.Path | str,
        timestamp_dim_name: str = _TS,
        id_dim_name: str = _ID,
        rename: dict[str, str] | None = None,
    ):
        """
        Arguments:
        ---------
            filepath: File path of the netcdf file.
            timestamp_dim_name: Name for the timestamp dimensions in the dataset.
            id_dim_name: Name for the "id" dimensions in the dataset.
            rename: This is passed to `xarray` to
                rename any coordinates or variable.
        """
        if rename is None:
            rename = {}

        self._path = pathlib.Path(filepath)
        self._timestamp_dim_name = timestamp_dim_name
        self._id_dim_name = id_dim_name
        self._rename = rename

        self._open()

        self._set_max_ts(None)

    def _set_max_ts(self, ts: Timestamp | None) -> None:
        # See `ignore_future`.
        self._max_ts = ts

    def _open(self):
        # Xarray doesn't like trivial renamings so we build a mapping of what actually changes.
        rename_map: dict[str, str] = {}

        if self._id_dim_name != _ID:
            rename_map[self._id_dim_name] = _ID
        if self._timestamp_dim_name != _TS:
            rename_map[self._timestamp_dim_name] = _TS

        rename_map.update(self._rename)

        self._data = xr.open_dataset(self._path).rename(rename_map)

        # We use `str` types for ids throughout.
        self._data.coords[_ID] = self._data.coords[_ID].astype(str)

    def get(
        self,
        pv_ids: list[PvId] | PvId,
        start_ts: Timestamp | None = None,
        end_ts: Timestamp | None = None,
    ) -> xr.Dataset:
        end_ts = min_timestamp(self._max_ts, end_ts)
        return self._data.sel(pv_id=pv_ids, ts=slice(start_ts, end_ts))

    def list_pv_ids(self):
        out = list(self._data.coords[_ID].values)

        if len(out) > 0:
            assert isinstance(out[0], PvId)

        return out

    def min_ts(self):
        ts = to_pydatetime(self._data.coords[_TS].min().values)  # type:ignore
        return min_timestamp(ts, self._max_ts)

    def max_ts(self):
        ts = to_pydatetime(self._data.coords[_TS].max().values)  # type:ignore
        return min_timestamp(ts, self._max_ts)

    def without_future(self, ts: Timestamp, *, blackout: int = 0):
        now = ts - datetime.timedelta(minutes=blackout) - datetime.timedelta(seconds=1)
        new_ds = copy.copy(self)
        new_ds._set_max_ts(min_timestamp(self._max_ts, now))
        return new_ds

    def __getstate__(self):
        d = self.__dict__.copy()
        # I'm not sure of the state contained in a `Dataset` object, so I make sure we don't save
        # it.
        del d["_data"]
        return d

    def __setstate__(self, state):
        for key, value in state.items():
            setattr(self, key, value)
        self._open()
