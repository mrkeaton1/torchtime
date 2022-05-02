import re

import pytest
import torch

from torchtime.data import UEA

SEED = 456789
RTOL = 1e-4
ATOL = 1e-4


class TestUEACharacterTrajectories:
    def test_invalid_split_arg(self):
        """Catch invalid split argument."""
        with pytest.raises(
            AssertionError,
            match=re.escape("argument 'split' must be one of ['train', 'val']"),
        ):
            UEA(
                dataset="CharacterTrajectories",
                split="xyz",
                train_prop=0.8,
                seed=SEED,
            )

    def test_invalid_split_size(self):
        """Catch invalid split sizes."""
        with pytest.raises(
            AssertionError,
            match=re.escape("argument 'train_prop' must be in range (0, 1)"),
        ):
            UEA(
                dataset="CharacterTrajectories",
                split="train",
                train_prop=-0.5,
                seed=SEED,
            )

    def test_incompatible_split_size(self):
        """Catch incompatible split sizes."""
        with pytest.raises(
            AssertionError,
            match=re.escape("argument 'train_prop' must be in range (0, 1)"),
        ):
            UEA(
                dataset="CharacterTrajectories",
                split="train",
                train_prop=1,
                seed=SEED,
            )
        with pytest.raises(
            AssertionError,
            match=re.escape("argument 'val_prop' must be in range (0, 1-train_prop)"),
        ):
            UEA(
                dataset="CharacterTrajectories",
                split="test",
                train_prop=0.5,
                val_prop=0.5,
                seed=SEED,
            )

    def test_train_val(self):
        """Test training/validation split sizes."""
        dataset = UEA(
            dataset="CharacterTrajectories",
            split="train",
            train_prop=0.7,
            seed=SEED,
        )
        # Check data set size
        assert dataset.X_train.shape == torch.Size([2001, 182, 4])
        assert dataset.y_train.shape == torch.Size([2001, 20])
        assert dataset.length_train.shape == torch.Size([2001])
        assert dataset.X_val.shape == torch.Size([857, 182, 4])
        assert dataset.y_val.shape == torch.Size([857, 20])
        assert dataset.length_val.shape == torch.Size([857])
        # Ensure no test data is returned
        with pytest.raises(
            AttributeError, match=re.escape("'UEA' object has no attribute 'X_test'")
        ):
            dataset.X_test
        with pytest.raises(
            AttributeError, match=re.escape("'UEA' object has no attribute 'y_test'")
        ):
            dataset.y_test
        with pytest.raises(
            AttributeError,
            match=re.escape("'UEA' object has no attribute 'length_test'"),
        ):
            dataset.length_test

    def test_train_val_test(self):
        """Test training/validation/test split sizes."""
        dataset = UEA(
            dataset="CharacterTrajectories",
            split="train",
            train_prop=0.7,
            val_prop=0.2,
            seed=SEED,
        )
        # Check data set size
        assert dataset.X_train.shape == torch.Size([2002, 182, 4])
        assert dataset.y_train.shape == torch.Size([2002, 20])
        assert dataset.length_train.shape == torch.Size([2002])
        assert dataset.X_val.shape == torch.Size([571, 182, 4])
        assert dataset.y_val.shape == torch.Size([571, 20])
        assert dataset.length_val.shape == torch.Size([571])
        assert dataset.X_test.shape == torch.Size([285, 182, 4])
        assert dataset.y_test.shape == torch.Size([285, 20])
        assert dataset.length_test.shape == torch.Size([285])

    def test_train_split(self):
        """Test training split is returned."""
        dataset = UEA(
            dataset="CharacterTrajectories",
            split="train",
            train_prop=0.7,
            val_prop=0.2,
            seed=SEED,
        )
        # Check correct split is returned
        assert torch.allclose(dataset.X, dataset.X_train, equal_nan=True)
        assert torch.allclose(dataset.y, dataset.y_train, equal_nan=True)
        assert torch.allclose(dataset.length, dataset.length_train, equal_nan=True)

    def test_val_split(self):
        """Test validation split is returned."""
        dataset = UEA(
            dataset="CharacterTrajectories",
            split="val",
            train_prop=0.7,
            val_prop=0.2,
            seed=SEED,
        )
        # Check correct split is returned
        assert torch.allclose(dataset.X, dataset.X_val, equal_nan=True)
        assert torch.allclose(dataset.y, dataset.y_val, equal_nan=True)
        assert torch.allclose(dataset.length, dataset.length_val, equal_nan=True)

    def test_test_split(self):
        """Test test split is returned."""
        dataset = UEA(
            dataset="CharacterTrajectories",
            split="test",
            train_prop=0.7,
            val_prop=0.2,
            seed=SEED,
        )
        # Check correct split is returned
        assert torch.allclose(dataset.X, dataset.X_test, equal_nan=True)
        assert torch.allclose(dataset.y, dataset.y_test, equal_nan=True)
        assert torch.allclose(dataset.length, dataset.length_test, equal_nan=True)

    def test_length(self):
        """Test length attribute."""
        dataset = UEA(
            dataset="CharacterTrajectories",
            split="train",
            train_prop=0.7,
            val_prop=0.2,
            time=False,
            seed=SEED,
        )
        for i, Xi in enumerate(dataset.X_train.unbind()):
            length_i = dataset.length_train[i]
            assert not torch.all(torch.isnan(Xi[length_i - 1]))
            assert torch.all(torch.isnan(Xi[length_i:]))
        for i, Xi in enumerate(dataset.X_val.unbind()):
            length_i = dataset.length_val[i]
            assert not torch.all(torch.isnan(Xi[length_i - 1]))
            assert torch.all(torch.isnan(Xi[length_i:]))
        for i, Xi in enumerate(dataset.X_test.unbind()):
            length_i = dataset.length_test[i]
            assert not torch.all(torch.isnan(Xi[length_i - 1]))
            assert torch.all(torch.isnan(Xi[length_i:]))

    def test_missing(self):
        """Test missing data simulation."""
        dataset = UEA(
            dataset="CharacterTrajectories",
            split="train",
            train_prop=0.7,
            val_prop=0.2,
            missing=0.5,
            seed=SEED,
        )
        # Check number of NaNs
        assert (
            torch.sum(torch.isnan(dataset.X_train)).item() == 732969
        )  # expect around 3 * (239873 * 0.5 + 2002 * 182 - 239873) = 733,283
        assert (
            torch.sum(torch.isnan(dataset.X_val)).item() == 208686
        )  # expect around 3 * (68797 * 0.5 + 571 * 182 - 68797) = 208,571
        assert (
            torch.sum(torch.isnan(dataset.X_test)).item() == 103872
        )  # expect around 3 * (34269 * 0.5 + 285 * 182 - 34269) = 104,207

    def test_invalid_impute(self):
        """Catch invalid impute arguments."""
        with pytest.raises(
            AssertionError,
            match=re.escape(
                "argument 'impute' must be a string in dict_keys(['none', 'mean', 'forward']) or a function"  # noqa: E501
            ),
        ):
            UEA(
                dataset="CharacterTrajectories",
                split="train",
                train_prop=0.7,
                missing=0.5,
                impute="blah",
                seed=SEED,
            )
        with pytest.raises(
            Exception,
            match=re.escape(
                "argument 'impute' must be a string in dict_keys(['none', 'mean', 'forward']) or a function"  # noqa: E501
            ),
        ):
            UEA(
                dataset="CharacterTrajectories",
                split="train",
                train_prop=0.7,
                missing=0.5,
                impute=3,
                seed=SEED,
            )

    def test_no_impute(self):
        """Test no imputation."""
        dataset = UEA(
            dataset="CharacterTrajectories",
            split="train",
            train_prop=0.7,
            val_prop=0.2,
            missing=0.5,
            impute="none",
            seed=SEED,
        )
        # Check number of NaNs
        assert torch.sum(torch.isnan(dataset.X_train)).item() == 732969
        assert torch.sum(torch.isnan(dataset.y_train)).item() == 0
        assert torch.sum(torch.isnan(dataset.X_val)).item() == 208686
        assert torch.sum(torch.isnan(dataset.y_val)).item() == 0
        assert torch.sum(torch.isnan(dataset.X_test)).item() == 103872
        assert torch.sum(torch.isnan(dataset.y_test)).item() == 0

    def test_mean_impute(self):
        """Test mean imputation."""
        dataset = UEA(
            dataset="CharacterTrajectories",
            split="train",
            train_prop=0.7,
            val_prop=0.2,
            missing=0.5,
            impute="mean",
            seed=SEED,
        )
        # Check no NaNs post imputation
        assert torch.sum(torch.isnan(dataset.X_train)).item() == 0
        assert torch.sum(torch.isnan(dataset.y_train)).item() == 0
        assert torch.sum(torch.isnan(dataset.X_val)).item() == 0
        assert torch.sum(torch.isnan(dataset.y_val)).item() == 0
        assert torch.sum(torch.isnan(dataset.X_test)).item() == 0
        assert torch.sum(torch.isnan(dataset.y_test)).item() == 0

    def test_forward_impute(self):
        """Test forward imputation."""
        dataset = UEA(
            dataset="CharacterTrajectories",
            split="train",
            train_prop=0.7,
            val_prop=0.2,
            missing=0.5,
            impute="forward",
            seed=SEED,
        )
        # Check no NaNs post imputation
        assert torch.sum(torch.isnan(dataset.X_train)).item() == 0
        assert torch.sum(torch.isnan(dataset.y_train)).item() == 0
        assert torch.sum(torch.isnan(dataset.X_val)).item() == 0
        assert torch.sum(torch.isnan(dataset.y_val)).item() == 0
        assert torch.sum(torch.isnan(dataset.X_test)).item() == 0
        assert torch.sum(torch.isnan(dataset.y_test)).item() == 0

    def test_custom_impute(self):
        """Test custom imputation function."""

        def custom_imputer(X, y, fill):
            """Does not impute data i.e. same as impute='none'"""
            return X, y

        dataset = UEA(
            dataset="CharacterTrajectories",
            split="train",
            train_prop=0.7,
            val_prop=0.2,
            missing=0.5,
            impute=custom_imputer,
            seed=SEED,
        )
        # Check number of NaNs
        assert torch.sum(torch.isnan(dataset.X_train)).item() == 732969
        assert torch.sum(torch.isnan(dataset.y_train)).item() == 0
        assert torch.sum(torch.isnan(dataset.X_val)).item() == 208686
        assert torch.sum(torch.isnan(dataset.y_val)).item() == 0
        assert torch.sum(torch.isnan(dataset.X_test)).item() == 103872
        assert torch.sum(torch.isnan(dataset.y_test)).item() == 0

    def test_time(self):
        """Test time argument."""
        dataset = UEA(
            dataset="CharacterTrajectories",
            split="train",
            train_prop=0.7,
            val_prop=0.2,
            time=True,
            seed=SEED,
        )
        # Check data set size
        assert dataset.X_train.shape == torch.Size([2002, 182, 4])
        assert dataset.X_val.shape == torch.Size([571, 182, 4])
        assert dataset.X_test.shape == torch.Size([285, 182, 4])
        # Check time channel
        for i in range(182):
            assert torch.equal(
                dataset.X_train[:, i, 0],
                torch.full([2002], fill_value=i, dtype=torch.float),
            )
            assert torch.equal(
                dataset.X_val[:, i, 0],
                torch.full([571], fill_value=i, dtype=torch.float),
            )
            assert torch.equal(
                dataset.X_test[:, i, 0],
                torch.full([285], fill_value=i, dtype=torch.float),
            )

    def test_no_time(self):
        """Test time argument."""
        dataset = UEA(
            dataset="CharacterTrajectories",
            split="train",
            train_prop=0.7,
            val_prop=0.2,
            time=False,
            seed=SEED,
        )
        # Check data set size
        assert dataset.X_train.shape == torch.Size([2002, 182, 3])
        assert dataset.X_val.shape == torch.Size([571, 182, 3])
        assert dataset.X_test.shape == torch.Size([285, 182, 3])

    def test_mask(self):
        """Test mask argument."""
        dataset = UEA(
            dataset="CharacterTrajectories",
            split="train",
            train_prop=0.7,
            val_prop=0.2,
            time=False,
            mask=True,
            seed=SEED,
        )
        # Check data set size
        assert dataset.X_train.shape == torch.Size([2002, 182, 6])
        assert dataset.X_val.shape == torch.Size([571, 182, 6])
        assert dataset.X_test.shape == torch.Size([285, 182, 6])
        # Check mask channel
        assert torch.sum(dataset.X_train[:, :, 3]) == torch.sum(dataset.length_train)
        assert torch.sum(dataset.X_val[:, :, 3]) == torch.sum(dataset.length_val)
        assert torch.sum(dataset.X_test[:, :, 3]) == torch.sum(dataset.length_test)

    def test_delta(self):
        """Test time delta argument."""
        dataset = UEA(
            dataset="CharacterTrajectories",
            split="train",
            train_prop=0.7,
            val_prop=0.2,
            time=False,
            delta=True,
            seed=SEED,
        )
        # Check data set size
        assert dataset.X_train.shape == torch.Size([2002, 182, 6])
        assert dataset.X_val.shape == torch.Size([571, 182, 6])
        assert dataset.X_test.shape == torch.Size([285, 182, 6])
        # Check time delta channel
        assert torch.equal(
            dataset.X_train[:, 0, 3], torch.zeros([2002], dtype=torch.float)
        )
        assert torch.equal(
            dataset.X_val[:, 0, 3], torch.zeros([571], dtype=torch.float)
        )
        assert torch.equal(
            dataset.X_test[:, 0, 3], torch.zeros([285], dtype=torch.float)
        )

    def test_time_mask_delta(self):
        """Test combination of time/mask/delta arguments."""
        dataset = UEA(
            dataset="CharacterTrajectories",
            split="train",
            train_prop=0.7,
            val_prop=0.2,
            mask=True,
            delta=True,
            seed=SEED,
        )
        # Check data set size
        assert dataset.X_train.shape == torch.Size([2002, 182, 10])
        assert dataset.X_val.shape == torch.Size([571, 182, 10])
        assert dataset.X_test.shape == torch.Size([285, 182, 10])
        # Check time channel
        for i in range(182):
            assert torch.equal(
                dataset.X_train[:, i, 0],
                torch.full([2002], fill_value=i, dtype=torch.float),
            )
            assert torch.equal(
                dataset.X_val[:, i, 0],
                torch.full([571], fill_value=i, dtype=torch.float),
            )
            assert torch.equal(
                dataset.X_test[:, i, 0],
                torch.full([285], fill_value=i, dtype=torch.float),
            )
        # Check mask channel
        assert torch.sum(dataset.X_train[:, :, 4]) == torch.sum(dataset.length_train)
        assert torch.sum(dataset.X_val[:, :, 4]) == torch.sum(dataset.length_val)
        assert torch.sum(dataset.X_test[:, :, 4]) == torch.sum(dataset.length_test)
        # Check time delta channel
        assert torch.equal(
            dataset.X_train[:, 0, 7], torch.zeros([2002], dtype=torch.float)
        )
        assert torch.equal(
            dataset.X_val[:, 0, 7], torch.zeros([571], dtype=torch.float)
        )
        assert torch.equal(
            dataset.X_test[:, 0, 7], torch.zeros([285], dtype=torch.float)
        )

    def test_downscale(self):
        """Test downscale argument."""
        dataset = UEA(
            dataset="CharacterTrajectories",
            split="train",
            train_prop=0.7,
            val_prop=0.2,
            downscale=0.1,
            seed=SEED,
        )
        # Check data set size
        assert dataset.X_train.shape == torch.Size([200, 164, 4])
        assert dataset.y_train.shape == torch.Size([200, 20])
        assert dataset.length_train.shape == torch.Size([200])
        assert dataset.X_val.shape == torch.Size([57, 164, 4])
        assert dataset.y_val.shape == torch.Size([57, 20])
        assert dataset.length_val.shape == torch.Size([57])
        assert dataset.X_test.shape == torch.Size([28, 164, 4])
        assert dataset.y_test.shape == torch.Size([28, 20])
        assert dataset.length_test.shape == torch.Size([28])

    def test_reproducibility_1(self):
        """Test seed argument."""
        dataset = UEA(
            dataset="CharacterTrajectories",
            split="train",
            train_prop=0.7,
            val_prop=0.2,
            seed=SEED,
        )
        # Check first value in each data set
        assert torch.allclose(
            dataset.X_train[0, 0, 1], torch.tensor(-0.1869), rtol=RTOL, atol=ATOL
        )
        assert torch.allclose(
            dataset.X_val[0, 0, 1], torch.tensor(0.0418), rtol=RTOL, atol=ATOL
        )
        assert torch.allclose(
            dataset.X_test[0, 0, 1], torch.tensor(0.0940), rtol=RTOL, atol=ATOL
        )

    def test_reproducibility_2(self):
        """Test seed argument."""
        dataset = UEA(
            dataset="CharacterTrajectories",
            split="train",
            train_prop=0.7,
            val_prop=0.2,
            seed=999999,
        )
        # Check first value in each data set
        assert torch.allclose(
            dataset.X_train[0, 0, 1], torch.tensor(0.0391), rtol=RTOL, atol=ATOL
        )
        assert torch.allclose(
            dataset.X_val[0, 0, 1], torch.tensor(0.0), rtol=RTOL, atol=ATOL
        )
        assert torch.allclose(
            dataset.X_test[0, 0, 1], torch.tensor(-0.0767), rtol=RTOL, atol=ATOL
        )
