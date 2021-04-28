# Copyright 2020 - 2021 MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import warnings
from copy import deepcopy
from typing import TYPE_CHECKING, Callable, Optional, Sequence, Union

import torch
from torch.utils.data import DataLoader as TorchDataLoader

from monai.data import BatchInverseTransform
from monai.data.utils import no_collation
from monai.engines.utils import CommonKeys, IterationEvents
from monai.transforms import InvertibleTransform, ToTensor, allow_missing_keys_mode, convert_inverse_interp_mode
from monai.utils import InverseKeys, ensure_tuple, ensure_tuple_rep, exact_version, optional_import

Events, _ = optional_import("ignite.engine", "0.4.4", exact_version, "Events")
if TYPE_CHECKING:
    from ignite.engine import Engine
else:
    Engine, _ = optional_import("ignite.engine", "0.4.4", exact_version, "Engine")


class TransformInverter:
    """
    Ignite handler to automatically invert `transforms`.
    It takes `engine.state.output` as the input data and uses the transforms information from `engine.state.batch`.
    The inverted data are stored in `engine.state.output` with key: "{output_key}_{postfix}".
    And the inverted meta dict will be stored in `engine.state.batch`
    with key: "{output_key}_{postfix}_{meta_key_postfix}".

    """

    def __init__(
        self,
        transform: InvertibleTransform,
        loader: TorchDataLoader,
        output_keys: Union[str, Sequence[str]] = CommonKeys.PRED,
        batch_keys: Union[str, Sequence[str]] = CommonKeys.IMAGE,
        meta_key_postfix: str = "meta_dict",
        collate_fn: Optional[Callable] = no_collation,
        postfix: str = "inverted",
        nearest_interp: Union[bool, Sequence[bool]] = True,
        to_tensor: Union[bool, Sequence[bool]] = True,
        device: Union[Union[str, torch.device], Sequence[Union[str, torch.device]]] = "cpu",
        post_func: Union[Callable, Sequence[Callable]] = lambda x: x,
        num_workers: Optional[int] = 0,
    ) -> None:
        """
        Args:
            transform: a callable data transform on input data.
            loader: data loader used to run transforms and generate the batch of data.
            output_keys: the key of expected data in `ignite.engine.output`, invert transforms on it.
                it also can be a list of keys, will invert transform for each of them. Default to "pred".
            batch_keys: the key of input data in `ignite.engine.batch`. will get the applied transforms
                for this input data, then invert them for the expected data with `output_keys`.
                It can also be a list of keys, each matches to the `output_keys` data. default to "image".
            meta_key_postfix: use `{batch_key}_{postfix}` to to fetch the meta data according to the key data,
                default is `meta_dict`, the meta data is a dictionary object.
                For example, to handle key `image`,  read/write affine matrices from the
                metadata `image_meta_dict` dictionary's `affine` field.
            collate_fn: how to collate data after inverse transformations.
                default won't do any collation, so the output will be a list of size batch size.
            postfix: will save the inverted result into `ignite.engine.output` with key `{output_key}_{postfix}`.
            nearest_interp: whether to use `nearest` interpolation mode when inverting the spatial transforms,
                default to `True`. If `False`, use the same interpolation mode as the original transform.
                it also can be a list of bool, each matches to the `output_keys` data.
            to_tensor: whether to convert the inverted data into PyTorch Tensor first, default to `True`.
                it also can be a list of bool, each matches to the `output_keys` data.
            device: if converted to Tensor, move the inverted results to target device before `post_func`,
                default to "cpu", it also can be a list of string or `torch.device`,
                each matches to the `output_keys` data.
            post_func: post processing for the inverted data, should be a callable function.
                it also can be a list of callable, each matches to the `output_keys` data.
            num_workers: number of workers when run data loader for inverse transforms,
                default to 0 as only run one iteration and multi-processing may be even slower.
                Set to `None`, to use the `num_workers` of the input transform data loader.

        """
        self.transform = transform
        self.inverter = BatchInverseTransform(
            transform=transform,
            loader=loader,
            collate_fn=collate_fn,
            num_workers=num_workers,
        )
        self.output_keys = ensure_tuple(output_keys)
        self.batch_keys = ensure_tuple_rep(batch_keys, len(self.output_keys))
        self.meta_key_postfix = meta_key_postfix
        self.postfix = postfix
        self.nearest_interp = ensure_tuple_rep(nearest_interp, len(self.output_keys))
        self.to_tensor = ensure_tuple_rep(to_tensor, len(self.output_keys))
        self.device = ensure_tuple_rep(device, len(self.output_keys))
        self.post_func = ensure_tuple_rep(post_func, len(self.output_keys))
        self._totensor = ToTensor()

    def attach(self, engine: Engine) -> None:
        """
        Args:
            engine: Ignite Engine, it can be a trainer, validator or evaluator.
        """
        engine.add_event_handler(IterationEvents.MODEL_COMPLETED, self)

    def __call__(self, engine: Engine) -> None:
        """
        Args:
            engine: Ignite Engine, it can be a trainer, validator or evaluator.
        """
        for output_key, batch_key, nearest_interp, to_tensor, device, post_func in zip(
            self.output_keys, self.batch_keys, self.nearest_interp, self.to_tensor, self.device, self.post_func
        ):
            transform_key = batch_key + InverseKeys.KEY_SUFFIX
            if transform_key not in engine.state.batch:
                warnings.warn(f"all the transforms on `{batch_key}` are not InvertibleTransform.")
                continue

            transform_info = engine.state.batch[transform_key]
            if nearest_interp:
                transform_info = convert_inverse_interp_mode(
                    trans_info=deepcopy(transform_info),
                    mode="nearest",
                    align_corners=None,
                )

            output = engine.state.output[output_key]
            if isinstance(output, torch.Tensor):
                output = output.detach()
            segs_dict = {
                batch_key: output,
                transform_key: transform_info,
            }
            meta_dict_key = f"{batch_key}_{self.meta_key_postfix}"
            if meta_dict_key in engine.state.batch:
                segs_dict[meta_dict_key] = engine.state.batch[meta_dict_key]

            with allow_missing_keys_mode(self.transform):  # type: ignore
                inverted = self.inverter(segs_dict)

            # save the inverted data into state.output
            inverted_key = f"{output_key}_{self.postfix}"
            engine.state.output[inverted_key] = [
                post_func(self._totensor(i[batch_key]).to(device) if to_tensor else i[batch_key]) for i in inverted
            ]

            # save the inverted meta dict into state.batch
            if meta_dict_key in engine.state.batch:
                engine.state.batch[f"{inverted_key}_{self.meta_key_postfix}"] = [i.get(meta_dict_key) for i in inverted]
