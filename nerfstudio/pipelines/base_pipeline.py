# Copyright 2022 The Nerfstudio Team. All rights reserved.
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

"""
Abstracts for the Pipeline class.
"""
from __future__ import annotations

import random
import typing
from abc import abstractmethod
from dataclasses import dataclass, field
from time import time
from typing import Any, Dict, List, Optional, Type, Union, cast

import torch
import torch.distributed as dist
from rich.progress import (
    BarColumn,
    Console,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
    track,
)
from torch import nn
from torch.nn import Parameter
from torch.nn.parallel import DistributedDataParallel as DDP
from tqdm import tqdm
from typing_extensions import Literal

from nerfstudio.configs import base_config as cfg
from nerfstudio.data.datamanagers.base_datamanager import (
    DataManager,
    VanillaDataManager,
    VanillaDataManagerConfig,
)
from nerfstudio.engine.callbacks import (
    TrainingCallback,
    TrainingCallbackAttributes,
)
from nerfstudio.model_components.losses import MSELoss
from nerfstudio.models.base_model import Model, ModelConfig
from nerfstudio.utils import profiler

CONSOLE = Console(width=120)


def module_wrapper(ddp_or_model: Union[DDP, Model]) -> Model:
    """
    If DDP, then return the .module. Otherwise, return the model.
    """
    if isinstance(ddp_or_model, DDP):
        return cast(Model, ddp_or_model.module)
    return ddp_or_model


class Pipeline(nn.Module):
    """The intent of this class is to provide a higher level interface for the Model
    that will be easy to use for our Trainer class.

    This class will contain high level functions for the model like getting the loss
    dictionaries and visualization code. It should have ways to get the next iterations
    training loss, evaluation loss, and generate whole images for visualization. Each model
    class should be 1:1 with a pipeline that can act as a standardized interface and hide
    differences in how each model takes in and outputs data.

    This class's function is to hide the data manager and model classes from the trainer,
    worrying about:
    1) Fetching data with the data manager
    2) Feeding the model the data and fetching the loss
    Hopefully this provides a higher level interface for the trainer to use, and
    simplifying the model classes, which each may have different forward() methods
    and so on.

    Args:
        config: configuration to instantiate pipeline
        device: location to place model and data
        test_mode:
            'train': loads train/eval datasets into memory
            'test': loads train/test datset into memory
            'inference': does not load any dataset into memory
        world_size: total number of machines available
        local_rank: rank of current machine

    Attributes:
        datamanager: The data manager that will be used
        model: The model that will be used
    """

    # pylint: disable=abstract-method

    datamanager: DataManager
    _model: Model

    @property
    def model(self):
        """Returns the unwrapped model if in ddp"""
        return module_wrapper(self._model)

    @property
    def device(self):
        """Returns the device that the model is on."""
        return self.model.device

    @profiler.time_function
    def get_train_loss_dict(self, step: int):
        """This function gets your training loss dict. This will be responsible for
        getting the next batch of data from the DataManager and interfacing with the
        Model class, feeding the data to the model's forward function.

        Args:
            step: current iteration step to update sampler if using DDP (distributed)
        """
        if self.world_size > 1 and step:
            assert self.datamanager.train_sampler is not None
            self.datamanager.train_sampler.set_epoch(step)
        ray_bundle, batch = self.datamanager.next_train(step)
        model_outputs = self.model(ray_bundle, batch)
        metrics_dict = self.model.get_metrics_dict(model_outputs, batch)
        loss_dict = self.model.get_loss_dict(
            model_outputs, batch, metrics_dict
        )

        return model_outputs, loss_dict, metrics_dict

    @profiler.time_function
    def get_eval_loss_dict(self, step: int):
        """This function gets your evaluation loss dict. It needs to get the data
        from the DataManager and feed it to the model's forward function

        Args:
            step: current iteration step
        """
        self.eval()
        if self.world_size > 1:
            assert self.datamanager.eval_sampler is not None
            self.datamanager.eval_sampler.set_epoch(step)
        ray_bundle, batch = self.datamanager.next_eval(step)
        model_outputs = self.model(ray_bundle, batch)
        metrics_dict = self.model.get_metrics_dict(model_outputs, batch)
        loss_dict = self.model.get_loss_dict(
            model_outputs, batch, metrics_dict
        )
        self.train()
        return model_outputs, loss_dict, metrics_dict

    @abstractmethod
    @profiler.time_function
    def get_eval_image_metrics_and_images(self, step: int):
        """This function gets your evaluation loss dict. It needs to get the data
        from the DataManager and feed it to the model's forward function

        Args:
            step: current iteration step
        """

    @abstractmethod
    @profiler.time_function
    def get_average_eval_image_metrics(self, step: Optional[int] = None):
        """Iterate over all the images in the eval dataset and get the average."""

    def load_pipeline(self, loaded_state: Dict[str, Any]) -> None:
        """Load the checkpoint from the given path

        Args:
            loaded_state: pre-trained model state dict
        """

    def get_training_callbacks(
        self, training_callback_attributes: TrainingCallbackAttributes
    ) -> List[TrainingCallback]:
        """Returns the training callbacks from both the Dataloader and the Model."""

    def get_param_groups(self) -> Dict[str, List[Parameter]]:
        """Get the param groups for the pipeline.

        Returns:
            A list of dictionaries containing the pipeline's param groups.
        """


@dataclass
class VanillaPipelineConfig(cfg.InstantiateConfig):
    """Configuration for pipeline instantiation"""

    _target: Type = field(default_factory=lambda: VanillaPipeline)
    """Target class to instantiate."""
    datamanager: VanillaDataManagerConfig = VanillaDataManagerConfig()
    """Specifies the datamanager config."""
    model: ModelConfig = ModelConfig()
    """Specifies the model config."""
    eval_optimize_cameras: bool = False
    """Whether to optimize the cameras during evaluation."""
    eval_num_pose_iters: int = 10
    """Number of iterations to optimize the cameras."""
    eval_optimize_appearance: bool = False
    """Whether to optimize the appearance during evaluation."""
    eval_num_appearance_iters: int = 10
    """Number of iterations to optimize the appearance."""
    eval_image_scale_factor: float = 0.125
    """Scale factor to use for evaluation images so that they fit in memory."""


class VanillaPipeline(Pipeline):
    """The pipeline class for the vanilla nerf setup of multiple cameras for one or a few scenes.

        config: configuration to instantiate pipeline
        device: location to place model and data
        test_mode:
            'val': loads train/val datasets into memory
            'test': loads train/test datset into memory
            'inference': does not load any dataset into memory
        world_size: total number of machines available
        local_rank: rank of current machine

    Attributes:
        datamanager: The data manager that will be used
        model: The model that will be used
    """

    def __init__(
        self,
        config: VanillaPipelineConfig,
        device: str,
        test_mode: Literal["test", "val", "inference"] = "val",
        world_size: int = 1,
        local_rank: int = 0,
    ):
        super().__init__()
        self.config = config
        self.test_mode = test_mode
        self.datamanager: VanillaDataManager = config.datamanager.setup(
            device=device,
            test_mode=test_mode,
            world_size=world_size,
            local_rank=local_rank,
        )
        self.datamanager.to(device)
        # TODO(ethan): get rid of scene_bounds from the model
        assert (
            self.datamanager.train_dataset is not None
        ), "Missing input dataset"

        self._model = config.model.setup(
            scene_box=self.datamanager.train_dataset.scene_box,
            num_train_data=len(self.datamanager.train_dataset),
            num_eval_data=len(self.datamanager.eval_dataset),
            metadata=self.datamanager.train_dataset.metadata,
        )
        self.model.to(device)

        self.world_size = world_size
        if world_size > 1:
            self._model = typing.cast(
                Model,
                DDP(
                    self._model,
                    device_ids=[local_rank],
                    find_unused_parameters=True,
                ),
            )
            dist.barrier(device_ids=[local_rank])

    @property
    def device(self):
        """Returns the device that the model is on."""
        return self.model.device

    @profiler.time_function
    def get_train_loss_dict(self, step: int):
        """This function gets your training loss dict. This will be responsible for
        getting the next batch of data from the DataManager and interfacing with the
        Model class, feeding the data to the model's forward function.

        Args:
            step: current iteration step to update sampler if using DDP (distributed)
        """
        ray_bundle, batch = self.datamanager.next_train(step)
        model_outputs = self.model(ray_bundle)
        metrics_dict = self.model.get_metrics_dict(model_outputs, batch)

        camera_opt_param_group = (
            self.config.datamanager.camera_optimizer.param_group + "_train"
        )
        if camera_opt_param_group in self.datamanager.get_param_groups():
            # Report the camera optimization metrics
            metrics_dict["camera_opt_translation"] = (
                self.datamanager.get_param_groups()[camera_opt_param_group][0]
                .data[:, :3]
                .norm()
            )
            metrics_dict["camera_opt_rotation"] = (
                self.datamanager.get_param_groups()[camera_opt_param_group][0]
                .data[:, 3:]
                .norm()
            )

        loss_dict = self.model.get_loss_dict(
            model_outputs, batch, metrics_dict
        )

        return model_outputs, loss_dict, metrics_dict

    def forward(self):
        """Blank forward method

        This is an nn.Module, and so requires a forward() method normally, although in our case
        we do not need a forward() method"""
        raise NotImplementedError

    @profiler.time_function
    def get_eval_loss_dict(self, step: int):
        """This function gets your evaluation loss dict. It needs to get the data
        from the DataManager and feed it to the model's forward function

        Args:
            step: current iteration step
        """
        self.eval()
        ray_bundle, batch = self.datamanager.next_eval(step)
        model_outputs = self.model(ray_bundle)
        metrics_dict = self.model.get_metrics_dict(model_outputs, batch)
        loss_dict = self.model.get_loss_dict(
            model_outputs, batch, metrics_dict
        )
        self.train()
        return model_outputs, loss_dict, metrics_dict

    @profiler.time_function
    def get_eval_image_metrics_and_images(self, step: int):
        """This function gets your evaluation loss dict. It needs to get the data
        from the DataManager and feed it to the model's forward function

        Args:
            step: current iteration step
        """
        self.eval()
        image_idx, camera_ray_bundle, batch = self.datamanager.next_eval_image(
            step
        )
        with torch.no_grad():
            outputs = self.model.get_outputs_for_camera_ray_bundle(
                camera_ray_bundle
            )
            (
                metrics_dict,
                images_dict,
            ) = self.model.get_image_metrics_and_images(outputs, batch)
        assert "image_idx" not in metrics_dict
        metrics_dict["image_idx"] = image_idx
        assert "num_rays" not in metrics_dict
        metrics_dict["num_rays"] = len(camera_ray_bundle)
        self.train()
        return metrics_dict, images_dict

    @profiler.time_function
    def get_average_eval_image_metrics(
        self, step: Optional[int] = None, return_imgs: bool = False
    ):
        """Iterate over all the images in the eval dataset and get the average.

        Returns:
            metrics_dict: dictionary of metrics
        """
        self.eval()
        metrics_dict_list, images_dict_list = [], []
        num_images = len(self.datamanager.eval_dataset)
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TimeElapsedColumn(),
            MofNCompleteColumn(),
            transient=True,
        ) as progress:
            task = progress.add_task(
                "[green]Evaluating all eval images...", total=num_images
            )
            for idx in range(num_images):
                camera_ray_bundle, batch = self.datamanager.get_eval_image(idx)
                # time this the following line
                inner_start = time()
                height, width = camera_ray_bundle.shape
                num_rays = height * width

                # optimize camera poses on full image
                if self.config.eval_optimize_cameras:

                    # raise error if the method not nerfacto

                    # get the eval camera optimizer's parameters
                    camera_opt_param_group = (
                        self.config.datamanager.camera_optimizer.param_group
                        + "_eval"
                    )
                    # reinitialize the parameters
                    self.datamanager.eval_camera_optimizer.initialize_parameters()
                    param_groups = self.datamanager.get_param_groups()
                    optimizer = torch.optim.Adam(
                        param_groups[camera_opt_param_group],
                        lr=1e-3,
                        eps=1e-15,
                    )

                    # # check that camera optimizer is on
                    # if self.config.datamanager.camera_optimizer.mode == "off":
                    #     raise ValueError(
                    #         "You must set datamanager.camera_optimizer.mode to optimize to optimize the camera poses."
                    #     )

                    # CONSOLE.print("Optimizing camera poses...")
                    # rescale the image to 1/4 resolution
                    self.datamanager.eval_ray_generator.cameras.rescale_output_resolution(
                        self.config.eval_image_scale_factor
                    )
                    self.train()
                    for _ in range(self.config.eval_num_pose_iters):
                        # optimize the ray generator's camera optimizer
                        optimizer.zero_grad()
                        (
                            camera_ray_bundle,
                            batch,
                        ) = self.datamanager.get_eval_image(
                            idx,
                            scale_factor=self.config.eval_image_scale_factor,
                        )
                        outputs = self.model.get_outputs_for_camera_ray_bundle(
                            camera_ray_bundle
                        )
                        metrics_dict = self.model.get_metrics_dict(
                            outputs, batch
                        )
                        loss_dict = self.model.get_loss_dict(
                            outputs, batch, metrics_dict
                        )

                        # save the image
                        # import mediapy as media
                        # _, images_dict = self.model.get_image_metrics_and_images(outputs, batch)
                        # media.write_image("camera_opt.png", images_dict["img"].detach().cpu().numpy())

                        loss_dict["rgb_loss"].backward()
                        optimizer.step()
                    self.datamanager.eval_ray_generator.cameras.rescale_output_resolution(
                        1.0 / self.config.eval_image_scale_factor
                    )
                    self.eval()
                    # CONSOLE.print("Done optimizing camera poses.")

                if self.config.eval_optimize_appearance:
                    # CONSOLE.print("Optimizing appearance...")
                    self.datamanager.eval_ray_generator.cameras.rescale_output_resolution(
                        self.config.eval_image_scale_factor
                    )
                    self.train()

                    # initial the parameters as the mean appearance embedding
                    self.model.field.initialize_embedding_appearance_eval()
                    appearance_parameters = (
                        self.model.field.embedding_appearance_eval.parameters()
                    )
                    optimizer = torch.optim.Adam(
                        appearance_parameters, lr=1e-3, eps=1e-15
                    )

                    side = random.choice([0, 1])
                    half_width = width // 2
                    rgb_loss = MSELoss()

                    for _ in range(self.config.eval_num_appearance_iters):
                        optimizer.zero_grad()
                        (
                            camera_ray_bundle,
                            batch,
                        ) = self.datamanager.get_eval_image(
                            idx,
                            scale_factor=self.config.eval_image_scale_factor,
                        )
                        outputs = self.model.get_outputs_for_camera_ray_bundle(
                            camera_ray_bundle
                        )
                        metrics_dict = self.model.get_metrics_dict(
                            outputs, batch
                        )

                        # compute loss on half the image
                        image = batch["image"].to(self.device)
                        if side == 0:
                            loss = rgb_loss(
                                image[:, :half_width],
                                outputs["rgb"][:, :half_width],
                            )
                        else:
                            loss = rgb_loss(
                                image[:, half_width:],
                                outputs["rgb"][:, half_width:],
                            )

                        # save the image
                        # import mediapy as media
                        # _, images_dict = self.model.get_image_metrics_and_images(outputs, batch)
                        # media.write_image("appearance_opt.png", images_dict["img"].detach().cpu().numpy())

                        loss.backward()
                        optimizer.step()

                    self.datamanager.eval_ray_generator.cameras.rescale_output_resolution(
                        1.0 / self.config.eval_image_scale_factor
                    )
                    # delete the appearance embedding
                    self.model.field.embedding_appearance_eval = None
                    self.eval()
                    # CONSOLE.print("Done optimizing appearance.")

                with torch.no_grad():
                    outputs = self.model.get_outputs_for_camera_ray_bundle(
                        camera_ray_bundle
                    )
                    (
                        metrics_dict,
                        images_dict,
                    ) = self.model.get_image_metrics_and_images(outputs, batch)
                assert "num_rays_per_sec" not in metrics_dict
                metrics_dict["num_rays_per_sec"] = num_rays / (
                    time() - inner_start
                )
                fps_str = "fps"
                assert fps_str not in metrics_dict
                metrics_dict[fps_str] = metrics_dict["num_rays_per_sec"] / (
                    height * width
                )
                metrics_dict_list.append(metrics_dict)
                images_dict_list.append(images_dict)
                progress.advance(task)
        # average the metrics list
        metrics_dict = {}
        for key in metrics_dict_list[0].keys():
            metrics_dict[key] = float(
                torch.mean(
                    torch.tensor(
                        [
                            metrics_dict[key]
                            for metrics_dict in metrics_dict_list
                        ]
                    )
                )
            )
        images_dict = {}
        for key in images_dict_list[0].keys():
            images_dict[key] = torch.stack(
                [images_dict[key] for images_dict in images_dict_list],
                dim=0,
            )
        self.train()
        if not return_imgs:
            return metrics_dict
        else:
            return metrics_dict, images_dict

    def load_pipeline(self, loaded_state: Dict[str, Any]) -> None:
        """Load the checkpoint from the given path

        Args:
            loaded_state: pre-trained model state dict
        """
        state = {
            key.replace("module.", ""): value
            for key, value in loaded_state.items()
        }
        if self.test_mode == "inference":
            state.pop(
                "datamanager.train_camera_optimizer.pose_adjustment", None
            )
            state.pop("datamanager.train_ray_generator.image_coords", None)
            state.pop(
                "datamanager.train_ray_generator.pose_optimizer.pose_adjustment",
                None,
            )
            state.pop(
                "datamanager.eval_camera_optimizer.pose_adjustment", None
            )
            state.pop("datamanager.eval_ray_generator.image_coords", None)
            state.pop(
                "datamanager.eval_ray_generator.pose_optimizer.pose_adjustment",
                None,
            )
        self.load_state_dict(state)  # type: ignore

    def get_training_callbacks(
        self, training_callback_attributes: TrainingCallbackAttributes
    ) -> List[TrainingCallback]:
        """Returns the training callbacks from both the Dataloader and the Model."""
        datamanager_callbacks = self.datamanager.get_training_callbacks(
            training_callback_attributes
        )
        model_callbacks = self.model.get_training_callbacks(
            training_callback_attributes
        )
        callbacks = datamanager_callbacks + model_callbacks
        return callbacks

    def get_param_groups(self) -> Dict[str, List[Parameter]]:
        """Get the param groups for the pipeline.

        Returns:
            A list of dictionaries containing the pipeline's param groups.
        """
        datamanager_params = self.datamanager.get_param_groups()
        model_params = self.model.get_param_groups()
        # TODO(ethan): assert that key names don't overlap
        return {**datamanager_params, **model_params}
