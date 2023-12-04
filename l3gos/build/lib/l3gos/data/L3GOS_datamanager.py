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
Datamanager.
"""

from __future__ import annotations

import os.path as osp
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, ForwardRef, Generic, List, Literal, Optional, Tuple, Type, Union, cast, get_args, get_origin
import random
import cv2
import numpy as np
import torch
import time
from copy import deepcopy, copy
from torch.nn import Parameter
from tqdm import tqdm

from nerfstudio.cameras.rays import RayBundle
from nerfstudio.cameras.cameras import Cameras, CameraType
from nerfstudio.configs.dataparser_configs import AnnotatedDataParserUnion
from nerfstudio.data.dataparsers.base_dataparser import DataparserOutputs
from nerfstudio.data.dataparsers.nerfstudio_dataparser import NerfstudioDataParserConfig
from nerfstudio.data.datasets.base_dataset import InputDataset
from nerfstudio.utils.misc import get_orig_class
from nerfstudio.utils.rich_utils import CONSOLE

from rich.progress import Console
from l3gos.data.L3GOS_dataloader import L3GOSDataloader

CONSOLE = Console(width=120)

# from lerf.data.utils.dino_dataloader import DinoDataloader
# from lerf.data.utils.pyramid_embedding_dataloader import PyramidEmbeddingDataloader
from functools import cached_property
from nerfstudio.data.datamanagers.base_datamanager import DataManager, DataManagerConfig, TDataset
import torch.multiprocessing as mp
from l3gos.encoders.image_encoder import BaseImageEncoderConfig, BaseImageEncoder
from l3gos.data.L3GOS_dataparser import L3GOSDataParserConfig
from l3gos.data.L3GOS_dataset import L3GOSDataset
from nerfstudio.utils.misc import get_orig_class



@dataclass
class L3GOSDataManagerConfig(DataManagerConfig):
    _target: Type = field(default_factory=lambda: L3GOSDataManager)
    dataparser: AnnotatedDataParserUnion = L3GOSDataParserConfig()
    camera_res_scale_factor: float = 1.0
    """The scale factor for scaling spatial data such as images, mask, semantics
    along with relevant information about camera intrinsics
    """
    eval_num_images_to_sample_from: int = -1
    """Number of images to sample during eval iteration."""
    eval_num_times_to_repeat_images: int = -1
    """When not evaluating on all images, number of iterations before picking
    new images. If -1, never pick new images."""
    eval_image_indices: Optional[Tuple[int, ...]] = (0,)
    """Specifies the image indices to use during eval; if None, uses all."""
    cache_images: Literal["no-cache", "cpu", "gpu"] = "cpu"
    """Whether to cache images in memory. If "numpy", caches as numpy arrays, if "torch", caches as torch tensors."""


class L3GOSDataManager(DataManager, Generic[TDataset]):
    """
    A datamanager that outputs full images and cameras instead of raybundles. This makes the
    datamanager more lightweight since we don't have to do generate rays. Useful for full-image
    training e.g. rasterization pipelines
    """

    config: L3GOSDataManagerConfig
    train_dataset: TDataset
    eval_dataset: TDataset

    def __init__(
        self,
        config: L3GOSDataManagerConfig,
        device: Union[torch.device, str] = "cpu",
        test_mode: Literal["test", "val", "inference"] = "val",
        world_size: int = 1,
        local_rank: int = 0,
        **kwargs,
    ):
        self.config = config
        self.device = device
        self.world_size = world_size
        self.local_rank = local_rank
        self.sampler = None
        self.test_mode = test_mode
        self.test_split = "test" if test_mode in ["test", "inference"] else "val"
        self.dataparser_config = self.config.dataparser
        if self.config.data is not None:
            self.config.dataparser.data = Path(self.config.data)
        else:
            self.config.data = self.config.dataparser.data
        self.dataparser = self.dataparser_config.setup()
        if test_mode == "inference":
            self.dataparser.downscale_factor = 1  # Avoid opening images
        self.includes_time = self.dataparser.includes_time

        self.train_dataparser_outputs: DataparserOutputs = self.dataparser.get_dataparser_outputs(split="train")
        self.train_dataset = self.create_train_dataset()
        self.eval_dataset = self.create_eval_dataset()
        if len(self.train_dataset) > 500 and self.config.cache_images == "gpu":
            # CONSOLE.print("Train dataset has over 500 images, overriding cach_images to cpu", style="bold yellow")
            self.config.cache_images = "cpu"
        self.cached_train, self.cached_eval = self.cache_images(self.config.cache_images)
        # self.exclude_batch_keys_from_device = self.train_dataset.exclude_batch_keys_from_device
        # if self.config.masks_on_gpu is True:
        #     self.exclude_batch_keys_from_device.remove("mask")
        # if self.config.images_on_gpu is True:
        #     self.exclude_batch_keys_from_device.remove("image")

        # Some logic to make sure we sample every camera in equal amounts
        self.train_unseen_cameras = [i for i in range(len(self.train_dataset))]
        self.eval_unseen_cameras = [i for i in range(len(self.eval_dataset))]
        # assert len(self.train_unseen_cameras) > 0, "No data found in dataset"

        super().__init__()

    def cache_images(self, cache_images_option):
        cached_train = []
        # CONSOLE.log("Caching / undistorting train images")
        for i in tqdm(range(len(self.train_dataset)), leave=False):
            # cv2.undistort the images / cameras
            data = self.train_dataset.get_data(i)
            camera = self.train_dataset.cameras[i].reshape(())
            K = camera.get_intrinsics_matrices().numpy()
            distortion_params = camera.distortion_params.numpy()
            image = data["image"].numpy()

            if camera.camera_type.item() == CameraType.PERSPECTIVE.value:
                distortion_params = np.array(
                    [
                        distortion_params[0],
                        distortion_params[1],
                        distortion_params[4],
                        distortion_params[5],
                        distortion_params[2],
                        distortion_params[3],
                        0,
                        0,
                    ]
                )
                newK, roi = cv2.getOptimalNewCameraMatrix(K, distortion_params, (image.shape[1], image.shape[0]), 0)
                image = cv2.undistort(image, K, distortion_params, None, newK)
                # crop the image and update the intrinsics accordingly
                x, y, w, h = roi
                image = image[y : y + h, x : x + w]
                if "mask" in data:
                    data["mask"] = data["mask"][y : y + h, x : x + w]
                if "depth_image" in data:
                    data["depth_image"] = data["depth_image"][y : y + h, x : x + w]
                K = newK
                # update the width, height
                self.train_dataset.cameras.width[i] = w
                self.train_dataset.cameras.height[i] = h

            elif camera.camera_type.item() == CameraType.FISHEYE.value:
                distortion_params = np.array(
                    [distortion_params[0], distortion_params[1], distortion_params[2], distortion_params[3]]
                )
                newK = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(
                    K, distortion_params, (image.shape[1], image.shape[0]), np.eye(3), balance=0
                )
                map1, map2 = cv2.fisheye.initUndistortRectifyMap(
                    K, distortion_params, np.eye(3), newK, (image.shape[1], image.shape[0]), cv2.CV_32FC1
                )
                # and then remap:
                image = cv2.remap(image, map1, map2, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
                K = newK
            else:
                raise NotImplementedError("Only perspective and fisheye cameras are supported")
            data["image"] = torch.from_numpy(image)

            if "mask" in data:
                mask = data["mask"].numpy()
                if camera.camera_type.item() == CameraType.PERSPECTIVE.value:
                    mask = cv2.undistort(mask, K, distortion_params, None, None)
                elif camera.camera_type.item() == CameraType.FISHEYE.value:
                    mask = cv2.fisheye.undistortImage(mask, K, distortion_params, None, None)
                else:
                    raise NotImplementedError("Only perspective and fisheye cameras are supported")
                data["mask"] = torch.from_numpy(mask)

            cached_train.append(data)

            self.train_dataset.cameras.fx[i] = float(K[0, 0])
            self.train_dataset.cameras.fy[i] = float(K[1, 1])
            self.train_dataset.cameras.cx[i] = float(K[0, 2])
            self.train_dataset.cameras.cy[i] = float(K[1, 2])

        cached_eval = []
        # CONSOLE.log("Caching / undistorting eval images")
        for i in tqdm(range(len(self.eval_dataset)), leave=False):
            # cv2.undistort the images / cameras
            data = self.eval_dataset.get_data(i)
            camera = self.eval_dataset.cameras[i].reshape(())
            K = camera.get_intrinsics_matrices().numpy()
            distortion_params = camera.distortion_params.numpy()
            image = data["image"].numpy()

            if camera.camera_type.item() == CameraType.PERSPECTIVE.value:
                distortion_params = np.array(
                    [
                        distortion_params[0],
                        distortion_params[1],
                        distortion_params[4],
                        distortion_params[5],
                        distortion_params[2],
                        distortion_params[3],
                        0,
                        0,
                    ]
                )
                newK, roi = cv2.getOptimalNewCameraMatrix(K, distortion_params, (image.shape[1], image.shape[0]), 0)
                image = cv2.undistort(image, K, distortion_params, None, newK)
                # crop the image and update the intrinsics accordingly
                x, y, w, h = roi
                image = image[y : y + h, x : x + w]
                if "mask" in data:
                    data["mask"] = data["mask"][y : y + h, x : x + w]
                if "depth_image" in data:
                    data["depth_image"] = data["depth_image"][y : y + h, x : x + w]
                K = newK
                # update the width, height
                self.eval_dataset.cameras.width[i] = w
                self.eval_dataset.cameras.height[i] = h

            elif camera.camera_type.item() == CameraType.FISHEYE.value:
                distortion_params = np.array(
                    [distortion_params[0], distortion_params[1], distortion_params[2], distortion_params[3]]
                )
                newK = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(
                    K, distortion_params, (image.shape[1], image.shape[0]), np.eye(3), balance=0
                )
                map1, map2 = cv2.fisheye.initUndistortRectifyMap(
                    K, distortion_params, np.eye(3), newK, (image.shape[1], image.shape[0]), cv2.CV_32FC1
                )
                # and then remap:
                image = cv2.remap(image, map1, map2, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
                K = newK
            else:
                raise NotImplementedError("Only perspective and fisheye cameras are supported")
            data["image"] = torch.from_numpy(image)

            if "mask" in data:
                mask = data["mask"].numpy()
                if camera.camera_type.item() == CameraType.PERSPECTIVE.value:
                    mask = cv2.undistort(mask, K, distortion_params, None, None)
                elif camera.camera_type.item() == CameraType.FISHEYE.value:
                    mask = cv2.fisheye.undistortImage(mask, K, distortion_params, None, None)
                else:
                    raise NotImplementedError("Only perspective and fisheye cameras are supported")
                data["mask"] = torch.from_numpy(mask)

            cached_eval.append(data)

            self.eval_dataset.cameras.fx[i] = float(K[0, 0])
            self.eval_dataset.cameras.fy[i] = float(K[1, 1])
            self.eval_dataset.cameras.cx[i] = float(K[0, 2])
            self.eval_dataset.cameras.cy[i] = float(K[1, 2])

        if cache_images_option == "gpu":
            for cache in cached_train:
                cache["image"] = cache["image"].to(self.device)
                if "mask" in cache:
                    cache["mask"] = cache["mask"].to(self.device)
            for cache in cached_eval:
                cache["image"] = cache["image"].to(self.device)
                if "mask" in cache:
                    cache["mask"] = cache["mask"].to(self.device)
        else:
            for cache in cached_train:
                cache["image"] = cache["image"].pin_memory()
                if "mask" in cache:
                    cache["mask"] = cache["mask"].pin_memory()
            for cache in cached_eval:
                cache["image"] = cache["image"].pin_memory()
                if "mask" in cache:
                    cache["mask"] = cache["mask"].pin_memory()

        return cached_train, cached_eval

    def create_train_dataset(self) -> TDataset:
        """Sets up the data loaders for training"""
        return self.dataset_type(
            dataparser_outputs=self.train_dataparser_outputs,
            scale_factor=self.config.camera_res_scale_factor,
        )

    def create_eval_dataset(self) -> TDataset:
        """Sets up the data loaders for evaluation"""
        return self.dataset_type(
            dataparser_outputs=self.dataparser.get_dataparser_outputs(split=self.test_split),
            scale_factor=self.config.camera_res_scale_factor,
        )

    @cached_property
    def dataset_type(self) -> Type[TDataset]:
        return L3GOSDataset
    

    def get_datapath(self) -> Path:
        return self.config.dataparser.data

    def setup_train(self):
        """Sets up the data loaders for training"""

    def setup_eval(self):
        """Sets up the data loader for evaluation"""

    @property
    def fixed_indices_eval_dataloader(self) -> List[Tuple[Cameras, Dict]]:
        """
        Pretends to be the dataloader for evaluation, it returns a list of (camera, data) tuples
        """
        image_indices = list(range(len(self.eval_unseen_cameras)))
        data = deepcopy(self.cached_eval)
        _cameras = deepcopy(self.eval_dataset.cameras).to(self.device)
        cameras = []
        for i in image_indices:
            data[i]["image"] = data[i]["image"].to(self.device)
            cameras.append(_cameras[i : i + 1])
        assert len(self.eval_dataset.cameras.shape) == 1, "Assumes single batch dimension"
        return list(zip(cameras, data))

    def get_param_groups(self) -> Dict[str, List[Parameter]]:
        """Get the param groups for the data manager.
        Returns:
            A list of dictionaries containing the data manager's param groups.
        """
        return {}

    def get_train_rays_per_batch(self):
        # TODO: fix this to be the resolution of the last image rendered
        return 800 * 800

    def next_train(self, step: int) -> Tuple[Cameras, Dict]:
        """Returns the next training batch

        Returns a Camera instead of raybundle"""
        # print(len(self.train_unseen_cameras))
        # print(self.train_unseen_cameras)
        image_idx = self.train_unseen_cameras.pop()
        # print(image_idx)
        # Make sure to re-populate the unseen cameras list if we have exhausted it
        if len(self.train_unseen_cameras) == 0:
            self.train_unseen_cameras = [i for i in range(len(self.train_dataset))]
        
        # start = time.time()
        data = copy(self.cached_train[image_idx])
        data["image"] = data["image"].to(self.device)
        # end = time.time()
        # elapsed = str((end-start)*1e3)
        # print("copy time: "+ elapsed + "(ms)")

        assert len(self.train_dataset.cameras.shape) == 1, "Assumes single batch dimension"
        camera = self.train_dataset.cameras[image_idx : image_idx + 1].to(self.device)
        if camera.metadata is None:
            camera.metadata = {}
        camera.metadata["cam_idx"] = image_idx
        
        return camera, data

    def next_eval(self, step: int) -> Tuple[Cameras, Dict]:
        """Returns the next evaluation batch

        Returns a Camera instead of raybundle"""
        image_idx = self.eval_unseen_cameras.pop(random.randint(0, len(self.eval_unseen_cameras) - 1))
        # Make sure to re-populate the unseen cameras list if we have exhausted it
        if len(self.eval_unseen_cameras) == 0:
            self.eval_unseen_cameras = [i for i in range(len(self.eval_dataset))]
        data = deepcopy(self.cached_eval[image_idx])
        data["image"] = data["image"].to(self.device)
        assert len(self.eval_dataset.cameras.shape) == 1, "Assumes single batch dimension"
        camera = self.eval_dataset.cameras[image_idx : image_idx + 1].to(self.device)
        return camera, data

    def next_eval_image(self, step: int) -> Tuple[Cameras, Dict]:
        """Returns the next evaluation batch

        Returns a Camera instead of raybundle

        TODO: Make sure this logic is consistent with the vanilladatamanager"""
        image_idx = self.eval_unseen_cameras.pop(random.randint(0, len(self.eval_unseen_cameras) - 1))
        # Make sure to re-populate the unseen cameras list if we have exhausted it
        if len(self.eval_unseen_cameras) == 0:
            self.eval_unseen_cameras = [i for i in range(len(self.eval_dataset))]
        data = deepcopy(self.cached_eval[image_idx])
        data["image"] = data["image"].to(self.device)
        assert len(self.eval_dataset.cameras.shape) == 1, "Assumes single batch dimension"
        camera = self.eval_dataset.cameras[image_idx : image_idx + 1].to(self.device)
        return camera, data
    def setup_train(self):
        """Sets up the data loaders for training"""
        assert self.train_dataset is not None
        CONSOLE.print("Setting up training dataset...")
        self.train_image_dataloader = L3GOSDataloader(self.train_dataset)
        self.iter_train_image_dataloader = iter(self.train_image_dataloader)
        raise NotImplementedError


    def add_image(self, img:torch.tensor, cam: Cameras):
        """
        Adds a new image to the datamanager
        1. add the actual image data
        2. make sure pixel sampling works on that                        (Should work because we override the __getitem__ function in lll_dataset)
        3. add lerf dino+clip features                                   (Justin)
        4. reset camera param for optimization                           (I think we should do this in trainer on the image callback)
        5. make sure we set the mask for the image we just added         (We should handle masks in the pipeline because adding one image requires adding a bunch of masks)
        """
        # ----------------- Handling the lerf features ----------------
        pass
        # self.clip_interpolator.add_images(img.unsqueeze(0))
        # self.dino_dataloader.add_images(img.unsqueeze(0))


        # ----------------- Handling the IMAGE ----------------
        # self.train_dataset.add_image(img,cam)
        # self.train_ray_generator.cameras = self.train_dataset.cameras.to(self.device)

    def process_image(self, img:torch.tensor, cam: Cameras, clip, dino):
        # ----------------- Handling the IMAGE ----------------
        # raise NotImplementedError
        self.train_dataset.add_image(img,cam)
        self.train_unseen_cameras = [i for i in range(len(self.train_dataset))]
        
        data = self.train_dataset[len(self.train_dataset)-1]
        self.cached_train.append(data)
        # print(self.train_dataset.get_data)
        # print(self.train_dataset[-1])
        # self.train_ray_generator.cameras = self.train_dataset.cameras.to(self.device)
        # dino = dino.to(self.device)
        # for i, tr in enumerate(self.clip_interpolator.tile_sizes):
        #     clip[i] = clip[i].to(self.device)
        #     if self.clip_interpolator.data_dict[i].data is not None:
        #         self.clip_interpolator.data_dict[i].data = torch.cat([self.clip_interpolator.data_dict[i].data, clip[i]])
        #     else:
        #         self.clip_interpolator.data_dict[i].data = clip[i]
        # if self.dino_dataloader.data is None:
        #     self.dino_dataloader.data = dino
        # else:
        #     self.dino_dataloader.data = torch.cat([self.dino_dataloader.data, dino], dim=0)



    # def next_train(self, step: int) -> Tuple[RayBundle, Dict]:
    #     """Returns the next batch of data from the train dataloader."""
    #     # raise NotImplementedError
    #     self.train_count += 1
    #     image_batch = next(self.iter_train_image_dataloader)
    #     assert self.train_pixel_sampler is not None
    #     batch = self.train_pixel_sampler.sample(image_batch)
    #     ray_indices = batch["indices"]
    #     ray_bundle = self.train_ray_generator(ray_indices)
    #     # batch["clip"], clip_scale = self.clip_interpolator(ray_indices)
    #     # batch["dino"] = self.dino_dataloader(ray_indices)
    #     # ray_bundle.metadata["clip_scales"] = clip_scale
    #     # # assume all cameras have the same focal length and image width
    #     ray_bundle.metadata["fx"] = self.train_dataset.cameras[0].fx.item()
    #     ray_bundle.metadata["width"] = self.train_dataset.cameras[0].width.item()
    #     ray_bundle.metadata["fy"] = self.train_dataset.cameras[0].fy.item()
    #     ray_bundle.metadata["height"] = self.train_dataset.cameras[0].height.item()
    #     return ray_bundle, batch
