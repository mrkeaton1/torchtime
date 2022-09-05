"""
=====================
Time series data sets
=====================

* `PhysioNet2012 <#torchtime.data.PhysioNet2012>`_
* `PhysioNet2019 <#torchtime.data.PhysioNet2019>`_
* `PhysioNet2019Binary <#torchtime.data.PhysioNet2019Binary>`_
* `UEA <#torchtime.data.UEA>`_
"""

import csv
import pathlib
from typing import Callable, Dict, List, Union

import numpy as np
import pandas as pd
import torch
import torch.nn.functional as F
from sklearn.model_selection import train_test_split
from sktime.datasets import load_from_tsfile_to_dataframe
from torch import Tensor
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data import Dataset
from tqdm import tqdm

from torchtime.constants import (
    EPS,
    OBJ_EXT,
    PHYSIONET_2012_CATEGORICAL,
    PHYSIONET_2012_DATASETS,
    PHYSIONET_2012_MEANS,
    PHYSIONET_2012_OUTCOMES,
    PHYSIONET_2012_STATIC,
    PHYSIONET_2012_VARS,
    PHYSIONET_2019_CATEGORICAL,
    PHYSIONET_2019_DATASETS,
    PHYSIONET_2019_MEANS,
    PHYSIONET_2019_STATIC,
    TQDM_FORMAT,
    UEA_DOWNLOAD_URL,
)
from torchtime.impute import forward_impute, replace_missing
from torchtime.utils import (
    _cache_data,
    _cache_exists,
    _download_archive,
    _download_to_directory,
    _get_file_list,
    _nanmode,
    _physionet_download,
    _simulate_missing,
    _validate_cache,
)


class _TimeSeriesDataset(Dataset):
    """**Generic time series PyTorch Dataset.**

    .. warning::
        Inherit from this class to define a time series. Overload the ``_get_data()``
        method to return the time series. This must contain a time stamp in the first
        channel followed by the time series channels.

    Args:
        dataset: Name of the cache directory for the data set.
        split: The data split to return, either *train*, *val* (validation) or *test*.
        train_prop: Proportion of data in the training set.
        val_prop: Proportion of data in the validation set (optional, see above).
        missing: The proportion of data to drop at random. If ``missing`` is a single
            value, data are dropped from all channels. To drop data independently across
            each channel, pass a list of the proportion missing for each channel e.g.
            ``[0.5, 0.2, 0.8]``. Default 0 i.e. no missing data simulation.
        impute: Method used to impute missing data, either *none*, *zero*, *mean*,
            *forward* or a custom imputation function (default "none"). See warning
            above.
        static: TODO
        categorical: List with channel indices of categorical variables. Only required
            if imputing data. Default ``[]`` i.e. no categorical variables.
        channel_means: Override the calculated channel mean/mode when imputing data.
            Only used if imputing data. Dictionary with channel indices and values e.g.
            ``{1: 4.5, 3: 7.2}`` (default ``{}`` i.e. no overridden channel mean/modes).
        time: Append time stamp in the first channel (default True).
        mask: Append missing data mask for each channel (default False).
        delta: Append time since previous observation for each channel calculated as in
            `Che et al (2018) <https://doi.org/10.1038/s41598-018-24271-9>`_. Default
            False.
        standardise: Standardise the time series, either *all* (all channels), *data*
            (time series channels only) or *none* (no standardisation) (default "none").
        overwrite_cache: Overwrite saved cache (default False).
        path: Location of the ``.torchtime`` cache directory (default ".").
        seed: Random seed for reproducibility (optional).

    Attributes:
        X (Tensor): A tensor of default shape (*n*, *s*, *c* + 1) where *n* = number of
            sequences, *s* = (longest) sequence length and *c* = number of
            channels. By default, a time stamp is appended as the first channel. If
            ``time`` is False, the time stamp is omitted and the tensor has shape
            (*n*, *s*, *c*).

            A missing data mask and/or time delta channels can be appended with the
            ``mask`` and ``delta`` arguments. These each have the same number of
            channels as the data set. For example, if ``time``, ``mask`` and
            ``delta`` are all True, ``X`` has shape (*n*, *s*, 3 * *c* + 1) and the
            channels are in the order: time stamp, time series, missing data mask, time
            deltas.

            Where sequences are of unequal lengths they are padded with ``NaNs`` to
            the length of the longest sequence in the data.
        X_static (Tensor): TODO
        y (Tensor): One-hot encoded label data. A tensor of shape (*n*, *l*) where *l*
            is the number of classes.
        length (Tensor): Length of each sequence prior to padding. A tensor of shape
            (*n*).
        n_channels: Number of time series channels in data.

    Returns:
        A PyTorch Dataset object which can be passed to a DataLoader.
    """

    def __init__(
        self,
        dataset: str,
        split: str,
        train_prop: float,
        val_prop: float = None,
        missing: Union[float, List[float]] = 0.0,
        impute: Union[str, Callable[[Tensor], Tensor]] = "none",
        static: List[int] = [],
        categorical: List[int] = [],
        channel_means: Dict[int, float] = {},
        time: bool = True,
        mask: bool = False,
        delta: bool = False,
        standardise: str = "none",
        overwrite_cache: bool = False,
        path: str = ".",
        seed: int = None,
    ) -> None:
        self.dataset = dataset
        self.split = split
        self.train_prop = train_prop
        self.val_prop = val_prop
        self.test_prop = 0
        self.missing = missing
        self.impute = impute
        self.static = static
        self.categorical = categorical
        self.channel_means = channel_means
        self.time = time
        self.mask = mask
        self.delta = delta
        self.standardise = standardise
        self.overwrite_cache = overwrite_cache
        self.path = pathlib.Path() / path / ".torchtime" / self.dataset
        self.seed = seed

        # Constants
        self.IMPUTE_FUNCTIONS = {
            "zero": self._zero_imputation,
            "mean": self._mean_imputation,
            "forward": self._forward_imputation,
        }

        # Validate arguments and set data splits
        self._validate_arguments()

        # 1. Get data from cache or, if no cache, call _get_data() and cache results
        if _cache_exists(self.path) and not self.overwrite_cache:
            if _validate_cache(self.path):
                X_all = torch.load(self.path / ("X" + OBJ_EXT))
                y_all = torch.load(self.path / ("y" + OBJ_EXT))
                length_all = torch.load(self.path / ("length" + OBJ_EXT))
            else:
                raise Exception(
                    "Cache is corrupted! Use 'overwrite_cache' = True to rebuild."
                )
        else:
            print("Processing raw data...")
            X_all, y_all, length_all = self._get_data()
            X_all = X_all.float()  # float32 precision
            y_all = y_all.float()  # float32 precision
            length_all = length_all.long()  # int64 precision
            _cache_data(self.path, X_all, y_all, length_all)
        print("Preparing data...")

        # Attributes for time, data, mask and time delta indices
        self.time_idx = [0]
        self.data_idx = list(np.arange(1, X_all.size(-1)))
        self.mask_idx = []
        self.delta_idx = []

        # 2. Split out static channels
        self.n_channels_static = 0
        X_all_static = None
        if self.static != []:
            assert np.all(
                [idx in self.data_idx for idx in self.static]
            ), "channels in argument 'static' are not included in the data"
            X_all_static = X_all[:, 0, self.static]
            self.data_idx = list(np.setdiff1d(self.data_idx, self.static))
            X_all = X_all[:, :, self.time_idx + self.data_idx]
            self.n_channels_static = len(self.static)
        self.n_channels = X_all.size(-1) - 1  # do not count time channel

        # 3. Simulate missing data
        if (type(self.missing) is list and sum(self.missing) > EPS) or (
            type(self.missing) is float and self.missing > EPS
        ):
            # TODO: do not drop values from time channel
            _simulate_missing(X_all, self.missing, seed=self.seed)

        # 4. Add time stamp/mask/time delta channels
        X_all = self._add_channels(X_all)

        # 5. Form train/validation/test splits
        stratify = torch.nansum(y_all, dim=1) > 0
        (
            self.X_train,
            self.y_train,
            self.length_train,
            self.X_static_train,
            self.X_val,
            self.y_val,
            self.length_val,
            self.X_static_val,
            self.X_test,
            self.y_test,
            self.length_test,
            self.X_static_test,
        ) = self._split_data(
            X_all,
            y_all,
            length_all,
            X_all_static,
            stratify,
        )

        # 6. Impute missing data
        if self.impute != "none":
            self._impute_data()
            if self.X_static_train is not None:
                self._impute_static()

        # 7. Standardise data
        if self.standardise != "none":
            self._standardise()
            if self.X_static_train is not None:
                self._standardise_static()

        # 8. Return data split
        if split == "test":
            self.X = self.X_test
            self.y = self.y_test
            self.length = self.length_test
            self.X_static = self.X_static_test
        elif split == "train":
            self.X = self.X_train
            self.y = self.y_train
            self.length = self.length_train
            self.X_static = self.X_static_train
        else:
            self.X = self.X_val
            self.y = self.y_val
            self.length = self.length_val
            self.X_static = self.X_static_val
        if self.test_prop <= EPS:
            del self.X_test, self.y_test, self.length_test, self.X_static_test
        if self.X_static is None:
            del self.X_static, self.X_static_train, self.X_static_val

    def __str__(self):
        """Print data set details."""
        return """TimeSeriesDataset: {}
 - cache location = {}
 - data split = {:.0f}/{:.0f}/{:.0f}% (training/validation/test)
 - time/mask/delta channels = {}/{}/{}
 - random seed = {}
 - static channels = {}
 - categorical channels = {}
 - standardise = {}
 - X, y, length attributes return the {} split""".format(
            self.dataset,
            self.path.resolve(),
            100 * self.train_prop,
            100 * self.val_prop,
            100 * self.test_prop,
            self.time,
            self.mask,
            self.delta,
            self.seed,
            self.static,
            self.categorical,
            self.standardise,
            self.split,
        )

    def _validate_arguments(self):
        """Validate arguments and set imputation function/data splits."""
        # Validate impute arguments
        impute_options = ["none"] + list(self.IMPUTE_FUNCTIONS.keys())
        impute_error = "argument 'impute' must be a string in {} or a function".format(
            impute_options
        )
        if self.impute != "none":
            categorical_error = "argument 'categorical' must be a list of integers"
            means_error = "argument 'channel_means' must be a dictionary"
            assert type(self.categorical) is list, categorical_error
            assert np.all(
                [type(idx) is int for idx in self.categorical]
            ), categorical_error
            assert type(self.channel_means) is dict, means_error
            assert np.all(
                [type(idx) is int for idx in self.channel_means.keys()]
            ), means_error
            assert np.all(
                [type(mean) is float for mean in self.channel_means.values()]
            ), means_error
        # Set impute function
        if type(self.impute) is str:
            assert self.impute in impute_options, impute_error
            self.imputer = self.IMPUTE_FUNCTIONS.get(self.impute)
        elif callable(self.impute):
            self.imputer = self.impute
        else:
            raise Exception(impute_error)
        # Validate standardise argument
        standardise_options = ["none", "all", "data"]
        standardise_error = "argument 'standardise' must be a string in {}".format(
            standardise_options
        )
        assert type(self.standardise) is str, standardise_error
        assert self.standardise in standardise_options, standardise_error
        # Validate/set data splits
        assert (
            self.train_prop > EPS and self.train_prop < 1
        ), "argument 'train_prop' must be in range (0, 1)"
        if self.val_prop is None:
            self.val_prop = 1 - self.train_prop
        else:
            assert (
                self.val_prop > EPS and self.val_prop < 1 - self.train_prop
            ), "argument 'val_prop' must be in range (0, {})".format(
                1 - self.train_prop
            )
            self.test_prop = 1 - self.train_prop - self.val_prop
            self.val_prop = self.val_prop / (1 - self.test_prop)
        splits = ["train", "val"]
        if self.test_prop > EPS:
            splits.append("test")
        assert self.split in splits, "argument 'split' must be one of {}".format(splits)
        # Validate 'static' argument
        static_error = "argument 'static' must be a list of integers"
        assert type(self.static) is list, static_error
        assert np.all([type(idx) is int for idx in self.static]), static_error

    @staticmethod
    def _zero_imputation(X, y, fill, select):
        """Zero imputation. Replace missing values with zeros."""
        X_imputed = replace_missing(X, fill=torch.zeros(select.size(-1)), select=select)
        return X_imputed, y

    @staticmethod
    def _mean_imputation(X, y, fill, select):
        """Mean imputation. Replace missing values in ``X`` from ``fill``."""
        X_imputed = replace_missing(X, fill=fill, select=select)
        return X_imputed, y

    @staticmethod
    def _forward_imputation(X, y, fill, select):
        """Forward imputation. Replace missing values with previous observation. Replace
        any initial missing values in ``X`` from ``fill``."""
        X_imputed = forward_impute(X, fill=fill, select=select)
        return X_imputed, y

    def _get_data(self):
        """Overload this function to return ``X``, ``y`` and ``length`` tensors."""
        raise NotImplementedError

    def _add_channels(self, X_all):
        """Add/remove time, mask and time delta channels and set/update indices
        attributes."""
        if self.mask:
            mask = self._missing_mask(X_all)
            self.mask_idx = list(
                np.arange(X_all.size(-1), X_all.size(-1) + mask.size(-1))
            )
            X_all = torch.cat([X_all, mask], dim=2)
        if self.delta:
            delta = self._time_delta(X_all)
            self.delta_idx = list(
                np.arange(X_all.size(-1), X_all.size(-1) + delta.size(-1))
            )
            X_all = torch.cat([X_all, delta], dim=2)
        if not self.time:
            X_all = X_all[:, :, 1:]
            self.time_idx = []
            self.data_idx = [idx - 1 for idx in self.data_idx]
            self.mask_idx = [idx - 1 for idx in self.mask_idx]
            self.delta_idx = [idx - 1 for idx in self.delta_idx]
        return X_all

    def _missing_mask(self, X):
        """Calculate missing data mask."""
        mask = torch.logical_not(torch.isnan(X[:, :, 1:]))
        return mask

    def _time_delta(self, X):
        """Calculate time delta calculated as in Che et al, 2018, see
        https://www.nature.com/articles/s41598-018-24271-9."""
        # Add mask channels
        if not self.mask:
            X = torch.cat([X, self._missing_mask(X)], dim=2)
        # Time of each observation by channel
        X = X.transpose(1, 2)  # shape (n, c, s)
        time_stamp = X[:, 0].unsqueeze(1).repeat(1, self.n_channels, 1)
        # Time delta/mask are 0/1 at time 0 by definition
        time_delta = time_stamp.clone()
        time_delta[:, :, 0] = 0
        time_mask = X[:, -self.n_channels :].clone()
        time_mask[:, :, 0] = 1
        # Time of previous observation if data missing
        time_delta = time_delta.gather(-1, torch.cummax(time_mask, -1)[1])
        # Calculate time delta
        time_delta = torch.cat(
            (
                time_delta[:, :, 0].unsqueeze(2),  # t = 0
                time_stamp[:, :, 1:] - time_delta[:, :, :-1],
            ),
            dim=2,
        )
        return time_delta.transpose(1, 2)

    def _split_data(self, X, y, length, X_static, stratify):
        """Split data into training, validation and (optional) test sets using
        stratified sampling."""
        random_state = np.random.RandomState(self.seed)
        X_test, y_test, length_test, X_static_test = (
            None,
            None,
            None,
            None,
        )
        if X_static is None:
            if self.test_prop > EPS:
                # Test split
                (
                    X_test,
                    X_nontest,
                    y_test,
                    y_nontest,
                    length_test,
                    length_nontest,
                    _,
                    stratify_nontest,
                ) = train_test_split(
                    X,
                    y,
                    length,
                    stratify,
                    train_size=self.test_prop,
                    random_state=random_state,
                    shuffle=True,
                    stratify=stratify,
                )
                X = X_nontest
                y = y_nontest
                length = length_nontest
                stratify = stratify_nontest
            # Validation/train split
            (
                X_val,
                X_train,
                y_val,
                y_train,
                length_val,
                length_train,
            ) = train_test_split(
                X,
                y,
                length,
                train_size=self.val_prop,
                random_state=random_state,
                shuffle=True,
                stratify=stratify,
            )
            X_static_train, X_static_val, X_static_test = (
                None,
                None,
                None,
            )
        else:
            if self.test_prop > EPS:
                # Test split
                (
                    X_test,
                    X_nontest,
                    y_test,
                    y_nontest,
                    length_test,
                    length_nontest,
                    X_static_test,
                    X_static_nontest,
                    _,
                    stratify_nontest,
                ) = train_test_split(
                    X,
                    y,
                    length,
                    X_static,
                    stratify,
                    train_size=self.test_prop,
                    random_state=random_state,
                    shuffle=True,
                    stratify=stratify,
                )
                X = X_nontest
                y = y_nontest
                length = length_nontest
                X_static = X_static_nontest
                stratify = stratify_nontest
            # Validation/train split
            (
                X_val,
                X_train,
                y_val,
                y_train,
                length_val,
                length_train,
                X_static_val,
                X_static_train,
            ) = train_test_split(
                X,
                y,
                length,
                X_static,
                train_size=self.val_prop,
                random_state=random_state,
                shuffle=True,
                stratify=stratify,
            )
        return (
            X_train,
            y_train,
            length_train,
            X_static_train,
            X_val,
            y_val,
            length_val,
            X_static_val,
            X_test,
            y_test,
            length_test,
            X_static_test,
        )

    def _impute_data(self):
        """Impute missing data (time series channels)."""
        data_fill = torch.nanmean(self.X_train[:, :, self.data_idx], dim=(0, 1))
        data_idx = torch.tensor(self.data_idx)
        # Impute with channel mode if categorical variable
        if self.categorical != []:
            assert np.all(
                [idx in self.data_idx or idx in self.static for idx in self.categorical]
            ), "channels in argument 'static' are not included in the data"
            for idx in self.categorical:
                if idx <= self.n_channels:
                    data_fill[idx - 1] = _nanmode(
                        self.X_train[:, :, idx - int(not self.time)]
                    )
        # Override mean/mode if specified
        if self.channel_means != {}:
            assert np.all(
                [
                    idx in self.data_idx or idx in self.static
                    for idx in self.channel_means.keys()
                ]
            ), "channels in argument 'channel_means' are not included in the data"
            for idx, new_mean in self.channel_means.items():
                if idx <= self.n_channels:
                    data_fill[idx - 1] = new_mean
        # Impute time series channels
        self.X_train, self.y_train = self.imputer(
            self.X_train, self.y_train, fill=data_fill, select=data_idx
        )
        self.X_val, self.y_val = self.imputer(
            self.X_val, self.y_val, fill=data_fill, select=data_idx
        )
        if self.test_prop > EPS:
            self.X_test, self.y_test = self.imputer(
                self.X_test, self.y_test, fill=data_fill, select=data_idx
            )

    def _impute_static(self):
        """Impute missing data (static channels)."""
        static_fill = torch.nanmean(self.X_static_train, dim=0)
        # Impute with channel mode if categorical variable
        if self.categorical != []:
            for idx in [
                self.static.index(i) for i in self.categorical if i in self.static
            ]:
                static_fill[idx] = _nanmode(self.X_static_train[:, idx])
        # Override mean/mode if specified
        if self.channel_means != {}:
            for idx, new_mean in self.channel_means.items():
                if idx in self.static:
                    static_fill[self.static.index(idx)] = new_mean
        # Impute static channels
        self.X_static_train = replace_missing(self.X_static_train, fill=static_fill)
        self.X_static_val = replace_missing(self.X_static_val, fill=static_fill)
        if self.test_prop > EPS:
            self.X_static_test = replace_missing(self.X_static_test, fill=static_fill)

    def _standardise(self):
        """Standardise data (continuous channels only)."""
        continuous_idx = self.data_idx
        if self.standardise == "all":
            continuous_idx = self.time_idx + self.data_idx + self.delta_idx
        categorical_idx = [idx - int(not self.time) for idx in self.categorical]
        standardise_idx = torch.tensor(np.setdiff1d(continuous_idx, categorical_idx))
        # Training data channel means/standard deviations
        train_means = torch.nanmean(
            self.X_train[:, :, standardise_idx], dim=(0, 1), keepdim=True
        )
        train_stds = torch.full(
            (1, 1, standardise_idx.size(-1)), fill_value=float("nan")
        )
        for c, Xc in enumerate(self.X_train[:, :, standardise_idx].unbind(dim=-1)):
            train_stds[:, :, c] = torch.std(Xc[~torch.isnan(Xc)])
        # Standardise time series data
        self.X_train[:, :, standardise_idx] = (
            self.X_train[:, :, standardise_idx] - train_means
        ) / (train_stds + EPS)
        self.X_val[:, :, standardise_idx] = (
            self.X_val[:, :, standardise_idx] - train_means
        ) / (train_stds + EPS)
        if self.test_prop > EPS:
            self.X_test[:, :, standardise_idx] = (
                self.X_test[:, :, standardise_idx] - train_means
            ) / (train_stds + EPS)

    def _standardise_static(self):
        """Standardise static channels."""
        categorical_idx = [idx - int(not self.time) for idx in self.categorical]
        standardise_idx_static = [
            self.static.index(i)
            for i in np.setdiff1d(self.static, categorical_idx)
            if i in self.static
        ]
        # Training data channel means/standard deviations
        train_means_static = torch.nanmean(
            self.X_static_train[:, standardise_idx_static], dim=0, keepdim=True
        )
        train_stds_static = torch.full(
            (1, self.X_static_train[:, standardise_idx_static].size(-1)),
            fill_value=float("nan"),
        )
        for c, Xc in enumerate(
            self.X_static_train[:, standardise_idx_static].unbind(dim=-1)
        ):
            train_stds_static[:, c] = torch.std(Xc[~torch.isnan(Xc)])
        # Standardise static channels
        self.X_static_train[:, standardise_idx_static] = (
            self.X_static_train[:, standardise_idx_static] - train_means_static
        ) / (train_stds_static + EPS)
        self.X_static_val[:, standardise_idx_static] = (
            self.X_static_val[:, standardise_idx_static] - train_means_static
        ) / (train_stds_static + EPS)
        if self.test_prop > EPS:
            self.X_static_test[:, standardise_idx_static] = (
                self.X_static_test[:, standardise_idx_static] - train_means_static
            ) / (train_stds_static + EPS)

    def __len__(self):
        return self.X.size(0)

    def __getitem__(self, idx):
        if self.static == []:
            return {
                "X": self.X[idx],
                "y": self.y[idx],
                "length": self.length[idx],
            }
        else:
            return {
                "X": self.X[idx],
                "X_static": self.X_static[idx],
                "y": self.y[idx],
                "length": self.length[idx],
            }


class PhysioNet2012(_TimeSeriesDataset):
    r"""**Returns the PhysioNet Challenge 2012 data as a PyTorch Dataset.** See the
    PhysioNet `website <https://physionet.org/content/challenge-2012/1.0.0/>`_ for a
    description of the data set.

    The proportion of data in the training, validation and (optional) test data sets are
    specified by the ``train_prop`` and ``val_prop`` arguments. For a
    training/validation split specify ``train_prop`` only. For a
    training/validation/test split specify both ``train_prop`` and ``val_prop``.

    For example ``train_prop=0.8`` generates a 80/20% train/validation split, but
    ``train_prop=0.8``, ``val_prop=0.1`` generates a 80/10/10% train/validation/test
    split. Splits are formed using stratified sampling.

    When passed to a PyTorch DataLoader, batches are a named dictionary with ``X``,
    ``y`` and ``length`` data. The ``split`` argument determines whether training,
    validation or test data are returned.

    Missing data can imputed using the ``impute`` argument. See the `missing data
    tutorial <https://philipdarke.com/torchtime/tutorials/missing_data.html>`_ for more
    information.

    Data channels are in the following order:

    :0. Time: Hours since ICU admission. Derived from the PhysioNet time stamp.
    :1. Albumin: Albumin (g/dL)
    :2. ALP: Alkaline phosphatase (IU/L)
    :3. ALT: Alanine transaminase (IU/L)
    :4. AST: Aspartate transaminase (IU/L)
    :5. Bilirubin: Bilirubin (mg/dL)
    :6. BUN: Blood urea nitrogen (mg/dL)
    :7. Cholesterol: Cholesterol (mg/dL)
    :8. Creatinine: Serum creatinine (mg/dL)
    :9. DiasABP: Invasive diastolic arterial blood pressure (mmHg)
    :10. FiO2: Fractional inspired O\ :sub:`2` (0-1)
    :11. GCS: Glasgow Coma Score (3-15)
    :12. Glucose: Serum glucose (mg/dL)
    :13. HCO3: Serum bicarbonate (mmol/L)
    :14. HCT: Hematocrit (%)
    :15. HR: Heart rate (bpm)
    :16. K: Serum potassium (mEq/L)
    :17. Lactate: Lactate (mmol/L)
    :18. Mg: Serum magnesium (mmol/L)
    :19. MAP: Invasive mean arterial blood pressure (mmHg)
    :20. MechVent: Mechanical ventilation respiration (0:false, or 1:true)
    :21. Na: Serum sodium (mEq/L)
    :22. NIDiasABP: Non-invasive diastolic arterial blood pressure (mmHg)
    :23. NIMAP: Non-invasive mean arterial blood pressure (mmHg)
    :24. NISysABP: Non-invasive systolic arterial blood pressure (mmHg)
    :25. PaCO2: Partial pressure of arterial CO\ :sub:`2` (mmHg)]
    :26. PaO2: Partial pressure of arterial O\ :sub:`2` (mmHg)
    :27. pH: Arterial pH (0-14)
    :28. Platelets: Platelets (cells/nL)
    :29. RespRate: Respiration rate (bpm)
    :30. SaO2: O\ :sub:`2` saturation in hemoglobin (%)
    :31. SysABP: Invasive systolic arterial blood pressure (mmHg)
    :32. Temp: Temperature (°C)
    :33. TroponinI: Troponin-I (μg/L). Note this is labelled *TropI* in the PhysioNet
        data dictionary.
    :34. TroponinT: Troponin-T (μg/L). Note this is labelled *TropT* in the PhysioNet
        data dictionary.
    :35. Urine: Urine output (mL)
    :36. WBC: White blood cell count (cells/nL)
    :37. Weight: Weight (kg)
    :38. Age: Age (years) at ICU admission
    :39. Gender: Gender (0: female, or 1: male)
    :40. Height: Height (cm) at ICU admission
    :41. ICUType1: Type of ICU unit (1: Coronary Care Unit)
    :42. ICUType2: Type of ICU unit (2: Cardiac Surgery Recovery Unit)
    :43. ICUType3: Type of ICU unit (3: Medical ICU)
    :44. ICUType4: Type of ICU unit (4: Surgical ICU)

    .. note::
        Channels 38 to 44 do not vary with time. These channels are returned by the
        ``X_static`` attribute if the ``static`` argument is True.

        Variables 11 (GCS) and 27 (pH) are assumed to be ordinal and are imputed using
        the same method as a continuous variable.

        Variable 20 (MechVent) has value ``Nan`` (the majority of values) or 1. It is
        assumed that value 1 indicates that mechanical ventilation has been used and
        ``NaN`` indicates either missing data or no mechanical ventilation. Accordingly,
        the channel mode is assumed to be zero.

        Variables 41-44 are the one-hot encoded value of ICUType. Missing ICUType values
        are assumed to be 3 i.e. Medical ICU.

    Args:
        split: The data split to return, either *train*, *val* (validation) or *test*.
        train_prop: Proportion of data in the training set.
        val_prop: Proportion of data in the validation set (optional, see above).
        impute: Method used to impute missing data, either *none*, *zero*, *mean*,
            *forward* or a custom imputation function (default "none").
        static: Split out static channels as above (default False).
        time: Append time stamp in the first channel (default True).
        mask: Append missing data mask for each channel (default False).
        delta: Append time since previous observation for each channel calculated as in
            `Che et al (2018) <https://doi.org/10.1038/s41598-018-24271-9>`_ (default
            False).
        standardise: Standardise the time series, either *all* (all channels), *data*
            (time series channels only) or *none* (no standardisation) (default "none").
        overwrite_cache: Overwrite saved cache (default False).
        path: Location of the ``.torchtime`` cache directory (default ".").
        seed: Random seed for reproducibility (optional).

    Attributes:
        X (Tensor): A tensor of default shape (*n*, *s*, *c* + 1) where *n* = number of
            sequences, *s* = (longest) sequence length and *c* = number of channels
            in the PhysioNet data (*excluding* the time since admission). See
            above for the order of the PhysioNet channels. By default, the time since
            admission is appended as the first channel. If ``time`` is False, it is
            omitted and the tensor has shape (*n*, *s*, *c*).

            A missing data mask and/or time delta channels can be appended with the
            ``mask`` and ``delta`` arguments. These each have the same number of
            channels as the PhysioNet data. For example, if ``time``, ``mask`` and
            ``delta`` are all True, ``X`` has shape (*n*, *s*, 3 * *c* + 1 = 127) and
            the channels are in the order: time stamp, time series, missing data mask,
            time deltas.

            Note that PhysioNet sequences are of unequal length and are therefore
            padded with ``NaNs`` to the length of the longest sequence in the data.
        X_static: TODO
        y (Tensor): In-hospital survival (the ``In-hospital_death`` variable) for each
            patient. *y* = 1 indicates an in-hospital death. A tensor of shape (*n*, 1).
        length (Tensor): Length of each sequence prior to padding. A tensor of shape
            (*n*).
        n_channels: Number of time series channels in data.

    .. note::
        ``X``, ``y`` and ``length`` are available for the training, validation and test
        splits by appending ``_train``, ``_val`` and ``_test`` respectively. For
        example, ``y_val`` returns the labels for the validation data set. These
        attributes are available regardless of the ``split`` argument.

    Returns:
        A PyTorch Dataset object which can be passed to a DataLoader.
    """

    def __init__(
        self,
        split: str,
        train_prop: float,
        val_prop: float = None,
        impute: Union[str, Callable[[Tensor], Tensor]] = "none",
        static: bool = False,
        time: bool = True,
        mask: bool = False,
        delta: bool = False,
        standardise: str = "none",
        overwrite_cache: bool = False,
        path: str = ".",
        seed: int = None,
    ) -> None:
        self.dataset_path = pathlib.Path() / path / ".torchtime" / "physionet_2012"
        if static:
            static_idx = PHYSIONET_2012_STATIC
        else:
            static_idx = []
        super(PhysioNet2012, self).__init__(
            dataset="physionet_2012",
            split=split,
            train_prop=train_prop,
            val_prop=val_prop,
            impute=impute,
            static=static_idx,
            categorical=PHYSIONET_2012_CATEGORICAL,
            channel_means=PHYSIONET_2012_MEANS,
            time=time,
            mask=mask,
            delta=delta,
            standardise=standardise,
            overwrite_cache=overwrite_cache,
            path=path,
            seed=seed,
        )

    @staticmethod
    def _get_lengths(data_files):
        """Get length of each time series."""
        lengths = []
        for files in data_files:
            for file_j in tqdm(
                files,
                total=len(files),
                bar_format=TQDM_FORMAT,
            ):
                with open(file_j) as file:
                    reader = csv.reader(file, delimiter=",")
                    lengths_j = []
                    for k, row in enumerate(reader):
                        if k > 0 and row[1] != "":  # ignore head and rows without data
                            lengths_j.append(row[0])
                    lengths_j = set(lengths_j)
                    lengths.append(len(lengths_j))
        return lengths

    @staticmethod
    def _process_files(files, max_length, channels):
        """Process ``.txt`` files."""
        X = np.full((len(files), max_length, len(channels)), float("nan"))
        template_dataframe = pd.DataFrame(columns=channels)
        for i, file_i in tqdm(
            enumerate(files),
            total=len(files),
            bar_format=TQDM_FORMAT,
        ):
            with open(file_i) as file:
                Xi = pd.read_csv(file)
                Xi = Xi.pivot_table(index="Time", columns="Parameter", values="Value")
                Xi["Hours"] = [float(t[:2]) + float(t[3:]) / 60.0 for t in Xi.index]
                Xi = pd.concat([template_dataframe, Xi])
                Xi = Xi.apply(pd.to_numeric, downcast="float")
                # Add static variables
                Xi["Age"] = Xi.loc["00:00", "Age"]
                Xi["Gender"] = Xi.loc["00:00", "Gender"]
                Xi["Height"] = Xi.loc["00:00", "Height"]
                # One-hot encode ICUType
                icu_classes = 4
                icu_onehot = np.eye(icu_classes)[int(Xi.loc["00:00", "ICUType"]) - 1]
                for j in range(icu_classes):
                    Xi["ICUType" + str(j + 1)] = icu_onehot[j]
                X[i, : Xi.shape[0], :] = Xi[channels]
        return torch.tensor(X)

    @staticmethod
    def _get_labels(outcome_files, data_files):
        """Process outcome files."""
        y = []
        for i, file_i in enumerate(outcome_files):
            ids = data_files[i]
            with open(file_i) as file:
                y_i = pd.read_csv(
                    file, index_col=0, usecols=["RecordID", "In-hospital_death"]
                )
                ids_i = [int(id.stem) for id in ids]
                y_i = torch.tensor(y_i.loc[ids_i].values)
                y.append(y_i)
        return y

    def _get_data(self):
        """Download data and form ``X``, ``y``, ``length`` tensors."""
        outcome_path = self.dataset_path / "outcomes"
        all_X = [None for _ in PHYSIONET_2012_DATASETS]
        # Download and extract data
        _physionet_download(
            PHYSIONET_2012_DATASETS, self.dataset_path, self.overwrite_cache
        )
        [
            _download_to_directory(url, outcome_path, self.overwrite_cache)
            for url in PHYSIONET_2012_OUTCOMES
        ]
        # Prepare time series data
        data_directories = [
            self.dataset_path / directory for directory in PHYSIONET_2012_DATASETS
        ]
        data_files = _get_file_list(data_directories)
        length = self._get_lengths(data_files)
        for i, files in enumerate(data_files):
            all_X[i] = self._process_files(files, max(length), PHYSIONET_2012_VARS)
        # Prepare labels
        outcome_files = _get_file_list(outcome_path)
        all_y = self._get_labels(outcome_files, data_files)
        # Form tensors
        X = torch.cat(all_X)
        X[X == -1] = float("nan")  # replace -1 missing data indicator with NaNs
        y = torch.cat(all_y)
        length = torch.tensor(length)
        return X, y, length


class PhysioNet2019(_TimeSeriesDataset):
    """**Returns the PhysioNet Challenge 2019 data as a PyTorch Dataset.** See the
    PhysioNet `website <https://physionet.org/content/challenge-2019/1.0.0/>`_ for a
    description of the data set.

    The proportion of data in the training, validation and (optional) test data sets are
    specified by the ``train_prop`` and ``val_prop`` arguments. For a
    training/validation split specify ``train_prop`` only. For a
    training/validation/test split specify both ``train_prop`` and ``val_prop``.

    For example ``train_prop=0.8`` generates a 80/20% train/validation split, but
    ``train_prop=0.8``, ``val_prop=0.1`` generates a 80/10/10% train/validation/test
    split. Splits are formed using stratified sampling.

    When passed to a PyTorch DataLoader, batches are a named dictionary with ``X``,
    ``y`` and ``length`` data. The ``split`` argument determines whether training,
    validation or test data are returned.

    Missing data can imputed using the ``impute`` argument. See the `missing data
    tutorial <https://philipdarke.com/torchtime/tutorials/missing_data.html>`_ for more
    information.

    Args:
        split: The data split to return, either *train*, *val* (validation) or *test*.
        train_prop: Proportion of data in the training set.
        val_prop: Proportion of data in the validation set (optional, see above).
        impute: Method used to impute missing data, either *none*, *zero*, *mean*,
            *forward* or a custom imputation function (default "none").
        static: Split out static channels as above (default False).
        time: Append time stamp in the first channel (default True).
        mask: Append missing data mask for each channel (default False).
        delta: Append time since previous observation for each channel calculated as in
            `Che et al (2018) <https://doi.org/10.1038/s41598-018-24271-9>`_. Default
            False.
        standardise: Standardise the time series, either *all* (all channels), *data*
            (time series channels only) or *none* (no standardisation) (default "none").
        overwrite_cache: Overwrite saved cache (default False).
        path: Location of the ``.torchtime`` cache directory (default ".").
        seed: Random seed for reproducibility (optional).

    Attributes:
        X (Tensor): A tensor of default shape (*n*, *s*, *c* + 1) where *n* = number of
            sequences, *s* = (longest) sequence length and *c* = number of channels
            in the PhysioNet data (*including* the ``ICULOS`` time stamp). The channels
            are ordered as set out on the PhysioNet `website
            <https://physionet.org/content/challenge-2019/1.0.0/>`_. By default, a
            time stamp is appended as the first channel. If ``time`` is False, the
            time stamp is omitted and the tensor has shape (*n*, *s*, *c*).

            A missing data mask and/or time delta channels can be appended with the
            ``mask`` and ``delta`` arguments. These each have the same number of
            channels as the Physionet data. For example, if ``time``, ``mask`` and
            ``delta`` are all True, ``X`` has shape (*n*, *s*, 3 * *c* + 1 = 121) and
            the channels are in the order: time stamp, time series, missing data mask,
            time deltas.

            Note that PhysioNet sequences are of unequal length and are therefore
            padded with ``NaNs`` to the length of the longest sequence in the data.
        y (Tensor): ``SepsisLabel`` at each time point. A tensor of shape (*n*, *s*, 1).
        length (Tensor): Length of each sequence prior to padding. A tensor of shape
            (*n*).
        n_channels: Number of time series channels in data.

    .. note::
        ``X``, ``y`` and ``length`` are available for the training, validation and test
        splits by appending ``_train``, ``_val`` and ``_test`` respectively. For
        example, ``y_val`` returns the labels for the validation data set. These
        attributes are available regardless of the ``split`` argument.

    Returns:
        A PyTorch Dataset object which can be passed to a DataLoader.
    """

    def __init__(
        self,
        split: str,
        train_prop: float,
        val_prop: float = None,
        impute: Union[str, Callable[[Tensor], Tensor]] = "none",
        static: bool = False,
        time: bool = True,
        mask: bool = False,
        delta: bool = False,
        standardise: str = "none",
        overwrite_cache: bool = False,
        path: str = ".",
        seed: int = None,
    ) -> None:
        if static:
            static_idx = PHYSIONET_2019_STATIC
        else:
            static_idx = []
        super(PhysioNet2019, self).__init__(
            dataset="physionet_2019",
            split=split,
            train_prop=train_prop,
            val_prop=val_prop,
            impute=impute,
            static=static_idx,
            categorical=PHYSIONET_2019_CATEGORICAL,
            channel_means=PHYSIONET_2019_MEANS,
            time=time,
            mask=mask,
            delta=delta,
            standardise=standardise,
            overwrite_cache=overwrite_cache,
            path=path,
            seed=seed,
        )

    @staticmethod
    def _get_lengths_channels(data_files, max_time=None):
        """Get length of each time series and number of channels. Time series can be
        truncated at a specific hour with the ``max_time`` argument."""
        lengths = []  # sequence lengths
        channels = []  # number of channels
        for files in data_files:
            for file_j in tqdm(files, total=len(files), bar_format=TQDM_FORMAT):
                with open(file_j) as file:
                    reader = csv.reader(file, delimiter="|")
                    lengths_j = []
                    for k, Xijk in enumerate(reader):
                        channels.append(len(Xijk))
                        if k > 0:  # ignore header
                            if max_time:
                                if int(Xijk[39]) <= max_time:
                                    lengths_j.append(1)
                            else:
                                lengths_j.append(1)
                    lengths.append(sum(lengths_j))
        channels = list(set(channels))
        assert len(channels) == 1, "corrupt file, delete data and re-run"
        return lengths, channels[0]

    @staticmethod
    def _process_files(files, max_length, channels):
        """Process ``.psv`` files."""
        X = np.full((len(files), max_length, channels - 1), float("nan"))
        y = np.full((len(files), max_length, 1), float("nan"))
        for i, file_i in tqdm(
            enumerate(files),
            total=len(files),
            bar_format=TQDM_FORMAT,
        ):
            with open(file_i) as file:
                reader = csv.reader(file, delimiter="|")
                for j, Xij in enumerate(reader):
                    if j > 0:  # ignore header
                        X[i, j - 1] = Xij[:-1]
                        y[i, j - 1, 0] = Xij[-1]
        return torch.tensor(X), torch.tensor(y)

    def _get_data(self):
        """Download data and form ``X``, ``y``, ``length`` tensors."""
        # Download and extract data
        _physionet_download(PHYSIONET_2019_DATASETS, self.path, self.overwrite_cache)
        # Prepare data
        data_directories = [self.path / dataset for dataset in PHYSIONET_2019_DATASETS]
        data_files = _get_file_list(data_directories)
        length, channels = self._get_lengths_channels(data_files)
        all_X = [None for _ in PHYSIONET_2019_DATASETS]
        all_y = [None for _ in PHYSIONET_2019_DATASETS]
        for i, files in enumerate(data_files):
            all_X[i], all_y[i] = self._process_files(files, max(length), channels)
        # Form tensors
        X = torch.cat(all_X)
        y = torch.cat(all_y)
        length = torch.tensor(length)
        # Move time channel (ICULOS) to first channel
        X = torch.cat((X[:, :, -1].unsqueeze(-1), X[:, :, :-1]), dim=2)
        return X, y, length


class PhysioNet2019Binary(_TimeSeriesDataset):
    """**Returns a binary prediction variant of the PhysioNet Challenge 2019 data as a
    PyTorch Dataset.**

    In contrast with the full challenge, the first 72 hours of data are used to predict
    whether a patient develops sepsis at any point during the period of hospitalisation
    as in `Kidger et al (2020) <https://arxiv.org/abs/2005.08926>`_. See the PhysioNet
    `website <https://physionet.org/content/challenge-2019/1.0.0/>`_ for a description
    of the data set.

    The proportion of data in the training, validation and (optional) test data sets are
    specified by the ``train_prop`` and ``val_prop`` arguments. For a
    training/validation split specify ``train_prop`` only. For a
    training/validation/test split specify both ``train_prop`` and ``val_prop``.

    For example ``train_prop=0.8`` generates a 80/20% train/validation split, but
    ``train_prop=0.8``, ``val_prop=0.1`` generates a 80/10/10% train/validation/test
    split. Splits are formed using stratified sampling.

    When passed to a PyTorch DataLoader, batches are a named dictionary with ``X``,
    ``y`` and ``length`` data. The ``split`` argument determines whether training,
    validation or test data are returned.

    Missing data can imputed using the ``impute`` argument. See the `missing data
    tutorial <https://philipdarke.com/torchtime/tutorials/missing_data.html>`_ for more
    information.

    Args:
        split: The data split to return, either *train*, *val* (validation) or *test*.
        train_prop: Proportion of data in the training set.
        val_prop: Proportion of data in the validation set (optional, see above).
        impute: Method used to impute missing data, either *none*, *zero*, *mean*,
            *forward* or a custom imputation function (default "none").
        time: Append time stamp in the first channel (default True).
        mask: Append missing data mask for each channel (default False).
        delta: Append time since previous observation for each channel calculated as in
            `Che et al (2018) <https://doi.org/10.1038/s41598-018-24271-9>`_. Default
            False.
        standardise: Standardise the time series, either *all* (all channels), *data*
            (time series channels only) or *none* (no standardisation) (default "none").
        overwrite_cache: Overwrite saved cache (default False).
        path: Location of the ``.torchtime`` cache directory (default ".").
        seed: Random seed for reproducibility (optional).

    Attributes:
        X (Tensor): A tensor of default shape (*n*, *s*, *c* + 1) where *n* = number of
            sequences, *s* = (longest) sequence length and *c* = number of channels
            in the PhysioNet data (*including* the ``ICULOS`` time stamp). The channels
            are ordered as set out on the PhysioNet `website
            <https://physionet.org/content/challenge-2019/1.0.0/>`_. By default, a
            time stamp is appended as the first channel. If ``time`` is False, the
            time stamp is omitted and the tensor has shape (*n*, *s*, *c*).

            A missing data mask and/or time delta channels can be appended with the
            ``mask`` and ``delta`` arguments. These each have the same number of
            channels as the Physionet data. For example, if ``time``, ``mask`` and
            ``delta`` are all True, ``X`` has shape (*n*, *s*, 3 * *c* + 1 = 121) and
            the channels are in the order: time stamp, time series, missing data mask,
            time deltas.

            Note that PhysioNet sequences are of unequal length and are therefore
            padded with ``NaNs`` to the length of the longest sequence in the data.
        y (Tensor): Whether patient is diagnosed with sepsis at any time during
            hospitalisation. A tensor of shape (*n*, 1).
        length (Tensor): Length of each sequence prior to padding. A tensor of shape
            (*n*).
        n_channels: Number of time series channels in data.

    .. note::
        ``X``, ``y`` and ``length`` are available for the training, validation and test
        splits by appending ``_train``, ``_val`` and ``_test`` respectively. For
        example, ``y_val`` returns the labels for the validation data set. These
        attributes are available regardless of the ``split`` argument.

    Returns:
        A PyTorch Dataset object which can be passed to a DataLoader.
    """

    def __init__(
        self,
        split: str,
        train_prop: float,
        val_prop: float = None,
        impute: Union[str, Callable[[Tensor], Tensor]] = "none",
        time: bool = True,
        mask: bool = False,
        delta: bool = False,
        standardise: str = "none",
        overwrite_cache: bool = False,
        path: str = ".",
        seed: int = None,
    ) -> None:
        self.DATASET_NAME = "physionet_2019binary"
        self.path_arg = path
        self.max_time = 72  # hours
        super(PhysioNet2019Binary, self).__init__(
            dataset=self.DATASET_NAME,
            split=split,
            train_prop=train_prop,
            val_prop=val_prop,
            impute=impute,
            time=time,
            mask=mask,
            delta=delta,
            standardise=standardise,
            overwrite_cache=overwrite_cache,
            path=path,
            seed=seed,
        )

    def _process_files(self, files, max_length, channels):
        """Process ``.psv`` files."""
        X = np.full((len(files), max_length, channels - 1), float("nan"))
        y = np.full((len(files), 1), 0.0)
        for i, file_i in tqdm(
            enumerate(files),
            total=len(files),
            bar_format=TQDM_FORMAT,
        ):
            with open(file_i) as file:
                reader = csv.reader(file, delimiter="|")
                for j, Xij in enumerate(reader):
                    if j > 0:  # ignore header
                        if int(Xij[39]) <= self.max_time:
                            X[i, j - 1] = Xij[:-1]
                        y[i, 0] = max(y[i, 0], int(Xij[-1]))  # sepsis at any point
        return torch.tensor(X), torch.tensor(y)

    def _get_data(self):
        """Download data and form ``X``, ``y``, ``length`` tensors."""
        # Download and extract data in "physionet2019" directory to avoid duplication
        cache_path = pathlib.Path() / self.path_arg / ".torchtime" / "physionet_2019"
        _physionet_download(PHYSIONET_2019_DATASETS, cache_path, self.overwrite_cache)
        # Prepare data
        data_directories = [cache_path / dataset for dataset in PHYSIONET_2019_DATASETS]
        data_files = _get_file_list(data_directories)
        length, channels = PhysioNet2019._get_lengths_channels(
            data_files, max_time=self.max_time
        )
        all_X = [None for _ in PHYSIONET_2019_DATASETS]
        all_y = [None for _ in PHYSIONET_2019_DATASETS]
        for i, files in enumerate(data_files):
            all_X[i], all_y[i] = self._process_files(files, max(length), channels)
        # Form tensors
        X = torch.cat(all_X)
        y = torch.cat(all_y)
        length = torch.tensor(length)
        # Drop patients with zero length sequences
        patient_idx = torch.arange(X.size(0)).masked_select(length != 0).int()
        X = X.index_select(index=patient_idx, dim=0)
        y = y.index_select(index=patient_idx, dim=0)
        length = length.index_select(index=patient_idx, dim=0)
        # Save cached files to "physionet2019binary" directory
        self.path = pathlib.Path() / self.path_arg / ".torchtime" / self.DATASET_NAME
        return X, y, length


class UEA(_TimeSeriesDataset):
    """
    **Returns a time series classification data set from the UEA/UCR
    repository as a PyTorch Dataset.** See the UEA/UCR repository `website
    <https://www.timeseriesclassification.com/>`_ for the data sets.

    The proportion of data in the training, validation and (optional) test data sets are
    specified by the ``train_prop`` and ``val_prop`` arguments. For a
    training/validation split specify ``train_prop`` only. For a
    training/validation/test split specify both ``train_prop`` and ``val_prop``.

    For example ``train_prop=0.8`` generates a 80/20% train/validation split, but
    ``train_prop=0.8``, ``val_prop=0.1`` generates a 80/10/10% train/validation/test
    split. Splits are formed using stratified sampling.

    When passed to a PyTorch DataLoader, batches are a named dictionary with ``X``,
    ``y`` and ``length`` data. The ``split`` argument determines whether training,
    validation or test data are returned.

    Missing data can be simulated by dropping data at random. Support is also provided
    to impute missing data. These options are controlled by the ``missing`` and
    ``impute`` arguments. See the `missing data tutorial
    <https://philipdarke.com/torchtime/tutorials/missing_data.html>`_ for more
    information.

    .. warning::
        Mean imputation is unsuitable for categorical variables. To impute missing
        values for a categorical variable with the channel mode (rather than the channel
        mean), pass the channel indices to the ``categorical`` argument. Note this is
        also required for forward imputation to appropriately impute initial missing
        values.

        Alternatively, the calculated channel mean/mode can be overridden using the
        ``channel_means`` argument. This can be used to impute missing data with a fixed
        value.

    Args:
        dataset: The UEA/UCR data set from list `here
            <https://timeseriesclassification.com/dataset.php>`_.
        split: The data split to return, either *train*, *val* (validation) or *test*.
        train_prop: Proportion of data in the training set.
        val_prop: Proportion of data in the validation set (optional, see above).
        missing: The proportion of data to drop at random. If ``missing`` is a single
            value, data are dropped from all channels. To drop data independently across
            each channel, pass a list of the proportion missing for each channel e.g.
            ``[0.5, 0.2, 0.8]``. Default 0 i.e. no missing data simulation.
        impute: Method used to impute missing data, either *none*, *zero*, *mean*,
            *forward* or a custom imputation function (default "none"). See warning
            above.
        categorical: List with channel indices of categorical variables. Only required
            if imputing data. Default ``[]`` i.e. no categorical variables.
        channel_means: Override the calculated channel mean/mode when imputing data.
            Only used if imputing data. Dictionary with channel indices and values e.g.
            ``{1: 4.5, 3: 7.2}`` (default ``{}`` i.e. no overridden channel mean/modes).
        time: Append time stamp in the first channel (default True).
        mask: Append missing data mask for each channel (default False).
        delta: Append time since previous observation for each channel calculated as in
            `Che et al (2018) <https://doi.org/10.1038/s41598-018-24271-9>`_. Default
            False.
        standardise: Standardise the time series, either *all* (all channels), *data*
            (time series channels only) or *none* (no standardisation) (default "none").
        overwrite_cache: Overwrite saved cache (default False).
        path: Location of the ``.torchtime`` cache directory (default ".").
        seed: Random seed for reproducibility (optional).

    Attributes:
        X (Tensor): A tensor of default shape (*n*, *s*, *c* + 1) where *n* = number of
            sequences, *s* = (longest) sequence length and *c* = number of
            channels. By default, a time stamp is appended as the first channel. If
            ``time`` is False, the time stamp is omitted and the tensor has shape
            (*n*, *s*, *c*).

            A missing data mask and/or time delta channels can be appended with the
            ``mask`` and ``delta`` arguments. These each have the same number of
            channels as the data set. For example, if ``time``, ``mask`` and
            ``delta`` are all True, ``X`` has shape (*n*, *s*, 3 * *c* + 1) and the
            channels are in the order: time stamp, time series, missing data mask, time
            deltas.

            Where sequences are of unequal lengths they are padded with ``NaNs`` to
            the length of the longest sequence in the data.
        y (Tensor): One-hot encoded label data. A tensor of shape (*n*, *l*) where *l*
            is the number of classes.
        length (Tensor): Length of each sequence prior to padding. A tensor of shape
            (*n*).
        n_channels: Number of time series channels in data.

    .. note::
        ``X``, ``y`` and ``length`` are available for the training, validation and test
        splits by appending ``_train``, ``_val`` and ``_test`` respectively. For
        example, ``y_val`` returns the labels for the validation data set. These
        attributes are available regardless of the ``split`` argument.

    Returns:
        A PyTorch Dataset object which can be passed to a DataLoader.
    """

    def __init__(
        self,
        dataset: str,
        split: str,
        train_prop: float,
        val_prop: float = None,
        missing: Union[float, List[float]] = 0.0,
        impute: Union[str, Callable[[Tensor], Tensor]] = "none",
        categorical: List[int] = [],
        channel_means: Dict[int, float] = {},
        time: bool = True,
        mask: bool = False,
        delta: bool = False,
        standardise: str = "none",
        overwrite_cache: bool = False,
        path: str = ".",
        seed: int = None,
    ) -> None:
        self.dataset_name = dataset
        self.raw_path = (
            pathlib.Path() / path / ".torchtime" / ("uea_" + self.dataset_name) / "raw"
        )
        super(UEA, self).__init__(
            dataset="uea_" + self.dataset_name,
            split=split,
            train_prop=train_prop,
            val_prop=val_prop,
            missing=missing,
            impute=impute,
            categorical=categorical,
            channel_means=channel_means,
            time=time,
            mask=mask,
            delta=delta,
            standardise=standardise,
            overwrite_cache=overwrite_cache,
            path=path,
            seed=seed,
        )

    def _download_uea_data(self, url, path):
        """Download UEA data if not already downloaded."""
        train_path = path / (self.dataset_name + "_TRAIN.ts")
        test_path = path / (self.dataset_name + "_TEST.ts")
        # Download and extract archive
        if not path.is_dir():
            _download_archive(url, path)
        else:
            downloaded_files = _get_file_list(path)
            if train_path not in downloaded_files or test_path not in downloaded_files:
                _download_archive(url, path)
        # Verify download
        downloaded_files = _get_file_list(path)
        assert (
            train_path in downloaded_files
        ), "{} not in downloaded archive, check {}".format(train_path, url)
        assert (
            test_path in downloaded_files
        ), "{} not in downloaded archive, check {}".format(test_path, url)
        return [train_path, test_path]

    @staticmethod
    def _extract_ts_files(data_files):
        """Extract ``.ts`` data based on ``sktime.datasets.load_UCR_UEA_dataset()``."""
        X = pd.DataFrame(dtype="object")
        y = pd.Series(dtype="object")
        for split in data_files:
            contents = load_from_tsfile_to_dataframe(split)
            X = pd.concat([X, pd.DataFrame(contents[0])])
            y = pd.concat([y, pd.Series(contents[1])])
        y = pd.Series.to_numpy(y, dtype=str)
        return X, y

    def _pad(self, Xi, max_length):
        """Pad sequences to length ``max_length``."""
        Xi = pad_sequence([torch.tensor(Xij) for Xij in Xi])
        out = torch.full((max_length, Xi.size(1)), float("nan"))  # shape (s, c)
        out[0 : Xi.size(0)] = Xi
        return out

    def _get_data(self):
        """Download data and form ``X``, ``y`` and ``length`` tensors."""
        data_files = self._download_uea_data(
            UEA_DOWNLOAD_URL + self.dataset_name + ".zip", self.raw_path
        )
        X_raw, y_raw = self._extract_ts_files(data_files)
        # Length of each sequence
        channel_lengths = X_raw.apply(lambda Xi: Xi.apply(len), axis=1)
        length = torch.tensor(channel_lengths.apply(max, axis=1).values)
        # Form tensor with padded sequences
        X = torch.stack(
            [self._pad(X_raw.iloc[i], length.max()) for i in range(len(X_raw))],
            dim=0,
        )
        # One-hot encode labels (start from zero)
        y = torch.tensor(y_raw.astype(int))
        if all(y != 0):
            y -= 1
        y = F.one_hot(y)
        return X, y, length
