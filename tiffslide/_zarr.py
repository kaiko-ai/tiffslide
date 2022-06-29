"""
provides helpers for handling and compositing arrays and zarr-like groups
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import Iterator
from typing import Mapping

import numpy as np
import zarr

from tiffslide._types import Point3D
from tiffslide._types import SeriesCompositionInfo
from tiffslide._types import Size3D
from tiffslide._types import Slice3D

if TYPE_CHECKING:
    from numpy.typing import DTypeLike
    from numpy.typing import NDArray
    from tifffile import TiffFile

__all__ = [
    "get_zarr_store",
    "get_zarr_depth_and_dtype",
    "get_zarr_selection",
]


# --- zarr storage classes --------------------------------------------


class _CompositedStore(Mapping[str, Any]):
    """prefix a zarr store to allow mounting a zarr array as a group"""

    def __init__(
        self,
        prefixed_stores: Mapping[str, Mapping[str, Any]],
        *,
        zattrs: Mapping[str, Any] | None = None,
    ) -> None:
        grp = zarr.group({})
        if zattrs:
            grp.attrs["tiffslide.series-composition"] = zattrs
        self._base = grp.store
        self._stores = {}
        for prefix, store in prefixed_stores.items():
            assert not prefix.endswith("/")
            self._stores[prefix] = store

    def __len__(self) -> int:
        return sum(map(len, self._stores.values())) + len(self._base)

    def __contains__(self, item: object) -> bool:
        if not isinstance(item, str):
            return False

        prefix, _, key = item.partition("/")
        if key:
            try:
                store = self._stores[prefix]
            except KeyError:
                pass
            else:
                return key in store

        return item in self._base or prefix in self._stores

    def __iter__(self) -> Iterator[str]:
        for prefix, store in self._stores.items():
            yield from (f"{prefix}/{key}" for key in store.keys())
        yield from self._base.keys()

    def __getitem__(self, item: str) -> Any:
        prefix, _, key = item.partition("/")

        if key:
            try:
                store = self._stores[prefix]
            except KeyError:
                pass
            else:
                return store[key]

        return self._base[item]


def get_zarr_store(
    properties: Mapping[str, Any], tf: TiffFile | None
) -> Mapping[str, Any]:
    """return a zarr store

    Parameters
    ----------
    properties:
        the TiffSlide().properties mapping
    tf:
        the corresponding TiffFile instance

    Returns
    -------
    store:
        a zarr store of the tiff
    """
    if tf is None:
        raise NotImplementedError("support in future versions")

    # the tiff might contain multiple series that require composition
    composition: SeriesCompositionInfo | None = properties.get(
        "tiffslide.series-composition"
    )
    if composition:
        prefixed_stores = {}
        for series_idx in composition["located_series"].keys():
            _store = tf.series[series_idx].aszarr()
            # encapsulate store as group if tifffile returns a zarr array
            if ".zarray" in _store:
                _store = _CompositedStore({"0": _store})
            prefixed_stores[str(series_idx)] = _store

        store = _CompositedStore(prefixed_stores, zattrs=composition)

    else:
        series_idx = properties.get("tiffslide.series-index", 0)
        store = tf.series[series_idx].aszarr()

        # encapsulate store as group if tifffile returns a zarr array
        if ".zarray" in store:
            store = _CompositedStore({"0": store})

    return store


def get_zarr_selection(
    grp: zarr.Group,
    level: int,
    selection: Slice3D,
) -> NDArray[np.int_]:
    """retrieve the selection of the zarr Group"""
    composition: SeriesCompositionInfo = grp.attrs.get("tiffslide.series-composition")

    if composition is None:
        # no composition required, simply retrieve the array
        return grp[str(level)][selection]

    else:
        # we need to composite the array
        # todo: getting these overlaps should be done via an RTree...
        overlaps = {}
        dtype = None
        fill_value = None
        shape = composition["level_shapes"][0]
        level_shape = composition["level_shapes"][level]

        for series_idx, offset in composition["located_series"].items():
            arr = grp[f"{series_idx}/{level}"]

            if dtype is None:
                dtype = arr.dtype
            if fill_value is None:
                fill_value = arr.fill_value

            offset = (
                int(offset[0] * level_shape[0] / shape[0]),
                int(offset[1] * level_shape[1] / shape[1]),
                int(offset[2] * level_shape[2] / shape[2]),
            )

            overlap = get_overlap(selection, level_shape, offset, arr.shape)
            if overlap is None:
                continue

            # todo: could take a shortcut here...
            # target, source = overlap
            # if target == item:  # < this check is incorrect [:, :] != [0:10, 0:10]
            #     return arr[source]

            overlaps[series_idx] = overlap

        s0, s1, s2 = selection
        r0 = range(level_shape[0])[s0]
        r1 = range(level_shape[1])[s1]
        r2 = range(level_shape[2])[s2]
        shape = (r0.stop - r0.start, r1.stop - r1.start, r2.stop - r2.start)
        # todo: optimize! In the most common case this should be filled entirely
        out = np.full(shape, fill_value=fill_value, dtype=dtype)

        for series_idx, (target, source) in overlaps.items():
            arr = grp[f"{series_idx}/{level}"]
            out[target] = arr[source]

    return out


# --- helper functions ------------------------------------------------


def get_overlap(
    selection: Slice3D,
    cshape: Size3D,
    offset: Point3D,
    ashape: Size3D,
) -> tuple[Slice3D, Slice3D] | None:
    """return correct slices for overlapping arrays

    CompositedArray
    +------------------------+
    | x------------x         |
    | | Slice o----|---o     |
    | |       |    |   |     |
    | x------------x   |     |
    |         | Array  |     |
    |         o--------o     |
    +------------------------+

    Notes
    -----
    if selection is a slice out of the CompositedArray with shape=cshape,
    this function returns either None, if the slice and the Array share
    no overlap, or two slices (target and source), so that:

    `new_arr[target] = array[source]`

    can be used to assign the overlap correctly to the output array.

    """
    s0, s1, s2 = selection
    d0, d1, d2 = cshape
    o0, o1, o2 = offset
    a0, a1, a2 = ashape

    # dim 0
    x0, x1, _ = s0.indices(d0)
    X0 = o0
    X1 = o0 + a0
    if X1 < x0 or x1 <= X0:
        return None
    # dim 1
    y0, y1, _ = s1.indices(d1)
    Y0 = o1
    Y1 = o1 + a1
    if Y1 < y0 or y1 <= Y0:
        return None
    # dim 2
    z0, z1, _ = s2.indices(d2)
    Z0 = o2
    Z1 = o2 + a2
    if Z1 < z0 or z1 <= Z0:
        return None

    ix0 = max(x0, X0)
    iy0 = max(y0, Y0)
    iz0 = max(z0, Z0)
    ix1 = min(x1, X1)
    iy1 = min(y1, Y1)
    iz1 = min(z1, Z1)

    target = (
        slice(ix0 - x0, ix1 - x0, 1),
        slice(iy0 - y0, iy1 - y0, 1),
        slice(iz0 - z0, iz1 - z0, 1),
    )
    source = (
        slice(ix0 - X0, ix1 - X0, 1),
        slice(iy0 - Y0, iy1 - Y0, 1),
        slice(iz0 - Z0, iz1 - Z0, 1),
    )
    return target, source


def verify_located_arrays(
    shape: Size3D, located_arrays: dict[Point3D, zarr.Array]
) -> tuple[str, Any]:
    """verify located arrays

    ensures that dtypes and fill_values agree and that
    arrays are within the composited boundaries.
    """
    dt_fv = set()
    for loc, arr in located_arrays.items():
        dt_fv.add((arr.dtype, arr.fill_value))

        x0, x1, x2 = loc
        d0, d1, d2 = arr.shape
        out_of_bounds = (
            (x0 + d0 < 0 or x0 >= shape[0])
            or (x1 + d1 < 0 or x1 >= shape[1])
            or (x2 + d2 < 0 or x2 >= shape[2])
        )
        if out_of_bounds:
            raise ValueError(f"array at location {loc!r} is out of bounds")

    if not len(dt_fv) == 1:
        raise ValueError(
            f"arrays don't share the same dtype and fill_value, got: {dt_fv!r}"
        )
    ((dtype, fill_value),) = dt_fv
    return dtype, fill_value


def get_zarr_depth_and_dtype(grp: zarr.Group, axes: str) -> tuple[int, DTypeLike]:
    """return the image depth from the zarr group"""
    if "tiffslide.series-composition" in grp.attrs:
        srs = next(iter(grp.attrs["series-compostion"]["located_series"]))
        key = f"{srs}/0"
    else:
        key = "0"  # -> level

    zarray: zarr.core.Array = grp[key]

    if axes == "YXS":
        depth = zarray.shape[2]
    elif axes == "CYX":
        depth = zarray.shape[0]
    else:
        raise NotImplementedError(f"axes={axes!r}")

    return depth, zarray.dtype
